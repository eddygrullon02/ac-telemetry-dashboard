"""
segments.py - where is the time lost? Splits the lap into equal segments
========================================================================

USAGE
    python segments.py                  # the session with the most usable laps
    python segments.py <session_id>     # a specific session (see laps.py)

For every lap except the fastest one, prints how many seconds it gained or lost
against the fastest lap in each of 20 equal stretches of track, and the 5 worst
segments. The per-segment losses add up to the lap-time difference.

Run resample.py first.
"""

import sys

import numpy as np
import pandas as pd

from pipeline import BASE

SEGMENTS = 20

df = pd.read_parquet(BASE / "processed" / "resampled_laps.parquet")
session = sys.argv[1] if len(sys.argv) > 1 else df.groupby("session_id")["lap"].nunique().idxmax()
s = df[df.session_id == session]
if s.empty:
    raise SystemExit(f"Session {session} not found in processed/resampled_laps.parquet")

lap_times = s.groupby("lap")["lap_time_s"].first().sort_values()
if len(lap_times) < 2:
    raise SystemExit("Need at least 2 representative laps in one session to compare.")

best = lap_times.index[0]
n = len(s[s.lap == best])
edges = np.linspace(0, n - 1, SEGMENTS + 1).round().astype(int)


def segment_times(g):
    t = g["cur_time_ms"].to_numpy() / 1000.0
    return np.diff(t[edges])            # seconds spent in each segment


ref = segment_times(s[s.lap == best].reset_index(drop=True))

rows = []
for lap, g in s.groupby("lap"):
    if lap == best:
        continue
    seg = segment_times(g.reset_index(drop=True))
    for i in range(SEGMENTS):
        rows.append({
            "lap": lap,
            "segment": i + 1,
            "track_pos": f"{edges[i] / (n - 1):.2f}-{edges[i + 1] / (n - 1):.2f}",
            "ref_s": round(ref[i], 3),
            "lap_s": round(seg[i], 3),
            "loss_s": round(seg[i] - ref[i], 3),
        })

out = pd.DataFrame(rows)
print(f"Session {session}: reference lap {best} ({lap_times[best]:.3f}s)")
print(out.to_string(index=False))
print("\nWorst 5 segments:")
print(out.sort_values("loss_s", ascending=False).head(5).to_string(index=False))
