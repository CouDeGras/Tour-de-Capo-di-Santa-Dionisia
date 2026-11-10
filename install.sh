#!/usr/bin/env bash
set -euo pipefail

PROJ="/home/josue/saignes_en_padaine"

echo "==> Installing Django system packages"
apt-get install -y python3-django

echo "==> Applying Django migrations for dashboard and airport observation history"
python3 "$PROJ/manage.py" migrate --noinput

echo "==> Installing systemd units"
cp "$PROJ/saignes-dashboard.service"  /etc/systemd/system/
cp "$PROJ/saignes-weather.service"    /etc/systemd/system/

echo "==> Installing Avahi service advertisement"
cp "$PROJ/saignes-dashboard.xml" /etc/avahi/services/

systemctl daemon-reload

echo "==> Enabling and starting dashboard (auto-starts on boot)"
systemctl enable --now saignes-dashboard.service

echo "==> Enabling weather forecasts and hourly airport observation refresh"
systemctl enable --now saignes-weather.service

echo "==> Reloading Avahi so the mDNS advertisement goes live"
systemctl reload avahi-daemon

echo ""
echo "Done. Dashboard: http://localhost:8080/"
echo "      mDNS:      http://$(hostname).local:8080/"
echo ""
echo "Check logs:"
echo "  journalctl -u saignes-dashboard  -f"
echo "  journalctl -u saignes-weather    -f"
