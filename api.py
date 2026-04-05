#!/usr/bin/env python3
"""
api.py — Lightweight Flask API for the GPS/NTP monitor dashboard.

Endpoints:
  GET /api/status          — latest single-row snapshot of everything
  GET /api/chrony/tracking — time-series for chrony tracking metrics
  GET /api/chrony/sources  — time-series for chrony sources
  GET /api/chrony/sourcestats — time-series for sourcestats
  GET /api/gpsd            — time-series for GPSD metrics
  GET /api/system          — time-series for system metrics
  GET /api/events          — recent events log

Query parameters for time-series endpoints:
  range   = day | week | month | year  (default: day)
  source  = <name>  filter sources/sourcestats by source name (optional)
  raw     = 1       return raw rows (no bucketing) — only valid for range=day

Run:  python3 api.py
      gunicorn -w 1 -b 0.0.0.0:5000 api:app   (production)
"""

import os
import time
import socket
import threading
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_from_directory
import db

app = Flask(__name__, static_folder="static")

# ---------------------------------------------------------------------------
# DNS resolution cache
# Resolves IP addresses to hostnames in a background thread so API responses
# are never blocked. Cache entries expire after 1 hour.
# ---------------------------------------------------------------------------

_dns_cache = {}          # {ip: (hostname, expiry_timestamp)}
_dns_lock = threading.Lock()
_DNS_TTL = 3600          # seconds before re-resolving


def resolve_hostname(ip):
    """Return hostname for ip, or ip itself if resolution fails.
    Non-blocking: returns cached value immediately, triggers background
    refresh if the entry is missing or expired."""
    now = time.time()
    with _dns_lock:
        entry = _dns_cache.get(ip)
        if entry and now < entry[1]:
            return entry[0]          # cache hit, still fresh

    # Cache miss or expired — resolve in background, return ip for now
    threading.Thread(target=_resolve_worker, args=(ip,), daemon=True).start()
    # Return stale value while refresh is in flight, or the raw IP first time
    return entry[0] if entry else ip


def _resolve_worker(ip):
    """Background DNS lookup — updates cache when done."""
    try:
        hostname = socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError):
        hostname = ip   # store ip as its own hostname so we don't retry until TTL
    with _dns_lock:
        _dns_cache[ip] = (hostname, time.time() + _DNS_TTL)


def resolve_names(source_names):
    """Given a list of source names, return a dict {name: display_name}.
    Refclock names (GPS, PPS, NMEA) are returned as-is."""
    result = {}
    for name in source_names:
        # Only try to resolve things that look like IP addresses
        try:
            socket.inet_aton(name)          # raises if not a valid IPv4
            result[name] = resolve_hostname(name)
        except OSError:
            result[name] = name             # refclock name or hostname already
    return result

# ---------------------------------------------------------------------------
# Range → seconds and bucket size
# ---------------------------------------------------------------------------

RANGE_CONFIG = {
    #         seconds back  bucket secs
    "day":   (86400,        60),
    "week":  (604800,       900),
    "month": (2592000,      3600),
    "year":  (31536000,     86400),
}


def get_range_params():
    r = request.args.get("range", "day")
    if r not in RANGE_CONFIG:
        r = "day"
    secs_back, bucket = RANGE_CONFIG[r]
    now = int(time.time())
    start = now - secs_back
    return start, now, bucket, r


def ts_to_iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def add_iso(rows, key="ts"):
    for row in rows:
        if key in row and row[key] is not None:
            row["iso"] = ts_to_iso(int(row[key]))
    return rows



# ---------------------------------------------------------------------------
# /api/status  — latest snapshot
# ---------------------------------------------------------------------------

@app.route("/api/status")
def status():
    result = {}

    with db.get_conn() as conn:
        def latest(table):
            row = conn.execute(
                f"SELECT * FROM {table} ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else {}

        result["chrony_tracking"]  = latest("chrony_tracking")
        result["gpsd"]             = latest("gpsd")
        result["system"]           = latest("system_metrics")
        result["db_size_mb"]       = db.db_size_mb()

        # Latest PPS source from sources
        pps = conn.execute(
            "SELECT * FROM chrony_sources WHERE name LIKE '%PPS%' OR name='NMEA' "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        result["pps_source"] = dict(pps) if pps else {}

    # Add ISO timestamps
    for key in ("chrony_tracking", "gpsd", "system"):
        if result[key].get("ts"):
            result[key]["iso"] = ts_to_iso(result[key]["ts"])

    result["server_time"] = ts_to_iso(int(time.time()))
    return jsonify(result)


# ---------------------------------------------------------------------------
# /api/chrony/tracking
# ---------------------------------------------------------------------------

@app.route("/api/chrony/tracking")
def chrony_tracking():
    start, end, bucket, r = get_range_params()
    cols = ["sys_time_offset", "rms_offset", "freq_offset", "residual_freq",
            "skew", "root_delay", "root_dispersion", "update_interval", "stratum"]

    rows = db.query_bucketed("chrony_tracking", start, end, bucket, cols)
    add_iso(rows, "bucket")
    return jsonify({"range": r, "bucket_secs": bucket, "rows": rows})


# ---------------------------------------------------------------------------
# /api/chrony/sources
# ---------------------------------------------------------------------------

@app.route("/api/chrony/sources")
def chrony_sources():
    start, end, bucket, r = get_range_params()
    source = request.args.get("source")

    where = f"AND name = '{source}'" if source else ""
    cols = ["offset", "err_bound", "reach"]

    if source:
        rows = db.query_bucketed("chrony_sources", start, end, bucket, cols)
    else:
        # Return distinct source names available
        with db.get_conn() as conn:
            names = conn.execute(
                "SELECT DISTINCT name FROM chrony_sources "
                "WHERE ts >= ? ORDER BY name",
                (int(time.time()) - 86400,)
            ).fetchall()
        all_names = [r2["name"] for r2 in names]
        # PPS and GPS always first, then remaining sources alphabetically
        priority = ["PPS", "GPS"]
        source_names = [n for n in priority if n in all_names] +                        [n for n in all_names if n not in priority]

        rows_by_source = {}
        for name in source_names:
            where_clause = f"AND name = ?"
            sql = f"""
                SELECT (ts / {bucket}) * {bucket} AS bucket,
                       AVG(offset) AS offset, AVG(err_bound) AS err_bound,
                       AVG(reach) AS reach
                FROM chrony_sources
                WHERE ts BETWEEN ? AND ? AND name = ?
                GROUP BY bucket ORDER BY bucket ASC
            """
            with db.get_conn() as conn:
                result = conn.execute(sql, (start, end, name)).fetchall()
            rows_by_source[name] = add_iso([dict(r2) for r2 in result])

        hostnames = resolve_names(source_names)
        return jsonify({
            "range": r, "bucket_secs": bucket,
            "sources": source_names,
            "hostnames": hostnames,
            "rows_by_source": rows_by_source
        })

    add_iso(rows, "bucket")
    return jsonify({"range": r, "bucket_secs": bucket, "source": source, "rows": rows})


# ---------------------------------------------------------------------------
# /api/chrony/sourcestats
# ---------------------------------------------------------------------------

@app.route("/api/chrony/sourcestats")
def chrony_sourcestats():
    start, end, bucket, r = get_range_params()
    cols = ["residual_freq", "skew", "std_dev", "est_offset", "offset_sd", "samples"]

    with db.get_conn() as conn:
        names = conn.execute(
            "SELECT DISTINCT name FROM chrony_sourcestats "
            "WHERE ts >= ? ORDER BY name",
            (int(time.time()) - 86400,)
        ).fetchall()
    all_names = [row["name"] for row in names]
    # PPS and GPS always first, then remaining sources alphabetically
    priority = ["PPS", "GPS"]
    source_names = [n for n in priority if n in all_names] +                    [n for n in all_names if n not in priority]

    rows_by_source = {}
    for name in source_names:
        sql = f"""
            SELECT (ts / {bucket}) * {bucket} AS bucket,
                   AVG(residual_freq) AS residual_freq, AVG(skew) AS skew,
                   AVG(std_dev) AS std_dev, AVG(est_offset) AS est_offset,
                   AVG(offset_sd) AS offset_sd, AVG(samples) AS samples
            FROM chrony_sourcestats
            WHERE ts BETWEEN ? AND ? AND name = ?
            GROUP BY bucket ORDER BY bucket ASC
        """
        with db.get_conn() as conn:
            result = conn.execute(sql, (start, end, name)).fetchall()
        rows_by_source[name] = add_iso([dict(r2) for r2 in result])

    hostnames = resolve_names(source_names)
    return jsonify({
        "range": r, "bucket_secs": bucket,
        "sources": source_names,
        "hostnames": hostnames,
        "rows_by_source": rows_by_source
    })


# ---------------------------------------------------------------------------
# /api/gpsd
# ---------------------------------------------------------------------------

@app.route("/api/gpsd")
def gpsd():
    start, end, bucket, r = get_range_params()
    cols = ["fix_mode", "altitude", "speed", "hdop", "vdop", "tdop", "pdop",
            "sats_used", "sats_seen", "time_err"]

    rows = db.query_bucketed("gpsd", start, end, bucket, cols)
    add_iso(rows, "bucket")
    return jsonify({"range": r, "bucket_secs": bucket, "rows": rows})


# ---------------------------------------------------------------------------
# /api/system
# ---------------------------------------------------------------------------

@app.route("/api/system")
def system():
    start, end, bucket, r = get_range_params()
    cols = ["cpu_temp", "cpu_freq", "load_1m", "load_5m", "load_15m",
            "mem_used", "mem_total", "disk_used", "disk_free"]

    rows = db.query_bucketed("system_metrics", start, end, bucket, cols)
    add_iso(rows, "bucket")
    return jsonify({"range": r, "bucket_secs": bucket, "rows": rows})


# ---------------------------------------------------------------------------
# /api/events
# ---------------------------------------------------------------------------

@app.route("/api/events")
def events():
    limit = min(int(request.args.get("limit", 100)), 500)
    level = request.args.get("level")
    source = request.args.get("source")

    where = "WHERE 1=1"
    params = []
    if level:
        where += " AND level = ?"
        params.append(level)
    if source:
        where += " AND source = ?"
        params.append(source)

    sql = f"SELECT * FROM events {where} ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    result = [dict(r) for r in rows]
    for row in result:
        row["iso"] = ts_to_iso(row["ts"])

    return jsonify({"rows": result})


# ---------------------------------------------------------------------------
# Serve dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")


# ---------------------------------------------------------------------------
# CORS for local development
# ---------------------------------------------------------------------------

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("MONITOR_PORT", 5001))
    print(f"Starting GPS/NTP monitor API on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
