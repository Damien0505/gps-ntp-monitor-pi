#!/usr/bin/env python3
"""
collect.py — Collects one sample from chronyc, gpsd, and system metrics,
             then writes to the SQLite database.

Run via systemd timer every 60 seconds (see monitor-collect.timer).
Can also be run manually: python3 collect.py [--verbose]
"""

import subprocess
import re
import json
import os
import sys
import glob
import logging
import argparse
from datetime import datetime, timezone

import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("collect")

GPSD_HOST = os.environ.get("GPSD_HOST", "127.0.0.1")
GPSD_PORT = int(os.environ.get("GPSD_PORT", "2947"))


# ---------------------------------------------------------------------------
# chronyc tracking
# ---------------------------------------------------------------------------

def parse_tracking(verbose=False):
    try:
        out = subprocess.check_output(["chronyc", "-c", "tracking"],
                                      stderr=subprocess.DEVNULL, text=True, timeout=10)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        db.insert_event("chrony", "error", f"chronyc tracking failed: {e}")
        log.warning("chronyc tracking failed: %s", e)
        return None

    # CSV output columns (chronyc -c) — two variants exist depending on chrony version:
    #
    # 13-column (older):
    #   RefID-hex, Stratum, RefTime, SysOffset, LastOffset, RMSOffset,
    #   FreqError, ResidualFreq, Skew, RootDelay, RootDispersion, UpdateInterval, LeapStatus
    #
    # 14-column (newer, e.g. chrony 4.x):
    #   RefID-hex, RefID-name, Stratum, RefTime, SysOffset, LastOffset, RMSOffset,
    #   FreqError, ResidualFreq, Skew, RootDelay, RootDispersion, UpdateInterval, LeapStatus
    #
    # Detected on this system: 14-column format confirmed.
    parts = out.strip().split(",")
    if len(parts) < 13:
        db.insert_event("chrony", "error", f"Unexpected tracking output: {out!r}")
        return None

    # Auto-detect format: if parts[2] cannot be parsed as a float (it's a name
    # like "PPS" or "GPS"), we have the 14-column variant.
    # Detect format: in the 14-column variant parts[1] is a name like "PPS"
    # or "GPS" (non-integer); in the 13-column variant parts[1] is the stratum
    # integer. Using int() is more reliable than float() on parts[2].
    try:
        int(parts[1])
        col_offset = 0   # 13-column: parts[1] is stratum
    except ValueError:
        col_offset = 1   # 14-column: parts[1] is name e.g. "PPS"

    # Build ref_id as "hex (name)" when name field is present
    ref_id = parts[0]
    if col_offset == 1:
        ref_id = f"{parts[0]} ({parts[1]})"

    try:
        data = {
            "ref_id":          ref_id,
            "stratum":         int(parts[1 + col_offset]),
            "ref_time":        float(parts[2 + col_offset]),
            "sys_time_offset": float(parts[3 + col_offset]),
            "last_offset":     float(parts[4 + col_offset]),
            "rms_offset":      float(parts[5 + col_offset]),
            "freq_offset":     float(parts[6 + col_offset]),
            "residual_freq":   float(parts[7 + col_offset]),
            "skew":            float(parts[8 + col_offset]),
            "root_delay":      float(parts[9 + col_offset]),
            "root_dispersion": float(parts[10 + col_offset]),
            "update_interval": float(parts[11 + col_offset]),
            "leap_status":     parts[12 + col_offset].strip(),
        }
        if verbose:
            log.info("tracking: sys_offset=%.3e rms=%.3e freq=%.3f skew=%.3f",
                     data["sys_time_offset"], data["rms_offset"],
                     data["freq_offset"], data["skew"])
        return data
    except (ValueError, IndexError) as e:
        db.insert_event("chrony", "error", f"Parse error in tracking: {e}")
        log.warning("Tracking parse error: %s", e)
        return None


# ---------------------------------------------------------------------------
# chronyc sources
# ---------------------------------------------------------------------------

def parse_sources(verbose=False):
    try:
        out = subprocess.check_output(["chronyc", "-c", "sources"],
                                      stderr=subprocess.DEVNULL, text=True, timeout=10)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        db.insert_event("chrony", "error", f"chronyc sources failed: {e}")
        return []

    # CSV columns confirmed from live output (10 fields, 0-indexed):
    #   [0] mode      '#'=refclock  '^'=server  '='=peer
    #   [1] state     '*'=synced  '+'=combined  '-'=not selected  '?'=unreachable
    #   [2] name      e.g. PPS, GPS, 129.250.35.250
    #   [3] stratum
    #   [4] poll      (log2 seconds)
    #   [5] reach     (octal reachability as decimal; 377 = all 8 polls OK)
    #   [6] last_rx   (seconds since last sample)
    #   [7] offset    (latest offset, seconds)
    #   [8] err_bound (latest error bound, seconds)
    #   [9] std_dev   (std dev of last sample — present but not stored separately)
    rows = []
    for line in out.strip().splitlines():
        parts = line.split(",")
        if len(parts) < 9:
            continue
        try:
            rows.append({
                "mode":      parts[0].strip(),
                "state":     parts[1].strip(),
                "name":      parts[2].strip(),
                "stratum":   int(parts[3]),
                "poll":      int(parts[4]),
                "reach":     int(parts[5]),
                "last_rx":   int(parts[6]),
                "offset":    float(parts[7]),
                "err_bound": float(parts[8]),
            })
        except (ValueError, IndexError):
            continue

    if verbose:
        log.info("sources: %d entries", len(rows))
    return rows


# ---------------------------------------------------------------------------
# chronyc sourcestats
# ---------------------------------------------------------------------------

def parse_sourcestats(verbose=False):
    try:
        out = subprocess.check_output(["chronyc", "-c", "sourcestats"],
                                      stderr=subprocess.DEVNULL, text=True, timeout=10)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        db.insert_event("chrony", "error", f"chronyc sourcestats failed: {e}")
        return []

    # CSV columns confirmed from live output (8 fields, 0-indexed):
    #   [0] name          source name e.g. PPS, GPS, 129.250.35.250
    #   [1] np            total number of samples
    #   [2] nr            number of samples used (good samples)
    #   [3] span_seconds  age of oldest sample (seconds)
    #   [4] residual_freq residual frequency error (ppm)
    #   [5] skew          estimated frequency error (ppm)
    #   [6] std_dev       standard deviation of offset samples (seconds)
    #   [7] offset_sd     estimated standard deviation of combined offset (seconds)
    rows = []
    for line in out.strip().splitlines():
        parts = line.split(",")
        if len(parts) < 8:
            continue
        try:
            rows.append({
                "name":          parts[0].strip(),
                "samples":       int(parts[1]),
                "residual_freq": float(parts[4]),
                "skew":          float(parts[5]),
                "std_dev":       float(parts[6]),
                "est_offset":    0.0,
                "offset_sd":     float(parts[7]),
            })
        except (ValueError, IndexError):
            continue

    if verbose:
        log.info("sourcestats: %d entries", len(rows))
    return rows


# ---------------------------------------------------------------------------
# gpsd  (using socket directly — no gpsd Python library required)
# ---------------------------------------------------------------------------

def get_gpsd_data(verbose=False):
    import socket, time

    data = {
        "fix_mode":  1,
        "latitude":  None, "longitude": None, "altitude": None,
        "speed":     None, "track":     None, "climb":    None,
        "hdop":      None, "vdop":      None, "tdop":     None,
        "pdop":      None, "gdop":      None,
        "sats_used": None, "sats_seen": None,
        "time_err":  None, "leap_secs": None,
    }

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((GPSD_HOST, GPSD_PORT))

        # Enable JSON watch
        s.sendall(b'?WATCH={"enable":true,"json":true}\n')

        deadline = time.monotonic() + 5
        buf = b""
        tpv_received = False
        sky_received = False

        while time.monotonic() < deadline and not (tpv_received and sky_received):
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                break

            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                cls = msg.get("class", "")

                if cls == "TPV":
                    data["fix_mode"]  = msg.get("mode", 1)
                    data["latitude"]  = msg.get("lat")
                    data["longitude"] = msg.get("lon")
                    data["altitude"]  = msg.get("altMSL") or msg.get("alt")
                    data["speed"]     = msg.get("speed")
                    data["track"]     = msg.get("track")
                    data["climb"]     = msg.get("climb")
                    data["time_err"]  = msg.get("ept")
                    data["leap_secs"] = msg.get("leapseconds")
                    tpv_received = True

                elif cls == "SKY":
                    data["hdop"] = msg.get("hdop")
                    data["vdop"] = msg.get("vdop")
                    data["tdop"] = msg.get("tdop")
                    data["pdop"] = msg.get("pdop")
                    data["gdop"] = msg.get("gdop")
                    sats = msg.get("satellites", [])
                    data["sats_seen"] = len(sats)
                    data["sats_used"] = sum(1 for s2 in sats if s2.get("used"))
                    sky_received = True

        s.sendall(b'?WATCH={"enable":false}\n')
        s.close()

        if verbose:
            log.info("gpsd: mode=%d hdop=%s sats=%s/%s",
                     data["fix_mode"], data["hdop"],
                     data["sats_used"], data["sats_seen"])

    except (OSError, ConnectionRefusedError) as e:
        db.insert_event("gpsd", "warn", f"gpsd connect failed: {e}")
        log.warning("gpsd connect failed: %s", e)

    return data


# ---------------------------------------------------------------------------
# System metrics
# ---------------------------------------------------------------------------

def get_system_metrics(verbose=False):
    data = {
        "cpu_temp":  None,
        "cpu_freq":  None,
        "load_1m":   None,
        "load_5m":   None,
        "load_15m":  None,
        "mem_total": None,
        "mem_used":  None,
        "mem_free":  None,
        "disk_used": None,
        "disk_free": None,
    }

    # CPU temperature — try thermal zone 0 first, then vcgencmd (Pi)
    temp_paths = sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp"))
    if temp_paths:
        try:
            raw = int(open(temp_paths[0]).read().strip())
            data["cpu_temp"] = raw / 1000.0
        except (OSError, ValueError):
            pass

    if data["cpu_temp"] is None:
        try:
            out = subprocess.check_output(["vcgencmd", "measure_temp"],
                                          text=True, timeout=3, stderr=subprocess.DEVNULL)
            m = re.search(r"temp=([\d.]+)", out)
            if m:
                data["cpu_temp"] = float(m.group(1))
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

    # CPU frequency (kHz → MHz)
    freq_paths = glob.glob("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if freq_paths:
        try:
            data["cpu_freq"] = int(open(freq_paths[0]).read().strip()) / 1000.0
        except (OSError, ValueError):
            pass

    # Load averages
    try:
        load = os.getloadavg()
        data["load_1m"], data["load_5m"], data["load_15m"] = load
    except OSError:
        pass

    # Memory from /proc/meminfo
    try:
        mem = {}
        for line in open("/proc/meminfo"):
            parts = line.split()
            if parts[0] in ("MemTotal:", "MemFree:", "MemAvailable:", "Buffers:", "Cached:"):
                mem[parts[0]] = int(parts[1])
        total = mem.get("MemTotal:", 0)
        free  = mem.get("MemFree:", 0)
        avail = mem.get("MemAvailable:", free)
        data["mem_total"] = total
        data["mem_free"]  = avail
        data["mem_used"]  = total - avail
    except (OSError, ValueError):
        pass

    # Disk usage for filesystem containing the database
    try:
        import shutil
        db_dir = os.path.dirname(os.path.abspath(db.DB_PATH))
        st = shutil.disk_usage(db_dir)
        data["disk_used"] = (st.used) // (1024 * 1024)
        data["disk_free"] = (st.free) // (1024 * 1024)
    except OSError:
        pass

    if verbose:
        log.info("system: temp=%.1f°C freq=%.0fMHz load=%.2f",
                 data["cpu_temp"] or 0, data["cpu_freq"] or 0, data["load_1m"] or 0)

    return data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GPS/NTP monitor data collector")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--prune", action="store_true",
                        help="Run data pruning after collection")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    db.init_db()

    errors = 0

    # Chrony tracking
    tracking = parse_tracking(args.verbose)
    if tracking:
        db.insert_chrony_tracking(tracking)
    else:
        errors += 1

    # Chrony sources
    sources = parse_sources(args.verbose)
    if sources:
        db.insert_chrony_sources(sources)
    else:
        errors += 1

    # Chrony sourcestats
    sourcestats = parse_sourcestats(args.verbose)
    if sourcestats:
        db.insert_chrony_sourcestats(sourcestats)
    else:
        errors += 1

    # GPSD
    gpsd = get_gpsd_data(args.verbose)
    db.insert_gpsd(gpsd)

    # System
    system = get_system_metrics(args.verbose)
    db.insert_system_metrics(system)

    if errors > 0:
        log.warning("Collection completed with %d error(s). Check events table.", errors)
    else:
        log.info("Collection complete. DB size: %s MB", db.db_size_mb())

    if args.prune:
        db.prune_old_data()
        log.info("Pruning complete.")


if __name__ == "__main__":
    main()
