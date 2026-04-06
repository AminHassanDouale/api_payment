#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# update.sh — Pull latest code from GitHub and restart the service on VPS
# Run on VPS:  bash update.sh
# ──────────────────────────────────────────────────────────────────────────────

set -e

APP_DIR="/var/www/scolapp_api"
SERVICE="scolapp-api"
REPO_DIR="/root/scolapp_api"   # where you cloned the repo

echo ""
echo "======================================================"
echo "  UPDATING D-Money API from GitHub"
echo "======================================================"

# ── Pull latest code ──────────────────────────────────────────────────────────
echo ""
echo "[1/4] Pulling latest code..."
cd "$REPO_DIR"
git pull origin main
echo "  Done."

# ── Copy updated files to app dir (never overwrites .env) ────────────────────
echo "[2/4] Copying files to $APP_DIR..."
cp main.py           "$APP_DIR/"
cp dmoney_gateway.py "$APP_DIR/"
cp requirements.txt  "$APP_DIR/"
echo "  Files copied. (.env preserved)"

# ── Install any new dependencies ─────────────────────────────────────────────
echo "[3/4] Updating dependencies..."
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
echo "  Done."

# ── Restart service ───────────────────────────────────────────────────────────
echo "[4/4] Restarting service..."
systemctl restart "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE" && echo "  Service running OK." || echo "  WARNING: service not running! Run: journalctl -u $SERVICE -n 50"

echo ""
echo "======================================================"
echo "  UPDATE COMPLETE"
echo "======================================================"
echo ""
echo "  Test: curl https://api.scolapp.com/health"
echo "  Logs: journalctl -u $SERVICE -f"
echo ""