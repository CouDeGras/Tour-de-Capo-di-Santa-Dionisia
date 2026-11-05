#!/usr/bin/env bash
set -euo pipefail

PROJ="/home/josue/saignes_en_padaine"

echo "==> Installing Django and paho-mqtt system packages"
apt-get install -y python3-django python3-paho-mqtt

echo "==> Applying Django migrations (auth/sessions/admin, plus dashboard's IrrigationDecision/MetarReading -- irrigation and METAR history now live in the DB, not CSV)"
python3 "$PROJ/manage.py" migrate --noinput

echo "==> Installing systemd units"
cp "$PROJ/saignes-dashboard.service"  /etc/systemd/system/
cp "$PROJ/saignes-weather.service"    /etc/systemd/system/

echo "==> Installing Avahi service advertisement"
cp "$PROJ/saignes-dashboard.xml" /etc/avahi/services/

systemctl daemon-reload

echo "==> Enabling and starting dashboard (auto-starts on boot)"
systemctl enable --now saignes-dashboard.service

echo "==> Enabling and starting the weather/irrigation service (persistent -- tri-hourly forecast cycle, hourly METAR refresh, and MQTT pump-ack listener all in one process)"
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
