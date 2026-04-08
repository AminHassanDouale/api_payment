#!/bin/bash
set -e
APP_DIR="/var/www/scolapp_api"
cd /root/scolapp_api
git pull origin main
cp main.py           "$APP_DIR/"
cp dmoney_gateway.py "$APP_DIR/"
cp requirements.txt  "$APP_DIR/"
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
systemctl restart scolapp-api
sleep 2
systemctl is-active --quiet scolapp-api && echo "OK" || echo "Check: journalctl -u scolapp-api"
echo "curl https://api.scolapp.com/health"