# GPS / NTP Monitor

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

A lightweight self-hosted dashboard for monitoring a stratum-1 GPS/PPS NTP server.
Collects data from `chronyc`, `gpsd`, and system sensors every 60 seconds,
stores it in SQLite, and serves a live web dashboard.

> **Author:** Damien0505 — [github.com/Damien0505](https://github.com/Damien0505)

---

## Files

| File | Purpose |
|------|---------|
| `db.py` | SQLite schema, insert helpers, bucketed query functions |
| `collect.py` | Runs every 60s: parses chronyc/gpsd/system, writes to DB |
| `api.py` | Flask API server — serves JSON data + the dashboard HTML |
| `dashboard.html` | Single-page dashboard (fetches from the API) |
| `install.sh` | Automated installer for Raspberry Pi / Debian / Ubuntu |

---

## Quick install (Raspberry Pi / Debian)

```bash
git clone https://github.com/Damien0505/gps-ntp-monitor-pi.git gps-ntp-monitor
cd gps-ntp-monitor
sudo bash install.sh
```

Then open `http://<pi-ip>:5001` in a browser.

> **Port note:** This app runs on port **5001** by default to avoid conflicting with
> other Flask apps on port 5000. Override with `MONITOR_PORT=<n>` if needed.

---

## Manual setup

```bash
# Install dependencies
pip3 install flask

# Initialise the database
python3 db.py

# Run a test collection (add -v for verbose output)
python3 collect.py -v

# Start the API server
python3 api.py
```

---

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MONITOR_DB` | `./monitor.db` | Path to the SQLite database |
| `MONITOR_PORT` | `5001` | HTTP port for the dashboard/API |
| `GPSD_HOST` | `127.0.0.1` | gpsd host |
| `GPSD_PORT` | `2947` | gpsd port |

Example:
```bash
MONITOR_DB=/var/lib/gps-monitor/monitor.db python3 api.py
```

---

## API endpoints

All time-series endpoints accept a `range` query parameter:
`day` (default), `week`, `month`, `year`

| Endpoint | Description |
|----------|-------------|
| `GET /api/status` | Latest snapshot of all metrics |
| `GET /api/chrony/tracking?range=day` | Chrony tracking time-series |
| `GET /api/chrony/sources?range=day` | Chrony sources (all, bucketed by source name) |
| `GET /api/chrony/sourcestats?range=day` | Chrony sourcestats |
| `GET /api/gpsd?range=day` | GPSD metrics |
| `GET /api/system?range=day` | System metrics |
| `GET /api/events?limit=100&level=error` | Event log |

---

## Database schema summary

### `chrony_tracking`
`ts, ref_id, stratum, ref_time, sys_time_offset, last_offset, rms_offset,
freq_offset, residual_freq, skew, root_delay, root_dispersion, update_interval, leap_status`

### `chrony_sources`
`ts, name, mode, state, stratum, poll, reach, last_rx, offset, err_bound`

### `chrony_sourcestats`
`ts, name, samples, residual_freq, skew, std_dev, est_offset, offset_sd`

### `gpsd`
`ts, fix_mode, latitude, longitude, altitude, speed, track, climb,
hdop, vdop, tdop, pdop, gdop, sats_used, sats_seen, time_err, leap_secs`

### `system_metrics`
`ts, cpu_temp, cpu_freq, load_1m, load_5m, load_15m, mem_total, mem_used, mem_free, disk_used, disk_free`

### `events`
`ts, source, level, msg` — collector errors, warnings, and info messages

---

## Storage estimates

At 60-second sampling:

| Interval | Estimated DB size |
|----------|------------------|
| 7 days   | ~4 MB |
| 30 days  | ~17 MB |
| 90 days  | ~50 MB |
| 1 year   | ~200 MB |

The default retention policy (set in `db.py → prune_old_data()`) is **90 days**,
keeping the database under ~50 MB indefinitely. Adjust `days_hourly` to suit.

For a full-year archive at full resolution, set `days_hourly=365`.
For a lean always-on deployment, set `days_hourly=30` (~17 MB).

---

## Systemd services installed

| Unit | Description |
|------|-------------|
| `gps-ntp-collect.timer` | Fires the collector every 60 seconds |
| `gps-ntp-collect.service` | The collector oneshot (triggered by timer) |
| `gps-ntp-prune.timer` | Runs pruning daily at 03:00 |
| `gps-ntp-prune.service` | The pruning oneshot |
| `gps-ntp-api.service` | The Flask API server (persistent, restarts on failure) |

Useful commands:
```bash
# Watch live collector output
journalctl -u gps-ntp-collect.service -f

# Check timer schedule
systemctl list-timers gps-ntp*

# Restart API
sudo systemctl restart gps-ntp-api.service

# View recent errors
sqlite3 /opt/gps-ntp-monitor/monitor.db \
  "SELECT datetime(ts,'unixepoch'), source, msg FROM events WHERE level='error' ORDER BY ts DESC LIMIT 20;"
```

---

## Troubleshooting

**chronyc returns no data**
- Ensure chrony is running: `systemctl status chrony`
- Test manually: `chronyc -c tracking`

**gpsd returns no data**
- Ensure gpsd is running: `systemctl status gpsd`
- Test manually: `gpspipe -w -n 5`
- Check `GPSD_HOST`/`GPSD_PORT` environment variables

**Dashboard shows "Cannot reach API"**
- Ensure api.py is running: `systemctl status gps-ntp-api.service`
- Check firewall: `sudo ufw allow 5001/tcp`

**CPU temperature missing**
- On Raspberry Pi: install `raspi-config` and enable thermal sensor
- On x86: install `lm-sensors` and run `sensors-detect`

---

## Screenshots:
![Image](https://github.com/user-attachments/assets/26e2ab35-419b-44b5-8269-9964323aec47)
![Image](https://github.com/user-attachments/assets/5f046baa-6f53-46fc-8105-4027b671256f)
![Image](https://github.com/user-attachments/assets/c3fedb0b-c0c9-40b4-a7f1-490324f08ce0)
![Image](https://github.com/user-attachments/assets/489e2eb2-ba6e-41fd-80ba-b9a7cabc9c29)
![Image](https://github.com/user-attachments/assets/46c1302f-543a-4765-98cc-73c464a58013)

---

## Contributing

Contributions are welcome! Please open an issue or pull request. By contributing
you agree that your contributions will be licensed under the same GPL v3 licence.

---

## Licence

Copyright (C) 2026 Damien0505

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this
program. If not, see <https://www.gnu.org/licenses/>.

If you would like to use this project in a commercial context, please contact the
author to discuss licensing arrangements.
