"""
pipeline.py - DuckDB views over all recorded sessions
=====================================================

Every analysis script starts with:

    from pipeline import get_connection
    con = get_connection()

and can then query these views with plain SQL:

    samples       every recorded sample, tagged with the session it came from
    laps          one row per (session, lap) with data-quality flags
    session_meta  car / track / layout per session (from metadata.json)

LAP FLAGS (built on each other)
    is_clean            complete recording of a full lap: it covers the whole
                        track, has a lap time, and the recorded duration matches
                        the game's own lap time (so no gaps in the data)
    is_valid            clean AND no sample with TYRES_OUT_LIMIT or more tyres off
                        track (no cuts)
    is_representative   valid AND within PACE_FACTOR (107%) of the session's best
                        valid lap, which removes spins and crawling laps

WHERE THE DATA IS
    sessions/ next to this file by default. To analyse another folder, e.g. the
    sample data:   $env:AC_SESSIONS_DIR = "sample_data"      (PowerShell)
                   export AC_SESSIONS_DIR=sample_data        (macOS / Linux)
"""

import os
from pathlib import Path

import duckdb

BASE = Path(__file__).resolve().parent
SESSIONS_DIR = Path(os.environ.get("AC_SESSIONS_DIR", BASE / "sessions")).resolve()

TYRES_OUT_LIMIT = 3    # a sample with this many tyres off track marks the lap as cut
PACE_FACTOR = 1.07     # race-style 107% rule vs the session's best valid lap

# The folder name of a file (= the session id), e.g. .../20261118_115218_89be0a/part_00000.parquet
_SESSION_ID_REGEX = "([^/]+)/[^/]+$"


def get_connection(sessions_dir=None):
    """Return a DuckDB connection with the `samples`, `laps` and `session_meta` views."""
    root = Path(sessions_dir).resolve() if sessions_dir else SESSIONS_DIR
    if not any(root.glob("*/part_*.parquet")):
        raise SystemExit(
            f"No recorded sessions found in {root}\n"
            "Record one with collector.py, or point AC_SESSIONS_DIR at a folder of sessions "
            "(for example sample_data)."
        )

    glob = str(root / "*" / "part_*.parquet").replace("\\", "/")
    meta_glob = str(root / "*" / "metadata.json").replace("\\", "/")

    con = duckdb.connect()

    # Every sample, tagged with its session. union_by_name lets old sessions
    # (recorded before a column existed) and new ones be read together.
    con.sql(f"""
        CREATE OR REPLACE VIEW samples AS
        SELECT regexp_extract(filename, '{_SESSION_ID_REGEX}', 1) AS session_id,
               * EXCLUDE (filename)
        FROM read_parquet('{glob}', filename=true, union_by_name=true)
    """)

    # One row per (session, lap), with the data-quality flags described above.
    con.sql(f"""
        CREATE OR REPLACE VIEW laps AS
        WITH per_lap AS (
            SELECT session_id, lap,
                   count(*)                       AS n_rows,
                   min(t)                         AS start_t,
                   max(t) - min(t)                AS wall_s,
                   min(pos)                       AS pos_min,
                   max(pos)                       AS pos_max,
                   max(speed)                     AS max_speed,
                   max(tyres_out)                 AS max_tyres_out,
                   -- NULL (not 0) for old sessions that never recorded tyres_out
                   CASE WHEN max(tyres_out) IS NULL THEN NULL
                        ELSE count(*) FILTER (WHERE tyres_out >= {TYRES_OUT_LIMIT})
                   END                            AS rows_cut,
                   max(in_pit)                    AS max_in_pit,
                   -- AC reports the previous lap's time once a new lap starts
                   first(last_time_ms ORDER BY t) AS prev_lap_time_ms
            FROM samples
            GROUP BY session_id, lap
        ),
        timed AS (
            SELECT *,
                   -- this lap's time = what the NEXT lap reports as "last lap"
                   lead(prev_lap_time_ms)
                       OVER (PARTITION BY session_id ORDER BY lap) / 1000.0 AS lap_time_s
            FROM per_lap
        ),
        flagged AS (
            SELECT *,
                   coalesce(pos_min < 0.02 AND pos_max > 0.98
                            AND lap_time_s > 0
                            AND abs(wall_s - lap_time_s) < 3, false) AS is_clean
            FROM timed
        ),
        validated AS (
            SELECT *, (is_clean AND coalesce(rows_cut, 0) = 0) AS is_valid
            FROM flagged
        )
        SELECT *,
               (is_valid AND lap_time_s <= {PACE_FACTOR} *
                    min(CASE WHEN is_valid THEN lap_time_s END)
                        OVER (PARTITION BY session_id)) AS is_representative
        FROM validated
    """)

    # Session metadata (older sessions have none, so guard for missing files)
    if any(root.glob("*/metadata.json")):
        con.sql(f"""
            CREATE OR REPLACE VIEW session_meta AS
            SELECT regexp_extract(filename, '{_SESSION_ID_REGEX}', 1) AS session_id,
                   car, track, layout, track_length_m
            FROM read_json_auto('{meta_glob}', filename=true, union_by_name=true)
        """)
    else:
        con.sql("""
            CREATE OR REPLACE VIEW session_meta AS
            SELECT NULL::VARCHAR AS session_id, NULL::VARCHAR AS car,
                   NULL::VARCHAR AS track, NULL::VARCHAR AS layout,
                   NULL::DOUBLE AS track_length_m
            WHERE false
        """)
    return con
