#!/bin/bash
set -e
APP_DIR="/var/www/scolapp_api"
SERVICE="scolapp-api"
DOMAIN="api.scolapp.com"

echo "======================================================"
echo "  DEPLOYING (HTTPS) — $DOMAIN"
echo "======================================================"

echo "[1/8] System packages..."
apt-get update -y -q
apt-get install -y -q python3 python3-pip python3-venv nginx certbot python3-certbot-nginx curl ufw fail2ban

echo "[2/8] App directory..."
mkdir -p "$APP_DIR"
cp main.py           "$APP_DIR/"
cp dmoney_gateway.py "$APP_DIR/"
cp requirements.txt  "$APP_DIR/"
[ ! -f "$APP_DIR/.env" ] && cp .env "$APP_DIR/.env" && echo "  .env copied" || echo "  .env kept"

echo "[3/8] Python virtualenv..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip -q
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

echo "[4/8] Permissions..."
chown -R www-data:www-data "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "[5/8] Systemd service..."
cp scolapp-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE" && echo "  Service OK" || echo "  WARNING: journalctl -u $SERVICE"

echo "[6/8] Nginx (HTTP first, then HTTPS after certbot)..."
cat > /etc/nginx/sites-available/scolapp-api << 'NGINX'
server {
    listen 80;
    server_name api.scolapp.com;
    server_tokens off;
    client_max_body_size 64k;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/scolapp-api /etc/nginx/sites-enabled/scolapp-api
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo "[7/8] SSL certificate..."
VPS_IP=$(curl -s ifconfig.me)
echo "  VPS IP: $VPS_IP"
echo "  Make sure DNS: api.scolapp.com → $VPS_IP"
read -p "  DNS ready? (y/n): " dns_ready
if [[ "$dns_ready" == "y" ]]; then
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m admin@scolapp.com
    cp nginx-scolapp-api.conf /etc/nginx/sites-available/scolapp-api
    nginx -t && systemctl reload nginx
    echo "  HTTPS enabled"
else
    echo "  Skipping SSL — run later: certbot --nginx -d $DOMAIN"
fi

echo "[8/8] Firewall + Fail2Ban..."
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

# Fail2Ban protects against repeated failures
cat > /etc/fail2ban/jail.d/nginx-api.conf << 'F2B'
[nginx-http-auth]
enabled = true

[nginx-limit-req]
enabled  = true
filter   = nginx-limit-req
action   = iptables-multiport[name=ReqLimit, port="http,https", protocol=tcp]
logpath  = /var/log/nginx/error.log
findtime = 600
bantime  = 7200
maxretry = 10
F2B
systemctl restart fail2ban && echo "  Fail2Ban active"

echo ""
echo "======================================================"
echo "  DEPLOYMENT COMPLETE"
echo "  Health:  https://$DOMAIN/health"
echo "  Docs:    https://$DOMAIN/docs"
echo "  Logs:    journalctl -u $SERVICE -f"
echo "======================================================"