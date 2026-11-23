"""
collector.py - Assetto Corsa telemetry logger
=============================================

WHAT THIS DOES
    Reads live telemetry from Assetto Corsa (original) ~60 times per second
    and saves it to Parquet files, one folder per session. It also writes a
    small metadata.json (car, track, layout, track length) into each session.

HOW IT FITS IN THE PROJECT
    AC shared memory --> THIS SCRIPT --> sessions/<session>/part_XXXXX.parquet
                                     +-> sessions/<session>/metadata.json
                                              |
                                              +--> pipeline.py (DuckDB views)
                                                   --> laps.py, resample.py,
                                                       compare.py, segments.py

REQUIREMENTS
    - Windows, running on the same PC as the game (shared memory is local only)
    - pip install pyarrow
    - Assetto Corsa must be RUNNING before you start this script, otherwise the
      shared memory blocks don't exist yet and you'll get an error.

USAGE
    python collector.py      (Ctrl+C to stop; the last data is saved on exit)
    Stop and restart the collector whenever you change car or track, so each
    session folder contains exactly one car/track combination.
"""

# ---------------------------------------------------------------------------
# IMPORTS
# ---------------------------------------------------------------------------
# NOTE: if your editor offers to auto-import things while you type (for example
# "from curses import meta"), dismiss it. Only the imports below are needed.
import ctypes, mmap, time, uuid, json        # ctypes+mmap: read AC's memory. json: metadata file.
from ctypes import c_int32, c_float, c_wchar # C data types matching what AC uses internally
from pathlib import Path                     # cleaner file/folder paths than plain strings
import pyarrow as pa                         # builds in-memory tables
import pyarrow.parquet as pq                 # writes those tables to Parquet files

# Folder this script lives in. Using it (instead of a relative path) means the
# "sessions" folder always lands next to collector.py, wherever you run it from.
BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# STRUCT DEFINITIONS (the "blueprint" of AC's memory)
# ---------------------------------------------------------------------------
# AC writes its data into memory as raw bytes laid out like a C struct.
# Python can't make sense of raw bytes on its own, so these classes describe
# EXACTLY what is stored and in what order.
#
# IMPORTANT RULES:
#   1. Field ORDER must match AC's definition exactly. One missing or
#      misplaced field shifts every field after it -> garbage values.
#   2. _pack_ = 4 matches AC's byte alignment. Without it, Python may insert
#      padding bytes and shift everything.
#   3. "c_float * 4" = an array of 4 floats. For anything per-wheel, the
#      order is: [0]=front-left, [1]=front-right, [2]=rear-left, [3]=rear-right.
#   4. Don't delete fields you don't use. They still occupy space in memory,
#      so removing them would misalign the ones after them.

class Physics(ctypes.Structure):
    """Fast-changing car data: pedals, speed, g-forces, tyres, brakes, etc."""
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int32),                 # counter that increments each physics update
        ("gas", c_float),                      # throttle pedal, 0.0 to 1.0
        ("brake", c_float),                    # brake pedal, 0.0 to 1.0
        ("fuel", c_float),                     # fuel remaining (litres)
        ("gear", c_int32),                     # 0 = reverse, 1 = neutral, 2 = 1st, ... (we subtract 1 later)
        ("rpms", c_int32),                     # engine RPM
        ("steerAngle", c_float),               # steering input, roughly -1.0 (left) to 1.0 (right)
        ("speedKmh", c_float),                 # speed in km/h
        ("velocity", c_float * 3),             # velocity vector (x, y, z)
        ("accG", c_float * 3),                 # g-forces: [0]=lateral, [1]=vertical, [2]=longitudinal
        ("wheelSlip", c_float * 4),            # slip per wheel
        ("wheelLoad", c_float * 4),            # load on each wheel
        ("wheelsPressure", c_float * 4),       # tyre pressures (psi)
        ("wheelAngularSpeed", c_float * 4),    # how fast each wheel spins
        ("tyreWear", c_float * 4),             # tyre wear per wheel
        ("tyreDirtyLevel", c_float * 4),       # dirt on each tyre
        ("tyreCoreTemperature", c_float * 4),  # tyre core temperature (C)
        ("camberRAD", c_float * 4),            # camber angle (radians)
        ("suspensionTravel", c_float * 4),     # suspension compression per wheel
        ("drs", c_float),                      # DRS state
        ("tc", c_float),                       # traction control activity
        ("heading", c_float),                  # car orientation: yaw
        ("pitch", c_float),                    # car orientation: pitch
        ("roll", c_float),                     # car orientation: roll
        ("cgHeight", c_float),                 # centre-of-gravity height
        ("carDamage", c_float * 5),            # damage on 5 zones of the car
        ("numberOfTyresOut", c_int32),         # how many tyres are off track (0-4)
        ("pitLimiterOn", c_int32),             # pit limiter on/off
        ("abs", c_float),                      # ABS activity
        ("kersCharge", c_float),               # KERS/ERS battery charge
        ("kersInput", c_float),                # KERS/ERS input
        ("autoShifterOn", c_int32),            # automatic gearbox on/off
        ("rideHeight", c_float * 2),           # ride height [front, rear]
        ("turboBoost", c_float),               # turbo boost pressure
        ("ballast", c_float),                  # ballast weight
        ("airDensity", c_float),               # air density
        ("airTemp", c_float),                  # air temperature (C)
        ("roadTemp", c_float),                 # track surface temperature (C)
        ("localAngularVel", c_float * 3),      # angular velocity of the car
        ("finalFF", c_float),                  # final force-feedback value
        ("performanceMeter", c_float),         # AC's live time-delta vs best lap
        ("engineBrake", c_int32),              # engine brake setting
        ("ersRecoveryLevel", c_int32),         # ERS recovery level
        ("ersPowerLevel", c_int32),            # ERS power level
        ("ersHeatCharging", c_int32),          # ERS heat charging mode
        ("ersIsCharging", c_int32),            # is ERS currently charging
        ("kersCurrentKJ", c_float),            # KERS energy used this lap (kJ)
        ("drsAvailable", c_int32),             # DRS available
        ("drsEnabled", c_int32),               # DRS currently open
        ("brakeTemp", c_float * 4),            # brake disc temperature per wheel (C)
        ("clutch", c_float),                   # clutch pedal
        ("tyreTempI", c_float * 4),            # tyre INNER surface temp per wheel
        ("tyreTempM", c_float * 4),            # tyre MIDDLE surface temp per wheel
        ("tyreTempO", c_float * 4),            # tyre OUTER surface temp per wheel
    ]


class Graphics(ctypes.Structure):
    """Session-level data: lap count, lap times, track position, coordinates."""
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int32),                 # counter that increments each graphics update
        ("status", c_int32),                   # 0=off/menu, 1=replay, 2=LIVE (driving), 3=paused
        ("session", c_int32),                  # session type (practice, qualifying, race...)
        ("currentTime", c_wchar * 15),         # current lap time as text
        ("lastTime", c_wchar * 15),            # last lap time as text
        ("bestTime", c_wchar * 15),            # best lap time as text
        ("split", c_wchar * 15),               # sector split as text
        ("completedLaps", c_int32),            # laps completed so far (our "lap number")
        ("position", c_int32),                 # race position
        ("iCurrentTime", c_int32),             # current lap time in MILLISECONDS
        ("iLastTime", c_int32),                # last lap time in milliseconds
        ("iBestTime", c_int32),                # best lap time in milliseconds
        ("sessionTimeLeft", c_float),          # time remaining in session
        ("distanceTraveled", c_float),         # total distance driven
        ("isInPit", c_int32),                  # 1 while in the pit lane
        ("currentSectorIndex", c_int32),       # which sector you're in (0, 1, 2)
        ("lastSectorTime", c_int32),           # last sector time (ms)
        ("numberOfLaps", c_int32),             # total laps in the session
        ("tyreCompound", c_wchar * 33),        # tyre compound name
        ("replayTimeMultiplier", c_float),     # replay speed
        ("normalizedCarPosition", c_float),    # position on track: 0.0 (start line) to 1.0 (back at start line)
        ("carCoordinates", c_float * 3),       # world position (x, y, z). y is height.
    ]


class Static(ctypes.Structure):
    """
    Per-session constants: car, track, layout, track length.
    This is only the FIRST part of AC's static block (we stop at the layout
    name, since that's all we need). Mapping a prefix is fine as long as the
    fields we do list are in exactly the right order.
    """
    _pack_ = 4
    _fields_ = [
        ("smVersion", c_wchar * 15),           # shared-memory version
        ("acVersion", c_wchar * 15),           # Assetto Corsa version
        ("numberOfSessions", c_int32),         # number of sessions in this event
        ("numCars", c_int32),                  # number of cars on track
        ("carModel", c_wchar * 33),            # car id, e.g. "ks_toyota_ae86"
        ("track", c_wchar * 33),               # track id
        ("playerName", c_wchar * 33),          # driver first name
        ("playerSurname", c_wchar * 33),       # driver surname
        ("playerNick", c_wchar * 33),          # driver nickname
        ("sectorCount", c_int32),              # number of sectors
        ("maxTorque", c_float),                # car max torque
        ("maxPower", c_float),                 # car max power
        ("maxRpm", c_int32),                   # engine rev limit
        ("maxFuel", c_float),                  # fuel tank size (litres)
        ("suspensionMaxTravel", c_float * 4),  # max suspension travel per wheel
        ("tyreRadius", c_float * 4),           # tyre radius per wheel
        ("maxTurboBoost", c_float),            # max turbo boost
        ("deprecated_1", c_float),             # unused by AC, but still takes up space
        ("deprecated_2", c_float),             # unused by AC, but still takes up space
        ("penaltiesEnabled", c_int32),         # penalties on/off
        ("aidFuelRate", c_float),              # fuel consumption multiplier
        ("aidTireRate", c_float),              # tyre wear multiplier
        ("aidMechanicalDamage", c_float),      # mechanical damage setting
        ("aidAllowTyreBlankets", c_int32),     # tyre blankets allowed
        ("aidStability", c_float),             # stability control aid
        ("aidAutoClutch", c_int32),            # auto clutch aid
        ("aidAutoBlip", c_int32),              # auto blip aid
        ("hasDRS", c_int32),                   # car has DRS
        ("hasERS", c_int32),                   # car has ERS
        ("hasKERS", c_int32),                  # car has KERS
        ("kersMaxJ", c_float),                 # KERS max energy (J)
        ("engineBrakeSettingsCount", c_int32), # engine brake setting count
        ("ersPowerControllerCount", c_int32),  # ERS power controller count
        ("trackSPlineLength", c_float),        # track length in metres
        ("trackConfiguration", c_wchar * 15),  # layout name (e.g. a short or full variant)
    ]


# ---------------------------------------------------------------------------
# CONNECT TO AC'S SHARED MEMORY
# ---------------------------------------------------------------------------
# AC creates named memory blocks ("acpmf_physics", "acpmf_graphics",
# "acpmf_static"). mmap.mmap(...) opens them. ctypes.sizeof(...) tells it how
# many bytes to map.
phys_map = mmap.mmap(0, ctypes.sizeof(Physics), "acpmf_physics")
gfx_map = mmap.mmap(0, ctypes.sizeof(Graphics), "acpmf_graphics")
static_map = mmap.mmap(0, ctypes.sizeof(Static), "acpmf_static")

# from_buffer lays our blueprint OVER the live memory. Nothing is copied:
# `phys`, `gfx` and `static` are live views, so reading phys.speedKmh always
# returns the CURRENT value. We only need to do this once.
phys = Physics.from_buffer(phys_map)
gfx = Graphics.from_buffer(gfx_map)
static = Static.from_buffer(static_map)


# ---------------------------------------------------------------------------
# SNAPSHOT: read the live values once and return them as one row of data
# ---------------------------------------------------------------------------
def snapshot():
    """Return the current telemetry as a flat dictionary (= one row)."""
    t = time.time()  # AC doesn't give us a timestamp, so we add our own
    return {
        "t": t,                                  # wall-clock time of this sample

        # --- lap / position info (used later to split data into laps) ---
        "lap": gfx.completedLaps,                # lap counter
        "pos": gfx.normalizedCarPosition,        # 0..1 around the lap (key for comparing laps)
        "x": gfx.carCoordinates[0],              # world X (for the track map)
        "z": gfx.carCoordinates[2],              # world Z (for the track map; y is height, unused)

        # --- core driving data ---
        "speed": phys.speedKmh,
        "rpm": phys.rpms,
        "gear": phys.gear - 1,                   # shift so -1=reverse, 0=neutral, 1+=forward gears
        "gas": phys.gas,                         # throttle
        "brake": phys.brake,
        "clutch": phys.clutch,
        "steer": phys.steerAngle,
        "fuel": phys.fuel,

        # --- g-forces (the three entries of the accG array) ---
        "g_lat": phys.accG[0],
        "g_vert": phys.accG[1],
        "g_long": phys.accG[2],

        # --- per-wheel values, flattened into separate columns ---
        # Flat columns (tyre_temp_0 ... tyre_temp_3) are much easier to query
        # in SQL than arrays. Index: 0=FL, 1=FR, 2=RL, 3=RR.
        **{f"tyre_temp_{i}": phys.tyreCoreTemperature[i] for i in range(4)},
        **{f"tyre_psi_{i}": phys.wheelsPressure[i] for i in range(4)},
        **{f"brake_temp_{i}": phys.brakeTemp[i] for i in range(4)},

        # --- timing, in milliseconds ---
        "cur_time_ms": gfx.iCurrentTime,         # current lap time so far
        "last_time_ms": gfx.iLastTime,           # previous lap's time

        # --- lap validity flags (used by pipeline.py to flag cut laps / pit laps) ---
        "tyres_out": phys.numberOfTyresOut,      # how many tyres are off track (0-4)
        "in_pit": gfx.isInPit,                   # 1 while in the pit lane
    }


# ---------------------------------------------------------------------------
# METADATA: car / track info, written once per session
# ---------------------------------------------------------------------------
def write_metadata(out, hz):
    """Save car/track info once per session, as soon as the car is live."""
    meta = {
        "car": static.carModel,
        "track": static.track,
        "layout": static.trackConfiguration,
        "track_length_m": round(static.trackSPlineLength, 1),
        "ac_version": static.acVersion,
        "max_rpm": static.maxRpm,
        "max_fuel_l": round(static.maxFuel, 1),
        "sample_rate_hz": hz,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (out / "metadata.json").write_text(json.dumps(meta, indent=2))
    print(f"Session: {meta['car']} at {meta['track']} ({meta['layout']}), "
          f"{meta['track_length_m']} m")


# ---------------------------------------------------------------------------
# MAIN LOOP: sample at a steady rate and save in batches
# ---------------------------------------------------------------------------
def run(hz=60, flush_every=10.0):
    """
    hz:          samples per second
    flush_every: how often (seconds) to write buffered samples to disk
    """
    # One unique folder per session, e.g. sessions/20261116_201500_a3f9c1/
    out = BASE_DIR / "sessions" / f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    out.mkdir(parents=True)

    buf = []                   # rows waiting to be written to disk
    part = 0                   # numbers the output files: part_00000, part_00001, ...
    period = 1.0 / hz          # seconds between samples (1/60 = ~16.7 ms)
    next_tick = time.perf_counter()   # when the next sample is due (high-precision clock)
    last_flush = time.time()          # when we last wrote to disk
    meta_written = False              # becomes True once metadata.json is saved

    print(f"Logging to {out.resolve()} (Ctrl+C to stop)")
    try:
        while True:
            # Only record while actually driving (status 2 = live).
            # Skips menus, pause, and replays so we don't log junk.
            if gfx.status == 2:
                # Write the metadata once, at the first live sample. We wait
                # until now because the static block may still be empty while
                # you're in the menus.
                if not meta_written:
                    write_metadata(out, hz)
                    meta_written = True
                buf.append(snapshot())

            # --- timing ---
            # Schedule against fixed deadlines instead of sleeping 1/60 s each
            # loop. Plain sleeping drifts (each loop takes slightly longer than
            # planned); deadlines keep the average rate steady.
            next_tick += period
            while time.perf_counter() < next_tick:
                time.sleep(0.001)  # short sleep = precise timing without burning 100% CPU

            # --- batch write to disk ---
            # Writing 60x/sec would be wasteful and could stutter the sampling.
            # Instead: collect in memory, write a new part file every ~10 s.
            # A crash loses at most the last 10 seconds.
            if time.time() - last_flush > flush_every and buf:
                pq.write_table(pa.Table.from_pylist(buf), out / f"part_{part:05d}.parquet")
                buf, part, last_flush = [], part + 1, time.time()  # reset buffer, next file

    except KeyboardInterrupt:
        # Ctrl+C: save whatever is left in the buffer so the end of the
        # session isn't lost.
        if buf:
            pq.write_table(pa.Table.from_pylist(buf), out / f"part_{part:05d}.parquet")
        print("Saved.")


# Only run when executed directly (python collector.py), not when this file
# is imported by another script (e.g. the future live dashboard server,
# which will reuse snapshot()).
if __name__ == "__main__":
    run()
