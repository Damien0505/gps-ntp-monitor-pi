"""
db.py — SQLite schema and helper functions for GPS/NTP monitor
"""

import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get("MONITOR_DB", os.path.join(os.path.dirname(__file__), "monitor.db"))


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=134217728")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chrony_tracking (
                ts              INTEGER NOT NULL,
                ref_id          TEXT,
                stratum         INTEGER,
                ref_time        REAL,
                sys_time_offset REAL,   -- seconds (positive = fast)
                last_offset     REAL,   -- seconds
                rms_offset      REAL,   -- seconds
                freq_offset     REAL,   -- ppm
                residual_freq   REAL,   -- ppm
                skew            REAL,   -- ppm
                root_delay      REAL,   -- seconds
                root_dispersion REAL,   -- seconds
                update_interval REAL,   -- seconds
                leap_status     TEXT
            );

            CREATE TABLE IF NOT EXISTS chrony_sources (
                ts          INTEGER NOT NULL,
                name        TEXT NOT NULL,
                mode        TEXT,    -- '^' server, '=' peer, '#' refclock
                state       TEXT,    -- '*' synced, '+' ok, '-' not used, '?' unreachable
                stratum     INTEGER,
                poll        INTEGER,
                reach       INTEGER, -- octal reachability register
                last_rx     INTEGER, -- seconds ago
                offset      REAL,    -- seconds
                err_bound   REAL     -- seconds
            );

            CREATE TABLE IF NOT EXISTS chrony_sourcestats (
                ts             INTEGER NOT NULL,
                name           TEXT NOT NULL,
                samples        INTEGER,
                residual_freq  REAL,    -- ppm
                skew           REAL,    -- ppm
                std_dev        REAL,    -- seconds
                est_offset     REAL,    -- seconds
                offset_sd      REAL     -- seconds
            );

            CREATE TABLE IF NOT EXISTS gpsd (
                ts          INTEGER NOT NULL,
                fix_mode    INTEGER,    -- 1=no fix, 2=2D, 3=3D
                latitude    REAL,
                longitude   REAL,
                altitude    REAL,       -- metres MSL
                speed       REAL,       -- m/s
                track       REAL,       -- degrees true
                climb       REAL,       -- m/s
                hdop        REAL,
                vdop        REAL,
                tdop        REAL,
                pdop        REAL,
                gdop        REAL,
                sats_used   INTEGER,
                sats_seen   INTEGER,
                time_err    REAL,       -- seconds (gpsd ept)
                leap_secs   INTEGER
            );

            CREATE TABLE IF NOT EXISTS system_metrics (
                ts          INTEGER NOT NULL,
                cpu_temp    REAL,       -- degrees C
                cpu_freq    REAL,       -- MHz
                load_1m     REAL,
                load_5m     REAL,
                load_15m    REAL,
                mem_total   INTEGER,    -- kB
                mem_used    INTEGER,    -- kB
                mem_free    INTEGER,    -- kB
                disk_used   INTEGER,    -- MB (of filesystem containing DB)
                disk_free   INTEGER     -- MB
            );

            CREATE TABLE IF NOT EXISTS events (
                ts      INTEGER NOT NULL,
                source  TEXT NOT NULL,  -- 'chrony', 'gpsd', 'system', 'collector'
                level   TEXT NOT NULL,  -- 'info', 'warn', 'error'
                msg     TEXT NOT NULL
            );

            -- Indexes for time-range queries
            CREATE INDEX IF NOT EXISTS idx_ct_ts   ON chrony_tracking(ts);
            CREATE INDEX IF NOT EXISTS idx_cs_ts   ON chrony_sources(ts);
            CREATE INDEX IF NOT EXISTS idx_css_ts  ON chrony_sourcestats(ts);
            CREATE INDEX IF NOT EXISTS idx_gps_ts  ON gpsd(ts);
            CREATE INDEX IF NOT EXISTS idx_sys_ts  ON system_metrics(ts);
            CREATE INDEX IF NOT EXISTS idx_evt_ts  ON events(ts);
        """)


def now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def insert_chrony_tracking(data: dict):
    sql = """INSERT INTO chrony_tracking
             (ts, ref_id, stratum, ref_time, sys_time_offset, last_offset,
              rms_offset, freq_offset, residual_freq, skew, root_delay,
              root_dispersion, update_interval, leap_status)
             VALUES (:ts,:ref_id,:stratum,:ref_time,:sys_time_offset,:last_offset,
                     :rms_offset,:freq_offset,:residual_freq,:skew,:root_delay,
                     :root_dispersion,:update_interval,:leap_status)"""
    with get_conn() as conn:
        conn.execute(sql, {**{"ts": now_ts()}, **data})


def insert_chrony_sources(rows: list):
    sql = """INSERT INTO chrony_sources
             (ts, name, mode, state, stratum, poll, reach, last_rx, offset, err_bound)
             VALUES (?,?,?,?,?,?,?,?,?,?)"""
    ts = now_ts()
    with get_conn() as conn:
        conn.executemany(sql, [(ts, r["name"], r["mode"], r["state"], r["stratum"],
                                r["poll"], r["reach"], r["last_rx"], r["offset"], r["err_bound"])
                               for r in rows])


def insert_chrony_sourcestats(rows: list):
    sql = """INSERT INTO chrony_sourcestats
             (ts, name, samples, residual_freq, skew, std_dev, est_offset, offset_sd)
             VALUES (?,?,?,?,?,?,?,?)"""
    ts = now_ts()
    with get_conn() as conn:
        conn.executemany(sql, [(ts, r["name"], r["samples"], r["residual_freq"],
                                r["skew"], r["std_dev"], r["est_offset"], r["offset_sd"])
                               for r in rows])


def insert_gpsd(data: dict):
    sql = """INSERT INTO gpsd
             (ts, fix_mode, latitude, longitude, altitude, speed, track, climb,
              hdop, vdop, tdop, pdop, gdop, sats_used, sats_seen, time_err, leap_secs)
             VALUES (:ts,:fix_mode,:latitude,:longitude,:altitude,:speed,:track,:climb,
                     :hdop,:vdop,:tdop,:pdop,:gdop,:sats_used,:sats_seen,:time_err,:leap_secs)"""
    with get_conn() as conn:
        conn.execute(sql, {**{"ts": now_ts()}, **data})


def insert_system_metrics(data: dict):
    sql = """INSERT INTO system_metrics
             (ts, cpu_temp, cpu_freq, load_1m, load_5m, load_15m,
              mem_total, mem_used, mem_free, disk_used, disk_free)
             VALUES (:ts,:cpu_temp,:cpu_freq,:load_1m,:load_5m,:load_15m,
                     :mem_total,:mem_used,:mem_free,:disk_used,:disk_free)"""
    with get_conn() as conn:
        conn.execute(sql, {**{"ts": now_ts()}, **data})


def insert_event(source: str, level: str, msg: str):
    with get_conn() as conn:
        conn.execute("INSERT INTO events (ts,source,level,msg) VALUES (?,?,?,?)",
                     (now_ts(), source, level, msg))


def query_range(table: str, start_ts: int, end_ts: int, columns="*", where_extra=""):
    """Generic time-range query with optional column selection."""
    sql = f"SELECT {columns} FROM {table} WHERE ts BETWEEN ? AND ? {where_extra} ORDER BY ts ASC"
    with get_conn() as conn:
        rows = conn.execute(sql, (start_ts, end_ts)).fetchall()
    return [dict(r) for r in rows]


def query_bucketed(table: str, start_ts: int, end_ts: int, bucket_secs: int, agg_cols: list):
    """
    Returns time-bucketed averages. agg_cols is a list of column names to average.
    Returns list of dicts with 'bucket' (timestamp) and each avg column.
    """
    agg_expr = ", ".join(f"AVG({c}) AS {c}" for c in agg_cols)
    sql = f"""
        SELECT (ts / {bucket_secs}) * {bucket_secs} AS bucket, {agg_expr}
        FROM {table}
        WHERE ts BETWEEN ? AND ?
        GROUP BY bucket
        ORDER BY bucket ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (start_ts, end_ts)).fetchall()
    return [dict(r) for r in rows]


def prune_old_data(days_full: int = 7, days_hourly: int = 90):
    """
    Retention policy:
      - Keep full resolution for last `days_full` days
      - Keep hourly averages (via VIEW / separate table) up to `days_hourly`
      - Delete everything older than `days_hourly`
    Simple implementation: just delete rows older than days_hourly days.
    For production, extend to materialise hourly summaries before deletion.
    """
    cutoff = now_ts() - (days_hourly * 86400)
    tables = ["chrony_tracking", "chrony_sources", "chrony_sourcestats", "gpsd", "system_metrics"]
    with get_conn() as conn:
        for t in tables:
            conn.execute(f"DELETE FROM {t} WHERE ts < ?", (cutoff,))
    insert_event("system", "info", f"Pruned data older than {days_hourly} days")


def db_size_mb():
    try:
        return round(os.path.getsize(DB_PATH) / (1024 * 1024), 2)
    except FileNotFoundError:
        return 0


if __name__ == "__main__":
    init_db()
    print(f"Database initialised at {DB_PATH}")
    print(f"Current size: {db_size_mb()} MB")
