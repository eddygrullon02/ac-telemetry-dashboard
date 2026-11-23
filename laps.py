"""
laps.py - lists every recorded lap with its data-quality flags
==============================================================

USAGE
    python laps.py

Columns: lap_time_s comes from the game's own lap timer. rows_cut counts samples
with 3+ tyres off track (blank = that session predates the column). The flags
are explained in pipeline.py: is_clean, is_valid, is_representative.
"""

from pipeline import get_connection

con = get_connection()
print(con.sql("""
    SELECT l.session_id, m.track, m.layout, m.car, l.lap, l.n_rows,
           round(l.lap_time_s, 3) AS lap_time_s,
           l.rows_cut, l.is_clean, l.is_valid, l.is_representative
    FROM laps l
    LEFT JOIN session_meta m USING (session_id)
    ORDER BY l.session_id, l.lap
""").df().to_string())
