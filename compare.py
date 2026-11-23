"""
compare.py - overlays the laps of one session and plots the time delta
======================================================================

USAGE
    python compare.py                  # the session with the most usable laps
    python compare.py <session_id>     # a specific session (see laps.py)

Opens an interactive chart in your browser (also saved to processed/lap_compare.html):
speed, throttle, brake, and the running time delta against the session's fastest lap.
Where the delta line slopes upward, that lap is losing time to the best lap.

Run resample.py first.
"""

import sys

import pandas as pd
import plotly.graph_objects as go
from plotly.colors import qualitative
from plotly.subplots import make_subplots

from pipeline import BASE

df = pd.read_parquet(BASE / "processed" / "resampled_laps.parquet")

session = sys.argv[1] if len(sys.argv) > 1 else df.groupby("session_id")["lap"].nunique().idxmax()
s = df[df.session_id == session]
if s.empty:
    raise SystemExit(f"Session {session} not found in processed/resampled_laps.parquet")

lap_times = s.groupby("lap")["lap_time_s"].first().sort_values()
if len(lap_times) < 2:
    raise SystemExit("Need at least 2 representative laps in one session to compare.")

best_lap = lap_times.index[0]
ref = s[s.lap == best_lap].reset_index(drop=True)

fig = make_subplots(
    rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.03,
    subplot_titles=("Speed (km/h)", "Throttle", "Brake",
                    f"Delta vs lap {best_lap} (s, positive = slower)"),
)

for i, (lap, g) in enumerate(s.groupby("lap")):
    color = qualitative.Plotly[i % len(qualitative.Plotly)]
    name = f"Lap {lap} ({lap_times[lap]:.3f}s)"
    g = g.reset_index(drop=True)
    delta = (g["cur_time_ms"] - ref["cur_time_ms"]) / 1000.0   # game lap timer vs best lap
    series = [g["speed"], g["gas"], g["brake"], delta]
    for row, y in enumerate(series, start=1):
        fig.add_trace(
            go.Scatter(x=g["pos"], y=y, name=name, legendgroup=name,
                       showlegend=(row == 1), mode="lines",
                       line=dict(color=color, width=1.5)),
            row=row, col=1,
        )

fig.update_xaxes(title_text="Track position (0 = start/finish line)", row=4, col=1)
fig.update_layout(height=900, title=f"Lap comparison, session {session}")

out = BASE / "processed" / "lap_compare.html"
fig.write_html(out, auto_open=True)
print(f"Wrote {out}")
