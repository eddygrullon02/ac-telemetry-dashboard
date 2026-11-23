"""
sources.py - where the dashboard's telemetry comes from
=======================================================

The dashboard page doesn't care whether the numbers come from the running game
or from a recording. Both sources below offer the same two things:

    source.meta()            car / track info for the page header
    source.stream(hz)        an async generator that yields one telemetry
                             dict (one "row") about hz times per second

LiveSource    reads Assetto Corsa's shared memory (Windows, game running).
ReplaySource  plays a recorded session back in real time, in a loop. It works
              anywhere (no game, no Windows needed), so anyone can try the
              dashboard with the sample data.
"""

import asyncio
import json
import math
import time
from pathlib import Path


class LiveSource:
    """Reads the running game's shared memory."""

    name = "live"

    def __init__(self):
        # Importing collector opens AC's shared memory, so this fails with an
        # error if the game isn't running yet (or on a non-Windows machine).
        import collector
        self._c = collector

    def meta(self):
        s = self._c.static          # read live: the static block is empty until a session starts
        return {
            "car": s.carModel,
            "track": s.track,
            "layout": s.trackConfiguration,
            "track_length_m": round(s.trackSPlineLength, 1),
            "max_rpm": s.maxRpm,
            "max_fuel_l": round(s.maxFuel, 1),
        }

    async def stream(self, hz):
        c = self._c
        period = 1.0 / hz
        while True:
            row = c.snapshot()                       # same dict the collector records
            row["status"] = c.gfx.status             # 2 = live, anything else = menu/pause/replay
            row["best_time_ms"] = c.gfx.iBestTime    # best lap time, for the timing panel
            yield row
            await asyncio.sleep(period)


class ReplaySource:
    """Plays a recorded session back at real speed, looping forever."""

    name = "replay"
    MAX_STEP_S = 0.1      # pauses in the recording (menus, paused game) are squeezed to this

    def __init__(self, df, info=None, label="replay"):
        import numpy as np

        if len(df) == 0:
            raise SystemExit("The recording is empty, nothing to replay.")
        df = df.sort_values("t").reset_index(drop=True)

        # Older sessions were recorded before these columns existed.
        for col in ("tyres_out", "in_pit"):
            if col not in df.columns:
                df[col] = 0
        df = df.fillna(0)                            # NaN is not valid JSON

        # Running best lap so far, from the game's own "last lap time" column.
        last = df["last_time_ms"].where(df["last_time_ms"] > 0)
        df["best_time_ms"] = last.cummin().fillna(0)
        df["status"] = 2                             # replay is always "live"

        # Seconds since the start of the recording, with long pauses squeezed.
        t = df["t"].to_numpy(dtype=float)
        step = np.clip(np.diff(t, prepend=t[0]), 0, self.MAX_STEP_S)
        self._rel_t = np.cumsum(step)
        self._rows = df.to_dict("records")

        max_rpm = int(max(1000, math.ceil(float(df["rpm"].max()) / 1000) * 1000))
        self._meta = {
            "car": "recorded session",
            "track": label,
            "layout": "",
            "track_length_m": 0,
            "max_rpm": max_rpm,
            "max_fuel_l": 0,
        }
        if info:                                     # metadata.json from the collector
            self._meta.update({
                "car": info.get("car", self._meta["car"]),
                "track": info.get("track", label),
                "layout": info.get("layout", ""),
                "track_length_m": info.get("track_length_m", 0),
                "max_rpm": info.get("max_rpm") or max_rpm,
                "max_fuel_l": info.get("max_fuel_l", 0),
            })

    @classmethod
    def from_path(cls, path):
        """Load a session folder (part_*.parquet files), or the best session inside a folder of sessions."""
        import pandas as pd

        p = Path(path)
        if not p.exists():
            raise SystemExit(f"Replay path not found: {p}")

        if p.is_dir() and not list(p.glob("part_*.parquet")):
            subs = [d for d in p.iterdir() if d.is_dir() and any(d.glob("part_*.parquet"))]
            if not subs:
                raise SystemExit(f"No recorded sessions (part_*.parquet) found in {p}")
            p = max(subs, key=lambda d: sum(f.stat().st_size for f in d.glob("part_*.parquet")))
            print(f"Replaying the largest session found: {p.name}")

        files = sorted(p.glob("part_*.parquet")) if p.is_dir() else [p]
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)

        meta_file = (p if p.is_dir() else p.parent) / "metadata.json"
        info = json.loads(meta_file.read_text()) if meta_file.exists() else None
        return cls(df, info, label=p.name if p.is_dir() else p.stem)

    def meta(self):
        return self._meta

    async def stream(self, hz):
        period = 1.0 / hz
        n = len(self._rows)
        while True:                                  # loop the recording forever
            start = time.perf_counter()
            i = 0
            while True:
                elapsed = time.perf_counter() - start
                while i < n - 1 and self._rel_t[i + 1] <= elapsed:
                    i += 1                           # skip ahead to where the recording "is" now
                yield self._rows[i]
                if i >= n - 1:
                    break                            # reached the end; start over
                await asyncio.sleep(period)
