"""
D-Money Payment Gateway Interface
Base URL:  https://api.scolapp.com
Security:  HTTPS only, rate limiting, security headers, input validation
Auth:      None required (open API secured at transport layer)

Endpoints:
  GET  /health                  — health check
  POST /payment/create          — create payment, get checkout_url
  POST /payment/query           — query payment status from D-Money
  POST /payment/notify          — webhook D-Money calls on status change
  GET  /payment/notify/{id}     — third party can check their notify logs
  GET  /payment/success         — success landing page
  GET  /payment/failed          — failed landing page
"""

import time
import logging
import hashlib
from collections import defaultdict
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, status, Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
import re

from dmoney_gateway import DmoneyPaymentGateway

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("DmoneyAPI")

# ── In-memory notification log (last 500 webhooks) ────────────────────────────
# Third parties can call GET /payment/notify/{order_id} to see what D-Money sent
_notify_log: dict = {}   # order_id → list of payloads
MAX_NOTIFY_LOG = 500

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Scolapp D-Money Payment Gateway",
    description="""
## Overview
Middleware between your platform and the **D-Money payment gateway** in Djibouti.

All D-Money authentication, RSA signing, and token management is handled internally.
Your platform makes simple HTTPS calls to this API.

## Base URL
```
https://api.scolapp.com
```

## Security
- HTTPS enforced — HTTP auto-redirects to HTTPS
- Rate limited: 30 requests/minute per IP
- All inputs validated and sanitised
- Security headers on every response

## How it works
1. **POST /payment/create** → get `checkout_url`
2. Open `checkout_url` in customer browser → D-Money payment page appears
3. Customer pays → D-Money calls **POST /payment/notify** (your `notify_url`)
4. **POST /payment/query** → verify final payment status
    """,
    version="1.0.0",
    contact={"name": "Scolapp", "url": "https://scolapp.com"},
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── Middleware ────────────────────────────────────────────────────────────────

# Force HTTPS
app.add_middleware(HTTPSRedirectMiddleware)

# Only accept known hosts
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["api.scolapp.com", "localhost", "127.0.0.1"],
)

# CORS — open
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Accept"],
)

# Security headers
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"]    = "nosniff"
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["X-XSS-Protection"]          = "1; mode=block"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"]   = "default-src 'none'; frame-ancestors 'none'"
    response.headers["Cache-Control"]             = "no-store"
    response.headers.pop("server", None)
    return response

# Rate limiter
RATE_LIMIT          = 30
RATE_WINDOW_SECONDS = 60
_rate_store: dict   = defaultdict(list)

@app.middleware("http")
async def rate_limiter(request: Request, call_next):
    if request.url.path in ("/health", "/payment/notify"):
        return await call_next(request)

    forwarded = request.headers.get("X-Forwarded-For")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "unknown"
    )

    now = time.time()
    window_start = now - RATE_WINDOW_SECONDS
    _rate_store[ip] = [t for t in _rate_store[ip] if t > window_start]

    if len(_rate_store[ip]) >= RATE_LIMIT:
        logger.warning(f"Rate limit exceeded — IP={ip[:8]}...")
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": f"Rate limit exceeded. Max {RATE_LIMIT} requests/minute."},
            headers={"Retry-After": str(RATE_WINDOW_SECONDS)},
        )

    _rate_store[ip].append(now)
    response = await call_next(request)
    remaining = RATE_LIMIT - len(_rate_store[ip])
    response.headers["X-RateLimit-Limit"]     = str(RATE_LIMIT)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"]     = str(int(window_start + RATE_WINDOW_SECONDS))
    return response

# Request logger
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start     = time.time()
    forwarded = request.headers.get("X-Forwarded-For")
    ip        = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "unknown"
    )
    ip_hash   = hashlib.sha256(ip.encode()).hexdigest()[:12]
    response  = await call_next(request)
    ms        = round((time.time() - start) * 1000)
    logger.info(f"{request.method} {request.url.path} {response.status_code} {ms}ms ip={ip_hash}")
    return response

# ── Gateway singleton ─────────────────────────────────────────────────────────
gateway: Optional[DmoneyPaymentGateway] = None

@app.on_event("startup")
def startup():
    global gateway
    gateway = DmoneyPaymentGateway()
    logger.info("Gateway ready — https://api.scolapp.com")

# ── Validators ────────────────────────────────────────────────────────────────
_ORDER_ID_RE = re.compile(r'^[A-Za-z0-9]{1,64}$')
_URL_RE      = re.compile(r'^https://.+')
_TIMEOUT_RE  = re.compile(r'^\d+[mh]$')

def _safe(v: str, n: int = 256) -> str:
    return str(v).strip()[:n]

# ── Models ────────────────────────────────────────────────────────────────────

class CreatePaymentRequest(BaseModel):
    amount: float = Field(
        ..., example=5000, gt=0, le=10_000_000,
        description="Amount in DJF. Must be positive and ≤ 10,000,000.",
    )
    title: str = Field(
        ..., example="Scolarite Trimestre 1 — Ahmed Ali",
        min_length=1, max_length=128,
        description="Payment description shown on D-Money page (max 128 chars).",
    )
    order_id: Optional[str] = Field(
        None, example="ORD2026040700001",
        description="Your unique order ID (alphanumeric, max 64 chars). Auto-generated if omitted.",
    )
    currency: str = Field("DJF", example="DJF", description="Only DJF supported.")
    timeout:  str = Field("120m", example="120m", description="Expiry: 30m, 60m, 120m, 2h.")
    notify_url:   Optional[str] = Field(
        None, example="https://yourplatform.com/webhooks/payment",
        description="Your webhook URL — D-Money POSTs here on payment status change. Must be HTTPS.",
    )
    redirect_url: Optional[str] = Field(
        None, example="https://yourplatform.com/payment/success",
        description="Customer redirect URL after payment. Must be HTTPS.",
    )
    language: str = Field("en", example="en", description="'en' or 'fr'.")

    @field_validator("order_id")
    @classmethod
    def val_order_id(cls, v):
        if v is None: return v
        v = _safe(v)
        if not _ORDER_ID_RE.match(v):
            raise ValueError("order_id must be alphanumeric only, max 64 chars")
        return v

    @field_validator("title")
    @classmethod
    def val_title(cls, v): return _safe(v, 128)

    @field_validator("currency")
    @classmethod
    def val_currency(cls, v):
        if v.upper() != "DJF": raise ValueError("Only DJF supported")
        return "DJF"

    @field_validator("timeout")
    @classmethod
    def val_timeout(cls, v):
        v = _safe(v, 10)
        if not _TIMEOUT_RE.match(v): raise ValueError("timeout must be e.g. 120m or 2h")
        return v

    @field_validator("notify_url", "redirect_url")
    @classmethod
    def val_url(cls, v):
        if v is None: return v
        v = _safe(v, 512)
        if not _URL_RE.match(v): raise ValueError("URL must start with https://")
        return v

    @field_validator("language")
    @classmethod
    def val_language(cls, v):
        if v not in ("en", "fr"): raise ValueError("language must be 'en' or 'fr'")
        return v

    class Config:
        json_schema_extra = {"example": {
            "amount": 5000, "title": "Scolarite Trimestre 1 — Ahmed Ali",
            "order_id": "ORD2026040700001",
            "notify_url": "https://yourplatform.com/webhooks/payment",
            "redirect_url": "https://yourplatform.com/payment/success",
            "language": "en",
        }}


class CreatePaymentResponse(BaseModel):
    success:      bool
    order_id:     str
    prepay_id:    Optional[str]
    checkout_url: Optional[str]
    amount:       float
    currency:     str


class QueryOrderRequest(BaseModel):
    merch_order_id: Optional[str] = Field(
        None, example="ORD2026040700001",
        description="Your platform order ID.",
    )
    trade_no: Optional[str] = Field(
        None, example="DM20260407123456",
        description="D-Money trade number (from notify webhook or create response).",
    )

    @field_validator("merch_order_id", "trade_no")
    @classmethod
    def val_ids(cls, v):
        return _safe(v, 64) if v else v

    class Config:
        json_schema_extra = {"example": {"merch_order_id": "ORD2026040700001"}}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"], summary="Health check")
def health():
    """Check whether the gateway is running."""
    return {
        "status":        "ok",
        "gateway_ready": gateway is not None,
        "base_url":      "https://api.scolapp.com",
        "version":       "1.0.0",
    }


@app.post(
    "/payment/create",
    response_model=CreatePaymentResponse,
    tags=["Payment"],
    summary="Create a payment order",
    description="""
Creates a D-Money payment order and returns a `checkout_url`.

Open `checkout_url` in the customer's browser to show the D-Money payment page.

### JavaScript
```javascript
const res = await fetch('https://api.scolapp.com/payment/create', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    amount: 5000, title: 'Scolarite Trimestre 1',
    order_id: 'ORD001',
    notify_url: 'https://yourplatform.com/webhooks/payment',
    redirect_url: 'https://yourplatform.com/payment/success',
  })
});
const data = await res.json();
window.open(data.checkout_url, '_blank');
```

### Python
```python
import requests
data = requests.post('https://api.scolapp.com/payment/create', json={
    'amount': 5000, 'title': 'Scolarite Trimestre 1',
    'order_id': 'ORD001',
    'notify_url': 'https://yourplatform.com/webhooks/payment',
    'redirect_url': 'https://yourplatform.com/payment/success',
}).json()
print(data['checkout_url'])
```

### PHP
```php
$res = Http::post('https://api.scolapp.com/payment/create', [
    'amount' => 5000, 'title' => 'Scolarite Trimestre 1',
    'order_id' => 'ORD001',
    'notify_url' => 'https://yourplatform.com/webhooks/payment',
    'redirect_url' => 'https://yourplatform.com/payment/success',
])->json();
header('Location: ' . $res['checkout_url']);
```
    """,
)
def create_payment(req: CreatePaymentRequest):
    try:
        result = gateway.create_payment(
            amount=req.amount,
            title=req.title,
            order_id=req.order_id,
            currency=req.currency,
            timeout=req.timeout,
            notify_url=req.notify_url     or "https://api.scolapp.com/payment/notify",
            redirect_url=req.redirect_url or "https://api.scolapp.com/payment/success",
            language=req.language,
        )
        logger.info(f"Payment created — order={result['order_id']} amount={req.amount}")
        return result
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"create_payment error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.post(
    "/payment/query",
    tags=["Payment"],
    summary="Query payment status",
    description="""
Query the **live status** of a payment order directly from D-Money.

Provide `merch_order_id` (your order ID) or `trade_no` (D-Money's trade number).

Always call this endpoint to **verify** a payment before marking it as paid in
your database — do not rely on the webhook payload alone.

### Payment statuses

| Status | Meaning | Your action |
|--------|---------|-------------|
| `SUCCESS` | Payment completed | Mark order paid, run business logic |
| `PENDING` | Not yet completed | Wait or keep polling |
| `FAILED` | Payment rejected | Allow customer to retry |
| `EXPIRED` | Link expired | Create a new payment |

### JavaScript
```javascript
const res = await fetch('https://api.scolapp.com/payment/query', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ merch_order_id: 'ORD001' })
});
const data = await res.json();
console.log(data.trade_status); // SUCCESS | PENDING | FAILED | EXPIRED
```

### Python
```python
import requests
data = requests.post('https://api.scolapp.com/payment/query',
    json={'merch_order_id': 'ORD001'}).json()
print(data['trade_status'])
```

### PHP
```php
$data = Http::post('https://api.scolapp.com/payment/query',
    ['merch_order_id' => 'ORD001'])->json();
echo $data['trade_status'];
```
    """,
)
def query_order(req: QueryOrderRequest):
    if not req.merch_order_id and not req.trade_no:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of: merch_order_id or trade_no"
        )
    try:
        data = gateway.query_order(
            merch_order_id=req.merch_order_id,
            trade_no=req.trade_no,
        )
        logger.info(f"Order queried — {req.merch_order_id or req.trade_no}")
        return data
    except Exception as e:
        logger.error(f"query_order error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.post(
    "/payment/notify",
    tags=["Webhooks"],
    summary="D-Money payment notification webhook",
    description="""
**D-Money calls this endpoint automatically** when payment status changes.

You do **not** call this yourself.

### How to use this for your platform

When calling `/payment/create`, pass your **own server URL** as `notify_url`:
```json
{
  "notify_url": "https://yourplatform.com/webhooks/payment"
}
```

D-Money will then POST directly to your server instead.

### If you use the default notify_url

If you do not pass a `notify_url`, D-Money calls this endpoint.
You can then retrieve the notification log by calling:
```
GET https://api.scolapp.com/payment/notify/{order_id}
```

### D-Money notification payload
```json
{
  "merch_order_id": "ORD2026040700001",
  "trade_no":       "DM20260407123456",
  "trade_status":   "SUCCESS",
  "total_amount":   "5000",
  "trans_currency": "DJF",
  "pay_time":       "2026-04-07 14:32:10",
  "appid":          "1598852445107200",
  "merch_code":     "200012"
}
```

### Your webhook server must respond with
```json
{ "returnCode": "SUCCESS", "returnMsg": "OK" }
```
    """,
)
async def payment_notify(request: Request):
    """
    D-Money POSTs here when payment status changes.
    Logs the payload. Third parties can read it via GET /payment/notify/{order_id}.
    Must return { returnCode: SUCCESS, returnMsg: OK } — always.
    """
    try:
        body     = await request.json()
        order_id = body.get("merch_order_id", "unknown")
        status   = body.get("trade_status",   "unknown")

        logger.info(f"D-Money notify: order={order_id} status={status}")

        # Store in notify log so third parties can poll it
        if order_id not in _notify_log:
            _notify_log[order_id] = []
        _notify_log[order_id].append(body)

        # Trim log size
        if len(_notify_log) > MAX_NOTIFY_LOG:
            oldest = list(_notify_log.keys())[0]
            del _notify_log[oldest]

    except Exception as e:
        logger.warning(f"Notify parse error: {e}")

    # D-Money requires this exact response — always
    return {"returnCode": "SUCCESS", "returnMsg": "OK"}


@app.get(
    "/payment/notify/{order_id}",
    tags=["Webhooks"],
    summary="Get D-Money notification log for an order",
    description="""
Retrieve the D-Money webhook notifications received for a specific order.

Useful when you use the default `notify_url` (i.e. you did not pass your own).
Poll this endpoint to detect when D-Money has confirmed a payment.

### JavaScript — poll until notified
```javascript
async function waitForNotification(orderId) {
  const poll = setInterval(async () => {
    const res  = await fetch(
      `https://api.scolapp.com/payment/notify/${orderId}`
    );
    const data = await res.json();

    if (data.notifications.length > 0) {
      const latest = data.notifications[data.notifications.length - 1];
      console.log('Payment status:', latest.trade_status);

      if (latest.trade_status === 'SUCCESS') {
        clearInterval(poll);
        // Verify and update your database
        const verify = await fetch('https://api.scolapp.com/payment/query', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ merch_order_id: orderId }),
        });
        const result = await verify.json();
        if (result.trade_status === 'SUCCESS') {
          updateYourDatabase(orderId, 'SUCCESS');
        }
        clearInterval(poll);
      }
    }
  }, 3000); // check every 3 seconds

  // Stop after 10 minutes
  setTimeout(() => clearInterval(poll), 10 * 60 * 1000);
}
```
    """,
)
def get_notify_log(
    order_id: str = Path(..., example="ORD2026040700001",
                         description="Your order ID")
):
    """Get D-Money webhook notifications received for this order_id."""
    notifications = _notify_log.get(order_id, [])
    return {
        "order_id":      order_id,
        "notifications": notifications,
        "count":         len(notifications),
        "latest_status": notifications[-1].get("trade_status") if notifications else None,
    }


@app.get(
    "/payment/success",
    tags=["Pages"],
    response_class=HTMLResponse,
    summary="Payment success landing page",
)
def payment_success():
    return HTMLResponse(_page("✅", "Payment Successful",
                              "Your payment has been processed successfully via D-Money.",
                              "#059669"))


@app.get(
    "/payment/failed",
    tags=["Pages"],
    response_class=HTMLResponse,
    summary="Payment failed landing page",
)
def payment_failed():
    return HTMLResponse(_page("❌", "Payment Failed",
                              "Something went wrong with your payment. Please try again.",
                              "#dc2626"))


def _page(icon, title, message, color):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>{title}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{min-height:100vh;display:flex;align-items:center;justify-content:center;
         background:#f4f6f9;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
    .card{{background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.1);
           padding:48px 40px;text-align:center;max-width:400px;width:100%;
           border-top:4px solid {color}}}
    .icon{{font-size:52px;margin-bottom:16px}}
    h2{{font-size:22px;font-weight:700;color:#111827;margin-bottom:10px}}
    p{{font-size:14px;color:#6b7280;line-height:1.6}}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h2>{title}</h2>
    <p>{message}</p>
  </div>
</body>
</html>"""