#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# deploy.sh — Full VPS setup for api.scolapp.com on Ubuntu 20.04
# Upload all files first, then run:  bash deploy.sh
# ──────────────────────────────────────────────────────────────────────────────
set -e

APP_DIR="/var/www/scolapp_api"
SERVICE="scolapp-api"
DOMAIN="api.scolapp.com"

echo ""
echo "======================================================"
echo "  DEPLOYING D-MONEY API → $DOMAIN"
echo "======================================================"

# ── 1. System packages ─────────────────────────────────────────────────────────
echo ""
echo "[1/8] Installing system packages..."
apt-get update -y -q
apt-get install -y -q python3 python3-pip python3-venv nginx certbot python3-certbot-nginx curl ufw

# ── 2. App directory ───────────────────────────────────────────────────────────
echo "[2/8] Setting up app directory at $APP_DIR..."
mkdir -p "$APP_DIR"

cp main.py           "$APP_DIR/"
cp dmoney_gateway.py "$APP_DIR/"
cp requirements.txt  "$APP_DIR/"

if [ ! -f "$APP_DIR/.env" ]; then
    cp .env "$APP_DIR/.env"
    echo "  .env copied."
else
    echo "  .env already exists — keeping existing config."
fi

# ── 3. Python virtualenv ───────────────────────────────────────────────────────
echo "[3/8] Creating Python virtualenv and installing dependencies..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip -q
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
echo "  Dependencies installed."

# ── 4. Permissions ────────────────────────────────────────────────────────────
echo "[4/8] Setting permissions..."
chown -R www-data:www-data "$APP_DIR"
chmod 600 "$APP_DIR/.env"

# ── 5. Systemd service ────────────────────────────────────────────────────────
echo "[5/8] Setting up systemd service..."
cp scolapp-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE" && echo "  Service running OK." || echo "  WARNING: service not running. Check: journalctl -u $SERVICE -n 50"

# ── 6. Nginx ──────────────────────────────────────────────────────────────────
echo "[6/8] Configuring Nginx..."
cp nginx-scolapp-api.conf /etc/nginx/sites-available/scolapp-api
ln -sf /etc/nginx/sites-available/scolapp-api /etc/nginx/sites-enabled/scolapp-api
# Remove default nginx site if it exists
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx
echo "  Nginx configured."

# ── 7. SSL with Let's Encrypt ─────────────────────────────────────────────────
echo "[7/8] Obtaining SSL certificate for $DOMAIN..."
echo "  (Make sure your DNS A record for $DOMAIN points to this VPS IP first!)"
VPS_IP=$(curl -s ifconfig.me)
echo "  This VPS IP: $VPS_IP"
read -p "  DNS ready? SSL cert will fail if not. Continue? (y/n): " confirm
if [[ "$confirm" == "y" ]]; then
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m admin@scolapp.com
    # Enable the HTTPS block in nginx config
    sed -i 's/^# //' /etc/nginx/sites-available/scolapp-api
    sed -i '/return 301/d' /etc/nginx/sites-available/scolapp-api
    nginx -t && systemctl reload nginx
    echo "  SSL certificate installed."
else
    echo "  Skipping SSL — run manually later:"
    echo "  certbot --nginx -d $DOMAIN --agree-tos -m admin@scolapp.com"
    # Temporarily serve HTTP without redirect so API is reachable
    cat > /etc/nginx/sites-available/scolapp-api << 'NGINX'
server {
    listen 80;
    server_name api.scolapp.com;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
NGINX
    nginx -t && systemctl reload nginx
fi

# ── 8. Firewall ───────────────────────────────────────────────────────────────
echo "[8/8] Configuring firewall..."
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
echo "  Firewall enabled."

# ── Done ──────────────────────────────────────────────────────────────────────
VPS_IP=$(curl -s ifconfig.me)
echo ""
echo "======================================================"
echo "  DEPLOYMENT COMPLETE"
echo "======================================================"
echo ""
echo "  VPS IP  : $VPS_IP"
echo "  Domain  : https://$DOMAIN"
echo ""
echo "  Endpoints:"
echo "    GET  https://$DOMAIN/health"
echo "    GET  https://$DOMAIN/docs"
echo "    POST https://$DOMAIN/payment/create"
echo "    POST https://$DOMAIN/payment/query"
echo "    GET  https://$DOMAIN/payment/token"
echo ""
echo "  Test health now:"
echo "    curl http://$VPS_IP/health"
echo ""
echo "  View logs:"
echo "    journalctl -u $SERVICE -f"
echo ""
echo "  IMPORTANT: Edit /var/www/scolapp_api/.env and set a strong API_KEY"
echo "  Then restart: systemctl restart $SERVICE"
echo ""