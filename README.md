# D-Money Payment API — api.scolapp.com

## Project Structure

```
scolapp_api/
├── main.py                  # FastAPI application
├── dmoney_gateway.py        # D-Money gateway class
├── requirements.txt         # Python dependencies
├── .env                     # Your real credentials  ← never commit this
├── .env.example             # Safe template          ← commit this
├── .gitignore               # Keeps .env out of Git
├── run_local.sh             # Run locally for testing
├── deploy.sh                # First-time VPS setup
├── update.sh                # Pull from GitHub & restart on VPS
├── scolapp-api.service      # Systemd service unit
└── nginx-scolapp-api.conf   # Nginx reverse proxy config
```

---

## STEP 1 — Test Locally

### 1a. Create your .env
```bash
cp .env.example .env
nano .env    # paste your real credentials
```

### 1b. Run the server
```bash
chmod +x run_local.sh
bash run_local.sh
```

Server starts at: http://localhost:8000

### 1c. Test endpoints

Health check:
```bash
curl http://localhost:8000/health
```

Create payment:
```bash
curl -X POST http://localhost:8000/payment/create \
  -H "Content-Type: application/json" \
  -H "X-API-Key: scolapp-dmoney-secret-2026" \
  -d '{"amount": 1000, "title": "Test Payment", "language": "en"}'
```

Query order:
```bash
curl -X POST http://localhost:8000/payment/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: scolapp-dmoney-secret-2026" \
  -d '{"merch_order_id": "ORD20260405AB1234"}'
```

Interactive Swagger UI: http://localhost:8000/docs

---

## STEP 2 — Push to GitHub

### 2a. Create a new PRIVATE repo on GitHub
Go to https://github.com/new — name it scolapp-api, set to Private.

### 2b. Initialize and push
```bash
cd scolapp_api
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/scolapp-api.git
git push -u origin main
```

.env is in .gitignore so it will NEVER be pushed to GitHub.
Only .env.example is committed as a safe template.

---

## STEP 3 — Deploy to VPS (First Time)

### 3a. SSH into your VPS
```bash
ssh root@YOUR_VPS_IP
```

### 3b. Clone the repo
```bash
cd /root
git clone https://github.com/YOUR_USERNAME/scolapp-api.git scolapp_api
cd scolapp_api
```

### 3c. Create .env on the VPS
```bash
cp .env.example .env
nano .env    # paste your real credentials
```

### 3d. Add DNS record
In your domain registrar add:
  Type: A  |  Name: api  |  Value: YOUR_VPS_IP  |  TTL: 300

### 3e. Run deploy script
```bash
chmod +x deploy.sh
bash deploy.sh
```

### 3f. Test live
```bash
curl https://api.scolapp.com/health
```

---

## STEP 4 — Update VPS After Code Changes

On your local machine:
```bash
git add .
git commit -m "your change description"
git push origin main
```

On your VPS:
```bash
ssh root@YOUR_VPS_IP
cd /root/scolapp_api
bash update.sh
```

---

## API Reference

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | /health | No | Health check |
| GET | /docs | No | Swagger UI |
| GET | /payment/token | Yes | Refresh token |
| POST | /payment/create | Yes | Create payment |
| POST | /payment/query | Yes | Query order |

Header for auth: X-API-Key: your-key

---

## Useful VPS Commands

```bash
journalctl -u scolapp-api -f        # live logs
systemctl restart scolapp-api       # restart
systemctl status scolapp-api        # status
nano /var/www/scolapp_api/.env      # edit credentials
certbot renew                       # renew SSL
```
"# api_payment" 
