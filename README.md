# Scolapp D-Money Payment Gateway — Integration Guide

**Base URL:** `https://api.scolapp.com`
**Version:** 1.0.0
**Authentication:** None required
**Protocol:** HTTPS only
**Interactive Docs:** https://api.scolapp.com/docs

---

## Project Structure

```
api-payment/
├── main.py                  # FastAPI application (endpoints, DB, middleware)
├── dmoney_gateway.py        # D-Money SDK (token, RSA signing, preorder, checkout URL)
├── requirements.txt         # Python dependencies
├── payment.html             # Test payment UI
├── .env                     # Credentials & config  ← never commit this
├── run_local.sh             # Start locally on localhost:8000
├── deploy.sh                # First-time VPS setup (Ubuntu 20.04)
├── update.sh                # Pull from GitHub & restart on VPS
├── scolapp-api.service      # Systemd service unit
└── nginx-scolapp-api.conf   # Nginx reverse proxy (HTTPS, TLS 1.2/1.3)
```

---

## All Endpoints

| Method | Endpoint | Who calls it | Purpose |
|--------|----------|-------------|---------|
| `GET` | `/health` | Anyone | Check gateway is running |
| `GET` | `/docs` | Developer | Interactive Swagger UI |
| `POST` | `/payment/create` | Your platform backend | Create payment, get checkout URL |
| `POST` | `/payment/query` | Your platform backend | Check live payment status from D-Money |
| `POST` | `/payment/notify` | D-Money (automatic) | D-Money posts payment status here → saved to DB |
| `GET` | `/payment/notify/{order_id}` | Your platform | Read notification log from DB for an order |
| `GET` | `/payment/success` | Customer browser | Success landing page after payment |
| `GET` | `/payment/failed` | Customer browser | Failed landing page after payment |

---

## Payment Flow

```
Your Platform                  api.scolapp.com               D-Money
      │                               │                          │
      │  POST /payment/create         │                          │
      │ ─────────────────────────────>│                          │
      │                               │  Create PreOrder         │
      │                               │ ────────────────────────>│
      │  { checkout_url, order_id }   │ <────────────────────────│
      │ <─────────────────────────────│                          │
      │                               │                          │
      │  Save to your DB (PENDING)    │                          │
      │                               │                          │
      │  Open checkout_url in browser │                          │
      │ ──────────────────────────────────────────────────────> │
      │                               │    Customer pays         │
      │                               │                          │
      │                               │  POST /payment/notify    │
      │                               │ <────────────────────────│
      │                               │  Saved to MySQL DB       │
      │                               │  (idempotent)            │
      │                               │                          │
      │  POST /payment/query          │                          │
      │ ─────────────────────────────>│                          │
      │  { trade_status: SUCCESS }    │                          │
      │ <─────────────────────────────│                          │
      │                               │                          │
      │  Update DB → SUCCESS          │                          │
      │  Run business logic           │                          │
```

---

## Endpoints

### GET /health

```
GET https://api.scolapp.com/health
```

**Response**
```json
{
  "status": "ok",
  "gateway_ready": true,
  "base_url": "https://api.scolapp.com",
  "version": "1.0.0"
}
```

---

### POST /payment/create

```
POST https://api.scolapp.com/payment/create
Content-Type: application/json
```

**Body**

| Field | Type | Required | Validation | Description |
|-------|------|----------|------------|-------------|
| `amount` | number | ✅ | > 0, ≤ 10,000,000 | Amount in DJF |
| `title` | string | ✅ | max 128 chars | Description shown on D-Money page |
| `order_id` | string | ❌ | alphanumeric, max 64 | Your unique order ID. Auto-generated if omitted |
| `notify_url` | string | ❌ | must be `https://` | Your webhook URL for payment notifications |
| `redirect_url` | string | ❌ | must be `https://` | Customer redirect after payment |
| `timeout` | string | ❌ | e.g. `120m`, `2h` | Payment link expiry. Default `120m` |
| `language` | string | ❌ | `en` or `fr` | Payment page language. Default `en` |
| `currency` | string | ❌ | only `DJF` | Default `DJF` |

**Example**
```json
{
  "amount": 5000,
  "title": "Scolarite Trimestre 1 — Ahmed Ali",
  "order_id": "ORD2026040700001",
  "notify_url": "https://yourplatform.com/webhooks/payment",
  "redirect_url": "https://yourplatform.com/payment/success",
  "language": "en"
}
```

**Response**
```json
{
  "success": true,
  "order_id": "ORD2026040700001",
  "prepay_id": "PX20260407123456",
  "checkout_url": "https://pgtest.d-money.dj:38443/payment/web/paygate?...",
  "amount": 5000,
  "currency": "DJF"
}
```

Open `checkout_url` in the customer's browser:
```javascript
window.open(data.checkout_url, '_blank');
```

---

### POST /payment/query

Query live payment status directly from D-Money.

> ⚠️ Always call this to **verify** before marking an order as paid. Never trust the webhook payload alone.

```
POST https://api.scolapp.com/payment/query
Content-Type: application/json
```

**Body** — provide at least one:
```json
{ "merch_order_id": "ORD2026040700001" }
```
or
```json
{ "trade_no": "DM20260407123456" }
```

**Response**
```json
{
  "merch_order_id": "ORD2026040700001",
  "trade_no": "DM20260407123456",
  "trade_status": "SUCCESS",
  "total_amount": "5000",
  "trans_currency": "DJF",
  "pay_time": "2026-04-07 14:32:10"
}
```

**Statuses**

| Status | Meaning | Action |
|--------|---------|--------|
| `SUCCESS` | Payment completed | Mark order paid |
| `PENDING` | Not yet paid | Wait, keep polling |
| `FAILED` | Payment rejected | Allow retry |
| `EXPIRED` | Link expired | Create new payment |

---

### POST /payment/notify

**D-Money calls this automatically — you do not call this yourself.**

When D-Money POSTs here, the notification is saved to MySQL **instantly** and is
immediately readable via `GET /payment/notify/{order_id}`.

- **Idempotent** — duplicate `payment_order_id` is silently ignored
- **Persistent** — survives server restarts (stored in MySQL)
- **Full payload** — all D-Money webhook fields are saved

If you want D-Money to notify your own server instead, pass your `notify_url`
when calling `/payment/create`.

**D-Money webhook payload (all fields saved to DB):**
```json
{
  "notify_url":       "https://api.scolapp.com/payment/notify",
  "appid":            "1598852445107200",
  "notify_time":      "1712500860",
  "merch_code":       "200012",
  "merch_order_id":   "ORD2026040700001",
  "payment_order_id": "DM20260407123456",
  "total_amount":     "5000",
  "trans_currency":   "DJF",
  "trade_status":     "Completed",
  "trans_end_time":   "1712500855",
  "callback_info":    "oid-123_uid-456",
  "sign":             "base64-signature...",
  "sign_type":        "SHA256WithRSA"
}
```

**Trade status values from D-Money:**

| D-Money value | Meaning |
|---------------|---------|
| `Paying` | User started payment, pending confirmation |
| `Completed` | Payment successful |
| `Expired` | Payment link timed out |
| `Failure` | Payment failed |

**Response D-Money expects:**
```json
{ "returnCode": "SUCCESS", "returnMsg": "OK" }
```

---

### GET /payment/notify/{order_id}

Read the D-Money notification log from the database for a specific order.

```
GET https://api.scolapp.com/payment/notify/ORD2026040700001
```

**Response**
```json
{
  "order_id": "ORD2026040700001",
  "notifications": [
    {
      "merch_order_id":   "ORD2026040700001",
      "payment_order_id": "DM20260407123456",
      "trade_status":     "Completed",
      "total_amount":     "5000",
      "trans_currency":   "DJF",
      "trans_end_time":   "1712500855",
      "callback_info":    "oid-123_uid-456",
      "_received_at":     "2026-04-07T14:32:10+00:00"
    }
  ],
  "count": 1,
  "latest_status": "Completed"
}
```

---

## Database Schema

### Gateway Database (MySQL on api.scolapp.com)

Tables created automatically on server startup.

```sql
-- All D-Money webhook notifications (one row per notification)
CREATE TABLE payment_notifications (
    id               INT           PRIMARY KEY AUTO_INCREMENT,
    merch_order_id   VARCHAR(64)   NOT NULL,
    payment_order_id VARCHAR(64)   UNIQUE,           -- idempotency key
    appid            VARCHAR(64),
    notify_time      VARCHAR(32),
    merch_code       VARCHAR(32),
    total_amount     VARCHAR(32),
    trans_currency   VARCHAR(8),
    trade_status     VARCHAR(32),                    -- Paying | Completed | Expired | Failure
    trans_end_time   VARCHAR(32),
    callback_info    TEXT,
    sign             TEXT,
    sign_type        VARCHAR(32),
    notify_url       TEXT,
    raw_payload      TEXT,                           -- full JSON as received
    received_at      DATETIME,
    processed        BOOLEAN       DEFAULT FALSE,
    INDEX idx_order (merch_order_id)
);

-- Audit trail (one row per event)
CREATE TABLE notification_logs (
    id             INT         PRIMARY KEY AUTO_INCREMENT,
    merch_order_id VARCHAR(64),
    message        TEXT        NOT NULL,
    data           TEXT,                             -- JSON string
    type           VARCHAR(32) DEFAULT 'general',
    created_at     DATETIME,
    INDEX idx_order (merch_order_id)
);
```

### Your Platform Database (scolapp.com)

```sql
-- Orders / payments table
CREATE TABLE payments (
    id            BIGINT         PRIMARY KEY AUTO_INCREMENT,
    order_id      VARCHAR(64)    NOT NULL UNIQUE,
    user_id       VARCHAR(64)    NOT NULL,
    description   VARCHAR(128)   NOT NULL,
    amount        DECIMAL(12,2)  NOT NULL,
    currency      VARCHAR(8)     NOT NULL DEFAULT 'DJF',
    prepay_id     VARCHAR(128),
    trade_no      VARCHAR(128),
    checkout_url  TEXT,
    status        VARCHAR(20)    NOT NULL DEFAULT 'PENDING',
    created_at    DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at       DATETIME,
    expires_at    DATETIME,
    notified_at   DATETIME,
    updated_at    DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP
                                          ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_order_id (order_id),
    INDEX idx_user_id  (user_id),
    INDEX idx_trade_no (trade_no),
    INDEX idx_status   (status)
);

-- Payment event logs
CREATE TABLE payment_logs (
    id         BIGINT      PRIMARY KEY AUTO_INCREMENT,
    order_id   VARCHAR(64) NOT NULL,
    message    TEXT        NOT NULL,
    data       JSON,
    type       VARCHAR(32) DEFAULT 'general',
    created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_order_id (order_id)
);
```

**Payment status lifecycle:**
```
PENDING → SUCCESS
PENDING → FAILED
PENDING → EXPIRED
```

---

## Environment Variables (.env)

```env
# ── MySQL ─────────────────────────────────────────────────────────────────────
DATABASE_URL=mysql+pymysql://scolapp:your_password@localhost:3306/scolapp_payments

# ── D-Money Gateway ───────────────────────────────────────────────────────────
DMONEY_BASE_URL=https://pgtest.d-money.dj:38443      # test env
# DMONEY_BASE_URL=https://pg.d-moneyservice.dj       # production env

DMONEY_X_APP_KEY=452fe2b7-4105-4fc8-b002-937d10a970b1
DMONEY_APP_SECRET=d5d750fdcf550a6ae2f4fd15acd1357a
DMONEY_APPID=1598852445107200
DMONEY_MERCH_CODE=200012
DMONEY_BUSINESS_TYPE=OnlineMerchant
DMONEY_NOTIFY_URL=https://api.scolapp.com/payment/notify
DMONEY_REDIRECT_URL=https://api.scolapp.com/payment/success
DMONEY_VERIFY_SSL=false
DMONEY_TIMEOUT_SEC=30
DMONEY_LOG_LEVEL=INFO

# ── RSA Private Key (required — base64 DER format) ────────────────────────────
DMONEY_PRIVATE_KEY_B64=MIIG/gIBADANBgkqhkiG9w0...
```

> ⚠️ Never commit `.env` to Git. It is in `.gitignore`.

---

## Integration Examples

### Step 1 — Create payment and save to database

**JavaScript / Node.js**
```javascript
app.post('/checkout', async (req, res) => {
  const { userId, amount, description } = req.body;
  const orderId = 'ORD' + Date.now();

  const response = await fetch('https://api.scolapp.com/payment/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      amount, title: description, order_id: orderId,
      notify_url:   'https://yourplatform.com/webhooks/payment',
      redirect_url: 'https://yourplatform.com/payment/success',
    }),
  });
  const data = await response.json();

  await db.query(`
    INSERT INTO payments
      (order_id, user_id, description, amount, prepay_id, checkout_url, status, expires_at)
    VALUES (?,?,?,?,?,?,'PENDING', DATE_ADD(NOW(), INTERVAL 120 MINUTE))
  `, [orderId, userId, description, amount, data.prepay_id, data.checkout_url]);

  res.json({ checkout_url: data.checkout_url, order_id: orderId });
});
```

**Python / Django**
```python
def checkout(request):
    order_id = 'ORD' + datetime.now().strftime('%Y%m%d%H%M%S')
    data = request.POST

    resp = requests.post('https://api.scolapp.com/payment/create', json={
        'amount': float(data['amount']),
        'title':  data['description'],
        'order_id': order_id,
        'notify_url':   'https://yourplatform.com/webhooks/payment',
        'redirect_url': 'https://yourplatform.com/payment/success',
    }).json()

    Payment.objects.create(
        order_id=order_id, user_id=request.user.id,
        description=data['description'], amount=data['amount'],
        prepay_id=resp.get('prepay_id'), checkout_url=resp.get('checkout_url'),
        status='PENDING', expires_at=datetime.now() + timedelta(minutes=120),
    )
    return JsonResponse({'checkout_url': resp['checkout_url'], 'order_id': order_id})
```

**PHP / Laravel**
```php
public function checkout(Request $request)
{
    $orderId = 'ORD' . time();

    $resp = Http::post('https://api.scolapp.com/payment/create', [
        'amount'       => $request->amount,
        'title'        => $request->description,
        'order_id'     => $orderId,
        'notify_url'   => 'https://yourplatform.com/webhooks/payment',
        'redirect_url' => 'https://yourplatform.com/payment/success',
    ])->json();

    Payment::create([
        'order_id'     => $orderId,
        'user_id'      => auth()->id(),
        'description'  => $request->description,
        'amount'       => $request->amount,
        'prepay_id'    => $resp['prepay_id'],
        'checkout_url' => $resp['checkout_url'],
        'status'       => 'PENDING',
        'expires_at'   => now()->addMinutes(120),
    ]);

    return response()->json(['checkout_url' => $resp['checkout_url']]);
}
```

---

### Step 2 — Handle webhook on your server

D-Money calls your `notify_url` when payment status changes.
Always verify with `/payment/query` before updating your database.

**JavaScript / Node.js**
```javascript
app.post('/webhooks/payment', async (req, res) => {
  const { merch_order_id, payment_order_id, trade_status, trans_end_time, callback_info } = req.body;

  // Idempotency — skip if already processed
  const existing = await db.query(
    `SELECT id FROM payment_logs WHERE order_id=? AND type='payment_notification' LIMIT 1`,
    [merch_order_id]
  );
  if (existing.length > 0) {
    return res.json({ returnCode: 'SUCCESS', returnMsg: 'OK' });
  }

  // Always verify with /payment/query before updating DB
  const verify = await fetch('https://api.scolapp.com/payment/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ merch_order_id }),
  });
  const result = await verify.json();
  const status = result.trade_status;

  if (status === 'SUCCESS' || trade_status === 'Completed') {
    await db.query(
      `UPDATE payments SET status='SUCCESS', trade_no=?, paid_at=NOW(), notified_at=NOW()
       WHERE order_id=?`,
      [payment_order_id, merch_order_id]
    );
    await onPaymentSuccess(merch_order_id); // your business logic
  } else if (['FAILED', 'EXPIRED', 'Failure', 'Expired'].includes(trade_status)) {
    await db.query(
      `UPDATE payments SET status=?, notified_at=NOW() WHERE order_id=?`,
      [status, merch_order_id]
    );
  }

  // Log the event
  await db.query(
    `INSERT INTO payment_logs (order_id, message, data, type) VALUES (?,?,?,?)`,
    [merch_order_id, `Webhook received: ${trade_status}`, JSON.stringify(req.body), 'payment_notification']
  );

  // Always return this — required by D-Money
  res.json({ returnCode: 'SUCCESS', returnMsg: 'OK' });
});
```

**Python / Django**
```python
@csrf_exempt
def payment_webhook(request):
    body     = json.loads(request.body)
    order_id = body.get('merch_order_id')
    trade_no = body.get('payment_order_id')
    status   = body.get('trade_status')

    # Verify with /payment/query
    result = requests.post('https://api.scolapp.com/payment/query',
                           json={'merch_order_id': order_id}, timeout=15).json()
    verified_status = result.get('trade_status')

    if verified_status == 'SUCCESS' or status == 'Completed':
        Payment.objects.filter(order_id=order_id).update(
            status='SUCCESS', trade_no=trade_no,
            paid_at=datetime.utcnow(), notified_at=datetime.utcnow(),
        )
        on_payment_success(order_id)  # your business logic

    elif status in ('Failure', 'Expired'):
        Payment.objects.filter(order_id=order_id).update(
            status=status.upper(), notified_at=datetime.utcnow(),
        )

    PaymentLog.objects.create(
        order_id=order_id,
        message=f'Webhook received: {status}',
        data=body, type='payment_notification',
    )

    return JsonResponse({'returnCode': 'SUCCESS', 'returnMsg': 'OK'})
```

**PHP / Laravel**
```php
public function webhook(Request $request)
{
    $orderId   = $request->merch_order_id;
    $tradeNo   = $request->payment_order_id;
    $status    = $request->trade_status;

    // Verify with /payment/query
    $result = Http::post('https://api.scolapp.com/payment/query',
                         ['merch_order_id' => $orderId])->json();
    $verifiedStatus = $result['trade_status'] ?? null;

    if ($verifiedStatus === 'SUCCESS' || $status === 'Completed') {
        Payment::where('order_id', $orderId)->update([
            'status'       => 'SUCCESS',
            'trade_no'     => $tradeNo,
            'paid_at'      => now(),
            'notified_at'  => now(),
        ]);
        $this->onPaymentSuccess($orderId); // your business logic

    } elseif (in_array($status, ['Failure', 'Expired'])) {
        Payment::where('order_id', $orderId)->update([
            'status'      => strtoupper($status),
            'notified_at' => now(),
        ]);
    }

    PaymentLog::create([
        'order_id' => $orderId,
        'message'  => "Webhook received: {$status}",
        'data'     => $request->all(),
        'type'     => 'payment_notification',
    ]);

    return response()->json(['returnCode' => 'SUCCESS', 'returnMsg' => 'OK']);
}
```

---

### Step 3 — Poll notification log (alternative to webhook)

Use this if you cannot expose a public webhook URL (e.g. during local development).

**JavaScript**
```javascript
async function waitForPayment(orderId, onSuccess, onFailed) {
  const poll = setInterval(async () => {
    const res  = await fetch(
      `https://api.scolapp.com/payment/notify/${orderId}`
    );
    const data = await res.json();

    if (data.latest_status === 'Completed') {
      // Verify before updating your DB
      const verify = await fetch('https://api.scolapp.com/payment/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ merch_order_id: orderId }),
      });
      const result = await verify.json();

      if (result.trade_status === 'SUCCESS') {
        clearInterval(poll);
        onSuccess(result);
      }
    } else if (['Failure', 'Expired'].includes(data.latest_status)) {
      clearInterval(poll);
      onFailed(data);
    }
  }, 3000); // poll every 3 seconds

  // Stop after 10 minutes
  setTimeout(() => clearInterval(poll), 10 * 60 * 1000);
}

// Usage
const { checkout_url, order_id } = await createPayment(amount, title);
window.open(checkout_url, '_blank');
waitForPayment(
  order_id,
  (r) => console.log('Payment successful!', r),
  (r) => console.log('Payment failed.', r),
);
```

---

## Error Responses

```json
{ "detail": "Error description here" }
```

| HTTP | Meaning | Fix |
|------|---------|-----|
| `400` | Missing required field | Check request body |
| `422` | Validation failed | Check field constraints (amount, order_id format, URL must be https) |
| `429` | Rate limited (30 req/min) | Wait `Retry-After` seconds |
| `502` | D-Money gateway error | Check `detail` message, retry |

**Rate limit headers on every response:**
```
X-RateLimit-Limit: 30
X-RateLimit-Remaining: 27
X-RateLimit-Reset: 1712500860
```

---

## Security

- HTTPS enforced — HTTP auto-redirects to HTTPS (301)
- TLS 1.2 / 1.3 only (configured in Nginx)
- Rate limited: 30 requests/minute per IP
- Security headers on every response (HSTS, X-Frame-Options, CSP, etc.)
- D-Money webhook notifications are idempotent (duplicate `payment_order_id` ignored)
- RSA private key loaded from `DMONEY_PRIVATE_KEY_B64` env var — never hardcoded
- MySQL connection pool with `pool_pre_ping=True` (auto-reconnect on stale connections)
- Fail2Ban active on VPS (bans IPs after repeated failures)

---

## STEP 1 — Run Locally

### 1a. Create MySQL database
```sql
CREATE DATABASE scolapp_payments CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'scolapp'@'localhost' IDENTIFIED BY 'your_password';
GRANT ALL PRIVILEGES ON scolapp_payments.* TO 'scolapp'@'localhost';
FLUSH PRIVILEGES;
```

### 1b. Configure .env
```bash
cp .env.example .env
nano .env   # set DATABASE_URL and DMONEY_PRIVATE_KEY_B64
```

### 1c. Start server
```bash
bash run_local.sh
```

Server starts at `http://localhost:8000`. Tables created automatically on first run.

### 1d. Test endpoints
```bash
# Health
curl http://localhost:8000/health

# Create payment
curl -X POST http://localhost:8000/payment/create \
  -H "Content-Type: application/json" \
  -d '{"amount": 1000, "title": "Test Payment", "language": "en"}'

# Query order
curl -X POST http://localhost:8000/payment/query \
  -H "Content-Type: application/json" \
  -d '{"merch_order_id": "ORD2026040700001"}'

# Check notification log
curl http://localhost:8000/payment/notify/ORD2026040700001
```

Interactive Swagger UI: http://localhost:8000/docs

---

## STEP 2 — Push to GitHub

```bash
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/scolapp-api.git
git push -u origin main
```

`.env` is in `.gitignore` — it will never be pushed.

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
nano .env   # paste real credentials + MySQL DATABASE_URL
```

### 3d. Set up MySQL on the VPS
```bash
apt install mysql-server -y
mysql -u root -p
```
```sql
CREATE DATABASE scolapp_payments CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'scolapp'@'localhost' IDENTIFIED BY 'your_password';
GRANT ALL PRIVILEGES ON scolapp_payments.* TO 'scolapp'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

### 3e. Add DNS record
In your domain registrar:
```
Type: A  |  Name: api  |  Value: YOUR_VPS_IP  |  TTL: 300
```

### 3f. Run deploy script
```bash
chmod +x deploy.sh
bash deploy.sh
```

The script installs: Python, Nginx, Certbot, UFW, Fail2Ban.
It configures HTTPS automatically via Let's Encrypt.

### 3g. Test live
```bash
curl https://api.scolapp.com/health
```

---

## STEP 4 — Update VPS After Code Changes

On your local machine:
```bash
git add .
git commit -m "describe your change"
git push origin main
```

On your VPS:
```bash
ssh root@YOUR_VPS_IP
cd /root/scolapp_api
bash update.sh
```

---

## Useful VPS Commands

```bash
journalctl -u scolapp-api -f            # live application logs
systemctl restart scolapp-api           # restart the API
systemctl status scolapp-api            # check status
nano /var/www/scolapp_api/.env          # edit credentials
certbot renew                           # renew SSL certificate
mysql -u scolapp -p scolapp_payments    # connect to database
```

**Check saved notifications in MySQL:**
```sql
SELECT merch_order_id, trade_status, total_amount, received_at
FROM payment_notifications
ORDER BY received_at DESC
LIMIT 20;
```

**Check audit log:**
```sql
SELECT merch_order_id, message, type, created_at
FROM notification_logs
ORDER BY created_at DESC
LIMIT 50;
```

---

## Dependencies

```
fastapi==0.111.0
uvicorn[standard]==0.29.0
pydantic==2.7.1
cryptography==42.0.5
requests==2.31.0
urllib3==2.2.1
python-dotenv==1.0.1
sqlalchemy==2.0.30
PyMySQL==1.1.1
```

---

## Support

**Email:** api@scolapp.com
**Docs:** https://api.scolapp.com/docs
**Website:** https://scolapp.com
