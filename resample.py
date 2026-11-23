"""
resample.py - puts every representative lap onto a common track-position grid
=============================================================================

WHY
    Two laps can't be compared sample by sample: they have different lengths in
    time. Instead each lap is interpolated onto 1000 equally spaced points
    around the track (normalised track position 0 to 1), so point 500 is the
    same piece of road on every lap.

USAGE
    python resample.py
OUTPUT
    processed/resampled_laps.parquet   (one row per lap per grid point)
"""

import numpy as np
import pandas as pd

from pipeline import BASE, get_connection

N_POINTS = 1000
GRID = np.linspace(0, 1, N_POINTS)           # 0 = start line, 1 = back at the start line
CHANNELS = ["speed", "gas", "brake", "steer", "gear", "rpm",
            "g_lat", "g_long", "cur_time_ms"]

con = get_connection()
laps = con.sql("""
    SELECT session_id, lap, lap_time_s
    FROM laps WHERE is_representative ORDER BY session_id, lap
""").df()

frames = []
for row in laps.itertuples():
    lap_df = con.execute(
        f"SELECT pos, {', '.join(CHANNELS)} FROM samples "
        "WHERE session_id = ? AND lap = ? ORDER BY t",
        [row.session_id, row.lap],
    ).df()

    # Track position must be strictly increasing to interpolate:
    # force it non-decreasing, then drop duplicate positions.
    pos = np.maximum.accumulate(lap_df["pos"].to_numpy())
    pos, idx = np.unique(pos, return_index=True)

    out = {"session_id": row.session_id, "lap": row.lap,
           "lap_time_s": row.lap_time_s, "pos": GRID}
    for ch in CHANNELS:
        out[ch] = np.interp(GRID, pos, lap_df[ch].to_numpy()[idx])
    out["gear"] = np.round(out["gear"])      # gears are integers, don't blend them
    frames.append(pd.DataFrame(out))

if not frames:
    raise SystemExit("No representative laps found. Record a few full laps first.")

result = pd.concat(frames, ignore_index=True)
out_dir = BASE / "processed"
out_dir.mkdir(exist_ok=True)
result.to_parquet(out_dir / "resampled_laps.parquet", index=False)

print(f"Wrote {len(frames)} lap(s) x {N_POINTS} points to {out_dir}")
print(laps.to_string())
