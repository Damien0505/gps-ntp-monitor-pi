#!/bin/bash
# install.sh — Sets up the GPS/NTP monitor on Raspberry Pi / Debian/Ubuntu
# Run as root: sudo bash install.sh
# ---------------------------------------------------------------------------

set -e

INSTALL_DIR="/opt/gps-ntp-monitor"
PYTHON="python3"

echo "=== GPS/NTP Monitor installer ==="
echo ""

# ── Detect user ──────────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root: sudo bash install.sh"
  exit 1
fi

# Use the user who invoked sudo; fall back to first normal user if needed
if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
  SERVICE_USER="$SUDO_USER"
else
  SERVICE_USER=$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 {print $1; exit}')
fi

if [ -z "$SERVICE_USER" ]; then
  echo "ERROR: Could not determine a non-root service user."
  echo "Please edit install.sh and set SERVICE_USER=yourusername near the top."
  exit 1
fi

echo "  Using service user: ${SERVICE_USER}"

# ── Install Python deps ──────────────────────────────────────────────────────
echo "[1/5] Installing Python dependencies..."
apt-get update -qq
apt-get install -y python3-pip python3-flask gpsd gpsd-clients chrony 2>/dev/null || true
pip3 install flask --break-system-packages 2>/dev/null || pip3 install flask

# ── Copy files ───────────────────────────────────────────────────────────────
echo "[2/5] Installing application files to ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"
cp "$(dirname "$0")/db.py"         "$INSTALL_DIR/"
cp "$(dirname "$0")/collect.py"    "$INSTALL_DIR/"
cp "$(dirname "$0")/api.py"        "$INSTALL_DIR/"
cp "$(dirname "$0")/dashboard.html" "$INSTALL_DIR/"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

# ── Initialise database ───────────────────────────────────────────────────────
echo "[3/5] Initialising database..."
sudo -u "$SERVICE_USER" "$PYTHON" "$INSTALL_DIR/db.py"

# ── Systemd: collector timer ──────────────────────────────────────────────────
echo "[4/5] Installing systemd units..."

cat > /etc/systemd/system/gps-ntp-collect.service << EOF
[Unit]
Description=GPS/NTP Monitor — data collector
After=network.target chrony.service gpsd.service
Wants=chrony.service gpsd.service

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PYTHON} ${INSTALL_DIR}/collect.py
Environment=MONITOR_DB=${INSTALL_DIR}/monitor.db
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/gps-ntp-collect.timer << EOF
[Unit]
Description=GPS/NTP Monitor — run collector every 60 seconds
Requires=gps-ntp-collect.service

[Timer]
OnActiveSec=10
OnUnitActiveSec=60
AccuracySec=5
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Pruning timer (daily at 03:00)
cat > /etc/systemd/system/gps-ntp-prune.service << EOF
[Unit]
Description=GPS/NTP Monitor — database pruning
After=network.target

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PYTHON} ${INSTALL_DIR}/collect.py --prune
Environment=MONITOR_DB=${INSTALL_DIR}/monitor.db
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/gps-ntp-prune.timer << EOF
[Unit]
Description=GPS/NTP Monitor — prune old data daily

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# API server (runs as persistent service)
cat > /etc/systemd/system/gps-ntp-api.service << EOF
[Unit]
Description=GPS/NTP Monitor — web API and dashboard
After=network.target
Wants=gps-ntp-collect.timer

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PYTHON} ${INSTALL_DIR}/api.py
Environment=MONITOR_DB=${INSTALL_DIR}/monitor.db
Environment=MONITOR_PORT=5001
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# ── Enable and start ──────────────────────────────────────────────────────────
echo "[5/5] Enabling and starting services..."
systemctl daemon-reload
systemctl enable --now gps-ntp-collect.timer
systemctl enable --now gps-ntp-prune.timer
systemctl enable --now gps-ntp-api.service

echo ""
echo "=== Installation complete ==="
echo ""
echo "  Dashboard:  http://$(hostname -I | awk '{print $1}'):5001"
echo "  API:        http://$(hostname -I | awk '{print $1}'):5001/api/status"
echo ""
echo "  Check collector:  journalctl -u gps-ntp-collect.service -f"
echo "  Check API:        journalctl -u gps-ntp-api.service -f"
echo "  Check timers:     systemctl list-timers gps-ntp*"
echo ""
echo "  Database:   ${INSTALL_DIR}/monitor.db"
echo ""
echo "  To adjust retention (default: 90 days), edit prune_old_data() in db.py"
echo "  To change sample interval, edit gps-ntp-collect.timer OnUnitActiveSec="
echo ""
