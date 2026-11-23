"""
check_session.py - data-quality report for every recorded session
=================================================================

USAGE
    python check_session.py

Per session it reports:
    rows            number of samples
    median_hz       typical sample rate (should be close to 60)
    p99_gap_ms      99% of samples arrive within this many milliseconds of the previous one
    gaps_over_1s    pauses in the recording (menus, paused game)
and then the value ranges of the main channels, which makes it easy to spot a
misaligned struct field (e.g. gas outside 0-1, speed stuck at 0).
"""

from pipeline import get_connection

con = get_connection()

print("\nSampling quality per session")
print(con.sql("""
    WITH d AS (
        SELECT session_id,
               t - lag(t) OVER (PARTITION BY session_id ORDER BY t) AS dt
        FROM samples
    )
    SELECT session_id,
           count(*)                                AS rows,
           round(1 / median(dt), 1)                AS median_hz,
           round(quantile_cont(dt, 0.99) * 1000, 1) AS p99_gap_ms,
           count(*) FILTER (WHERE dt > 1)          AS gaps_over_1s
    FROM d
    GROUP BY session_id
    ORDER BY session_id
""").df().to_string())

print("\nValue ranges per session")
print(con.sql("""
    SELECT session_id,
           round(max(speed), 1)        AS max_speed,
           round(min(gas), 2)          AS gas_min,   round(max(gas), 2)   AS gas_max,
           round(min(brake), 2)        AS brake_min, round(max(brake), 2) AS brake_max,
           min(gear)                   AS gear_min,  max(gear)            AS gear_max,
           round(min(fuel), 1)         AS fuel_min,  round(max(fuel), 1)  AS fuel_max
    FROM samples
    GROUP BY session_id
    ORDER BY session_id
""").df().to_string())
