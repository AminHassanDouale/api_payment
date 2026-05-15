"""D-Money Multi-Tenant Payment API Platform.

Architecture
------------
  Third-Party                  This Platform                     D-Money
  (enterprise)  ── token ──▶   /api/payments  ── signed call ─▶  preOrder
                              (lookup creds,
                               sign with their key)
                              ◀── checkout_url ──
       ◀───── checkout_url ───
                                                                 (user pays)
                              ◀──── webhook ─────────────────────  notify
                              (forward to
                               tp.notify_url) ──────▶  Third-Party

Auth
----
  * Admin    : email/password → JWT (Authorization: Bearer <jwt>)
  * 3rd-party: lifetime token  (Authorization: Bearer tp_live_xxx)

First admin can self-register when no admin exists yet; subsequent
admin creation requires an existing admin's JWT.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests
from dotenv import load_dotenv
load_dotenv()

from fastapi import (
    BackgroundTasks, Depends, FastAPI, HTTPException, Path, Query, Request, status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from db import engine, get_db, SessionLocal
from models import (
    ActivityLog, Admin, Base, NotificationLog, Payment, PaymentNotification,
    ThirdParty, WebhookDelivery,
)
from activity import Actions, log_activity
from auth import (
    create_admin_jwt, get_current_admin, get_current_third_party,
    hash_password, verify_password,
)
from crypto_utils import decrypt, encrypt, generate_api_token
from dmoney_gateway import DmoneyPaymentGateway

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("DmoneyAPI")

_ENV               = os.getenv("ENVIRONMENT", "production").lower()
PLATFORM_BASE_URL  = os.getenv("PLATFORM_BASE_URL", "https://api.scolapp.com").rstrip("/")
PLATFORM_NOTIFY_URL = f"{PLATFORM_BASE_URL}/payment/notify"


# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="D-Money Multi-Tenant Payment API",
    description="Platform that lets third-party enterprises take D-Money payments "
                "by sending only amount + description against a lifetime API token.",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── Middleware ────────────────────────────────────────────────────────────────
if _ENV != "development":
    app.add_middleware(HTTPSRedirectMiddleware)

_TRUSTED_HOSTS = ["api.scolapp.com"]
if _ENV == "development":
    _TRUSTED_HOSTS += ["localhost", "localhost:8000", "127.0.0.1", "127.0.0.1:8000", "*"]
else:
    _TRUSTED_HOSTS += ["localhost", "127.0.0.1"]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_TRUSTED_HOSTS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"]        = "DENY"
    response.headers["X-XSS-Protection"]       = "1; mode=block"
    response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    if _ENV != "development":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    path = request.url.path
    if path in ("/payment.html", "/admin.html"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; connect-src *"
        )
    elif path in ("/docs", "/redoc", "/openapi.json") or path.startswith(("/docs", "/redoc")):
        # Swagger UI / ReDoc load assets from jsdelivr CDN and fetch openapi.json
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fastapi.tiangolo.com; "
            "img-src   'self' data: https://cdn.jsdelivr.net https://fastapi.tiangolo.com; "
            "font-src  'self' data: https://cdn.jsdelivr.net; "
            "connect-src 'self'; "
            "worker-src blob:"
        )
    else:
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    response.headers["Cache-Control"] = "no-store"
    try:
        del response.headers["server"]
    except KeyError:
        pass
    return response


RATE_LIMIT          = 60
RATE_WINDOW_SECONDS = 60
_rate_store: dict   = defaultdict(list)


@app.middleware("http")
async def rate_limiter(request: Request, call_next):
    if request.url.path in ("/health", "/payment/notify"):
        return await call_next(request)

    fwd = request.headers.get("X-Forwarded-For")
    ip = (fwd.split(",")[0].strip() if fwd
          else (request.client.host if request.client else "unknown"))

    now = time.time()
    window_start = now - RATE_WINDOW_SECONDS
    _rate_store[ip] = [t for t in _rate_store[ip] if t > window_start]

    if len(_rate_store[ip]) >= RATE_LIMIT:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": f"Rate limit exceeded. Max {RATE_LIMIT}/min."},
            headers={"Retry-After": str(RATE_WINDOW_SECONDS)},
        )
    _rate_store[ip].append(now)

    response = await call_next(request)
    response.headers["X-RateLimit-Limit"]     = str(RATE_LIMIT)
    response.headers["X-RateLimit-Remaining"] = str(RATE_LIMIT - len(_rate_store[ip]))
    return response


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    fwd   = request.headers.get("X-Forwarded-For")
    ip    = (fwd.split(",")[0].strip() if fwd
             else (request.client.host if request.client else "unknown"))
    ip_h  = hashlib.sha256(ip.encode()).hexdigest()[:12]
    resp  = await call_next(request)
    ms    = round((time.time() - start) * 1000)
    logger.info(f"{request.method} {request.url.path} {resp.status_code} {ms}ms ip={ip_h}")
    return resp


def _is_already_exists(err: Exception) -> bool:
    """Detect MySQL/SQLite 'object already exists' errors so concurrent
    workers don't all crash on first boot."""
    msg = str(err).lower()
    return (
        "already exists" in msg
        or "duplicate column" in msg
        or "duplicate key name" in msg
        or "(1050," in msg          # MySQL: table already exists
        or "(1060," in msg          # MySQL: duplicate column
        or "(1061," in msg          # MySQL: duplicate key/index
    )


def _migrate_add_missing_columns():
    """Light migration: add columns we introduced after first deploy.
    SQLAlchemy.create_all only creates missing TABLES, not missing COLUMNS."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "webhook_deliveries" not in insp.get_table_names():
        return  # create_all will handle it
    cols = {c["name"] for c in insp.get_columns("webhook_deliveries")}
    statements = []
    if "request_body" not in cols:
        statements.append("ALTER TABLE webhook_deliveries ADD COLUMN request_body TEXT NULL")
    if "order_id" not in cols:
        statements.append("ALTER TABLE webhook_deliveries ADD COLUMN order_id VARCHAR(64) NULL")
        statements.append("CREATE INDEX ix_webhook_deliveries_order_id ON webhook_deliveries (order_id)")
    for stmt in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
            logger.info(f"Migration: {stmt}")
        except Exception as e:
            if _is_already_exists(e):
                logger.info(f"Migration already applied: {stmt}")
            else:
                logger.warning(f"Migration skipped ({stmt}): {e}")


# Defaults tuned for "buyer doesn't click Return to merchant" — payment
# lands in the DB within ~5–10s of actually completing on D-Money.
POLL_INTERVAL_SEC = int(os.getenv("PENDING_POLL_INTERVAL_SEC", "5"))
POLL_MIN_AGE_SEC  = int(os.getenv("PENDING_POLL_MIN_AGE_SEC",  "5"))
POLL_MAX_AGE_HRS  = int(os.getenv("PENDING_POLL_MAX_AGE_HRS",  "24"))


def _reconcile_one_payment(db, p: "Payment") -> str:
    """Hit D-Money queryOrder for one Payment row and update status if changed.
    Returns the new status (or the old one if no change). Raises on D-Money error."""
    tp = db.query(ThirdParty).filter_by(
        id=p.third_party_id, is_active=True
    ).first()
    if not tp:
        raise RuntimeError(f"no active TP for tp_id={p.third_party_id}")
    gateway = DmoneyPaymentGateway.from_third_party(tp, decrypt)
    data = gateway.query_order(merch_order_id=p.merch_order_id)
    biz = data.get("biz_content") or {}
    if isinstance(biz, str):
        try: biz = json.loads(biz)
        except Exception: biz = {}
    raw_ts = biz.get("trade_status") or ""
    new_status = _normalize_status(raw_ts)
    logger.info(
        f"queryOrder {p.merch_order_id}: D-Money trade_status={raw_ts!r} "
        f"→ canonical={new_status} (was {p.status})"
    )
    if new_status and new_status != "PENDING" and new_status != p.status:
        old = p.status
        p.status = new_status
        log_activity(
            db, action=Actions.PAYMENT_STATUS_CHANGED,
            third_party_id=tp.id, actor_type="system",
            order_id=p.merch_order_id,
            description=f"Status {old} → {new_status} (poller)",
            metadata={"from": old, "to": new_status,
                      "source": "poll", "raw_trade_status": raw_ts},
        )
        db.commit()
        logger.info(f"Poller flipped {p.merch_order_id}: {old} → {new_status}")
    return p.status


def _poll_pending_payments_loop():
    """Background thread that reconciles PENDING payments against D-Money."""
    logger.info(
        f"Pending-payment poller started pid={os.getpid()} "
        f"(interval={POLL_INTERVAL_SEC}s, min_age={POLL_MIN_AGE_SEC}s, "
        f"max_age={POLL_MAX_AGE_HRS}h)"
    )
    # Stagger workers so they don't all hit D-Money at the same instant
    time.sleep(POLL_INTERVAL_SEC * (1 + (os.getpid() % 3) / 10))

    tick = 0
    while True:
        try:
            db = SessionLocal()
            try:
                # IMPORTANT: MySQL DATETIME columns are stored as naive UTC
                # by SQLAlchemy when we feed it timezone-aware datetimes.
                # We MUST compare against naive UTC here too, otherwise
                # the WHERE clause matches nothing and the poller looks
                # like it isn't running.
                now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
                cutoff = now_utc_naive - timedelta(seconds=POLL_MIN_AGE_SEC)
                floor  = now_utc_naive - timedelta(hours=POLL_MAX_AGE_HRS)
                pending = (
                    db.query(Payment)
                    .filter(
                        Payment.status == "PENDING",
                        Payment.created_at < cutoff,
                        Payment.created_at > floor,
                    )
                    .order_by(Payment.id.desc())
                    .limit(20)
                    .all()
                )
                tick += 1
                if pending or tick % 12 == 1:   # log every minute even if idle
                    logger.info(
                        f"Poller tick #{tick} pid={os.getpid()}: "
                        f"{len(pending)} PENDING payments to reconcile"
                    )
                for p in pending:
                    try:
                        _reconcile_one_payment(db, p)
                    except Exception as e:
                        logger.warning(
                            f"Poller queryOrder failed for {p.merch_order_id}: {e}"
                        )
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Pending-poll loop error: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL_SEC)


@app.on_event("startup")
def startup():
    # With --workers >1, several processes race on create_all().
    # The loser gets "table already exists" (MySQL 1050) and would crash —
    # treat that as a benign no-op since the table is there either way.
    try:
        Base.metadata.create_all(bind=engine, checkfirst=True)
    except Exception as e:
        if _is_already_exists(e):
            logger.info("Tables already exist (concurrent worker startup) — continuing")
        else:
            raise
    _migrate_add_missing_columns()
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1")
    logger.info(f"Platform ready (env={_ENV}) notify={PLATFORM_NOTIFY_URL}")

    # Background reconciler — keeps PENDING payments fresh even when
    # D-Money never POSTs the webhook (common in TEST sandboxes).
    threading.Thread(target=_poll_pending_payments_loop, daemon=True).start()


# ──────────────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────────────
_URL_RE     = re.compile(r"^https?://.+")
_ORDER_RE   = re.compile(r"^[A-Za-z0-9]{1,64}$")
_TIMEOUT_RE = re.compile(r"^\d+[mh]$")


def _safe(v: str, n: int = 256) -> str:
    return str(v).strip()[:n]


# D-Money rejects these characters in the title field (PreOrder error 49401024995).
# We strip them silently so third parties don't have to know the gateway's rules.
_DMONEY_BAD_CHARS = re.compile(r"[~`!#$%^*()\-+=|/<>?;:\"\[\]{}\\&]")


def _clean_title(s: str) -> str:
    """Make a title safe for D-Money's PreOrder validation."""
    if not s:
        return "Payment"
    cleaned = _DMONEY_BAD_CHARS.sub(" ", s)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:128] or "Payment"


# ── Admin ────────────────────────────────────────────────────────────────────
class AdminRegister(BaseModel):
    email:    EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    name:     Optional[str] = Field(None, max_length=255)


class AdminLogin(BaseModel):
    email:    EmailStr
    password: str


class AdminPublic(BaseModel):
    id:    int
    email: str
    name:  Optional[str]
    is_active: bool

    class Config:
        from_attributes = True


class AdminAuthResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    admin:        AdminPublic


# ── Third party ──────────────────────────────────────────────────────────────
class ThirdPartyCreate(BaseModel):
    name:    str = Field(..., min_length=1, max_length=255)
    email:   Optional[EmailStr] = None
    company: Optional[str] = Field(None, max_length=255)
    phone:   Optional[str] = Field(None, max_length=64)

    # D-Money credentials provided by the admin during registration
    appid:        str = Field(..., min_length=1, max_length=64)
    app_key:      str = Field(..., min_length=1, max_length=255)
    app_secret:   str = Field(..., min_length=1)
    private_key:  str = Field(..., min_length=1, description="PEM or base64-DER")
    merch_code:   str = Field(..., min_length=1, max_length=32)
    business_type: str = Field("OnlineMerchant", max_length=64)

    dmoney_base_url:          str = Field(..., description="e.g. https://pgtest.d-money.dj:38443")
    dmoney_query_base_url:    Optional[str] = None
    dmoney_checkout_base_url: Optional[str] = None

    notify_url:   str = Field(..., description="Where to forward webhook events")
    redirect_url: str = Field(..., description="Where to redirect the buyer after payment")

    @field_validator("notify_url", "redirect_url",
                     "dmoney_base_url", "dmoney_query_base_url", "dmoney_checkout_base_url")
    @classmethod
    def _val_url(cls, v):
        if v is None:
            return v
        v = _safe(v, 512)
        if not _URL_RE.match(v):
            raise ValueError("Must be a valid http(s) URL")
        return v

    @field_validator("appid")
    @classmethod
    def _val_appid(cls, v):
        v = _safe(v, 64)
        if not re.match(r"^[A-Za-z0-9_-]+$", v):
            raise ValueError("appid must be alphanumeric / _ / -")
        return v


class ThirdPartyUpdate(BaseModel):
    name:    Optional[str] = None
    email:   Optional[EmailStr] = None
    company: Optional[str] = None
    phone:   Optional[str] = None

    app_key:      Optional[str] = None
    app_secret:   Optional[str] = None
    private_key:  Optional[str] = None
    merch_code:   Optional[str] = None
    business_type: Optional[str] = None

    dmoney_base_url:          Optional[str] = None
    dmoney_query_base_url:    Optional[str] = None
    dmoney_checkout_base_url: Optional[str] = None
    notify_url:   Optional[str] = None
    redirect_url: Optional[str] = None
    is_active:    Optional[bool] = None


class ThirdPartyPublic(BaseModel):
    id:      int
    name:    str
    email:   Optional[str]
    company: Optional[str]
    phone:   Optional[str]
    appid:    str
    app_key:  str
    merch_code: str
    business_type: str
    dmoney_base_url:          str
    dmoney_query_base_url:    Optional[str]
    dmoney_checkout_base_url: Optional[str]
    notify_url:   str
    redirect_url: str
    token_preview: Optional[str]
    is_active:  bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ThirdPartyCreated(ThirdPartyPublic):
    """Same as ThirdPartyPublic but includes the raw API token (shown ONCE)."""
    api_token: str


# ── Payment (third-party API) ────────────────────────────────────────────────
class PaymentItem(BaseModel):
    amount:      float = Field(..., gt=0, le=10_000_000)
    description: str   = Field(..., min_length=1, max_length=256)


class CreatePaymentBody(BaseModel):
    # Single
    amount:      Optional[float] = Field(None, gt=0, le=10_000_000)
    description: Optional[str]   = Field(None, min_length=1, max_length=256)
    # Bulk
    items:       Optional[List[PaymentItem]] = Field(None, max_length=100)
    # Optional overrides
    title:         Optional[str] = Field(None, max_length=128)
    order_id:      Optional[str] = None
    callback_info: Optional[str] = Field(None, max_length=512)
    currency:      str = "DJF"
    language:      str = "en"
    timeout:       str = "120m"
    redirect_url:  Optional[str] = None     # one-off override

    @field_validator("order_id")
    @classmethod
    def _val_order(cls, v):
        if v is None:
            return v
        v = _safe(v, 64)
        if not _ORDER_RE.match(v):
            raise ValueError("order_id must be alphanumeric, max 64 chars")
        return v

    @field_validator("currency")
    @classmethod
    def _val_currency(cls, v):
        if v.upper() != "DJF":
            raise ValueError("Only DJF supported")
        return "DJF"

    @field_validator("language")
    @classmethod
    def _val_lang(cls, v):
        if v not in ("en", "fr"):
            raise ValueError("language must be 'en' or 'fr'")
        return v

    @field_validator("timeout")
    @classmethod
    def _val_timeout(cls, v):
        v = _safe(v, 10)
        if not _TIMEOUT_RE.match(v):
            raise ValueError("timeout must be e.g. 120m or 2h")
        return v

    @field_validator("redirect_url")
    @classmethod
    def _val_redirect(cls, v):
        if v is None:
            return v
        v = _safe(v, 512)
        if not _URL_RE.match(v):
            raise ValueError("redirect_url must be a valid http(s) URL")
        return v

    @model_validator(mode="after")
    def _xor(self):
        single = self.amount is not None and self.description is not None
        bulk   = self.items is not None and len(self.items) > 0
        if single and bulk:
            raise ValueError("Provide either (amount, description) OR items, not both")
        if not single and not bulk:
            raise ValueError("Provide (amount, description) for single OR items for bulk")
        return self


class PaymentResponse(BaseModel):
    success:      bool
    order_id:     str
    prepay_id:    Optional[str]
    checkout_url: Optional[str]
    amount:       float
    currency:     str
    title:        str
    status:       str


class PaymentDetail(BaseModel):
    order_id:     str
    prepay_id:    Optional[str]
    checkout_url: Optional[str]
    amount:       float
    currency:     str
    title:        str
    status:       str
    items:        Optional[List[PaymentItem]]
    callback_info: Optional[str]
    created_at:   datetime
    updated_at:   datetime


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _validate_private_key_or_raise(pem_or_b64: str):
    """Try to load the key — raise 422 if it fails."""
    try:
        DmoneyPaymentGateway._load_key(pem_or_b64)
    except Exception as e:
        raise HTTPException(422, f"Invalid private_key: {e}")


def _tp_to_public(tp: ThirdParty) -> dict:
    return {
        "id": tp.id, "name": tp.name, "email": tp.email,
        "company": tp.company, "phone": tp.phone,
        "appid": tp.appid, "app_key": tp.app_key,
        "merch_code": tp.merch_code, "business_type": tp.business_type,
        "dmoney_base_url": tp.dmoney_base_url,
        "dmoney_query_base_url": tp.dmoney_query_base_url,
        "dmoney_checkout_base_url": tp.dmoney_checkout_base_url,
        "notify_url": tp.notify_url, "redirect_url": tp.redirect_url,
        "token_preview": tp.token_preview,
        "is_active": tp.is_active,
        "created_at": tp.created_at, "updated_at": tp.updated_at,
    }


# ──────────────────────────────────────────────────────────────────────────────
# System
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/", tags=["System"])
def root(request: Request):
    # D-Money's TEST sandbox redirects the buyer's browser to whichever
    # URL is configured on their merchant dashboard — sometimes that's
    # the root of our domain rather than /payment/notify. If the redirect
    # has D-Money's payment fields in the query string, forward to the
    # webhook handler so the status update + forwarding still happen.
    qs = request.url.query
    params = request.query_params
    if "trade_status" in params and "merch_order_id" in params:
        logger.info(
            f"D-Money redirect intercepted at '/' for order={params.get('merch_order_id')} "
            f"status={params.get('trade_status')} — forwarding to /payment/notify"
        )
        return RedirectResponse(url=f"/payment/notify?{qs}", status_code=303)

    return {
        "service": "D-Money Multi-Tenant Payment API",
        "version": "2.0.0",
        "docs":    "/docs",
    }


@app.get("/health", tags=["System"])
def health():
    db_ok = False
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        db_ok = True
    except Exception as e:
        logger.warning(f"Health check DB failed: {e}")
    return {
        "status":              "ok",
        "environment":         _ENV,
        "database_ok":         db_ok,
        "version":             "2.0.0",
        "platform_base_url":   PLATFORM_BASE_URL,
        "platform_notify_url": PLATFORM_NOTIFY_URL,
    }


@app.get("/payment.html", include_in_schema=False)
def _payment_demo():
    path = os.path.join(os.path.dirname(__file__), "payment.html")
    return FileResponse(path, media_type="text/html")


@app.get("/admin.html", include_in_schema=False)
def _admin_ui():
    path = os.path.join(os.path.dirname(__file__), "admin.html")
    return FileResponse(path, media_type="text/html")


# ──────────────────────────────────────────────────────────────────────────────
# Admin: register + login
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/admin/register", response_model=AdminAuthResponse, tags=["Admin"])
def admin_register(
    body: AdminRegister,
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Register an admin.

    - If NO admin exists yet, this endpoint is open (bootstrap).
    - Otherwise, requires an Authorization header with an existing admin's JWT.
    """
    existing_count = db.query(Admin).count()
    if existing_count > 0:
        # Re-use the JWT dependency manually since FastAPI Depends() would have made it required
        auth = request.headers.get("Authorization", "") if request else ""
        if not auth.lower().startswith("bearer "):
            raise HTTPException(401, "Admin JWT required to create additional admins")
        # Validate JWT
        from auth import _decode_jwt
        try:
            payload = _decode_jwt(auth.split(" ", 1)[1])
            if payload.get("role") != "admin":
                raise HTTPException(403, "Admin only")
            existing_admin = db.query(Admin).filter_by(
                id=int(payload["sub"]), is_active=True
            ).first()
            if not existing_admin:
                raise HTTPException(401, "Admin not found")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(401, "Invalid admin JWT")

    if db.query(Admin).filter_by(email=body.email.lower()).first():
        raise HTTPException(409, "Email already registered")

    admin = Admin(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        name=body.name,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    logger.info(f"Admin created id={admin.id} email={admin.email}")
    log_activity(
        db, action=Actions.ADMIN_REGISTERED, actor_type="admin", actor_id=admin.id,
        description=f"Admin registered {admin.email}", request=request, commit=True,
    )

    return {
        "access_token": create_admin_jwt(admin.id, admin.email),
        "token_type":   "bearer",
        "admin":        admin,
    }


@app.post("/admin/login", response_model=AdminAuthResponse, tags=["Admin"])
def admin_login(body: AdminLogin, request: Request, db: Session = Depends(get_db)):
    admin = db.query(Admin).filter_by(email=body.email.lower()).first()
    if not admin or not admin.is_active:
        raise HTTPException(401, "Invalid credentials")
    if not verify_password(body.password, admin.password_hash):
        raise HTTPException(401, "Invalid credentials")
    log_activity(
        db, action=Actions.ADMIN_LOGIN, actor_type="admin", actor_id=admin.id,
        description=f"Admin login {admin.email}", request=request, commit=True,
    )
    return {
        "access_token": create_admin_jwt(admin.id, admin.email),
        "token_type":   "bearer",
        "admin":        admin,
    }


@app.get("/admin/me", response_model=AdminPublic, tags=["Admin"])
def admin_me(current: Admin = Depends(get_current_admin)):
    return current


# ──────────────────────────────────────────────────────────────────────────────
# Admin: Third-Party CRUD
# ──────────────────────────────────────────────────────────────────────────────
@app.post(
    "/admin/third-parties",
    response_model=ThirdPartyCreated,
    status_code=201,
    tags=["Admin / Third-Parties"],
)
def create_third_party(
    body: ThirdPartyCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    _validate_private_key_or_raise(body.private_key)

    if db.query(ThirdParty).filter_by(appid=body.appid).first():
        raise HTTPException(409, "A third party with this appid already exists")

    raw_token, token_hash, preview = generate_api_token()

    tp = ThirdParty(
        name=body.name, email=body.email, company=body.company, phone=body.phone,
        appid=body.appid, app_key=body.app_key,
        app_secret_enc=encrypt(body.app_secret),
        private_key_enc=encrypt(body.private_key),
        merch_code=body.merch_code, business_type=body.business_type,
        dmoney_base_url=body.dmoney_base_url,
        dmoney_query_base_url=body.dmoney_query_base_url,
        dmoney_checkout_base_url=body.dmoney_checkout_base_url,
        notify_url=body.notify_url, redirect_url=body.redirect_url,
        token_hash=token_hash, token_preview=preview,
    )
    db.add(tp); db.commit(); db.refresh(tp)
    logger.info(f"ThirdParty created id={tp.id} appid={tp.appid}")
    log_activity(
        db, action=Actions.THIRD_PARTY_CREATED,
        third_party_id=tp.id, actor_type="admin", actor_id=admin.id,
        description=f"Created third party '{tp.name}' (appid={tp.appid})",
        request=request, metadata={"merch_code": tp.merch_code}, commit=True,
    )

    return {**_tp_to_public(tp), "api_token": raw_token}


@app.get("/admin/third-parties", response_model=List[ThirdPartyPublic], tags=["Admin / Third-Parties"])
def list_third_parties(
    db: Session = Depends(get_db),
    _admin: Admin = Depends(get_current_admin),
    active_only: bool = Query(False),
):
    q = db.query(ThirdParty)
    if active_only:
        q = q.filter_by(is_active=True)
    return [_tp_to_public(tp) for tp in q.order_by(ThirdParty.id.desc()).all()]


@app.get("/admin/third-parties/{tp_id}", response_model=ThirdPartyPublic, tags=["Admin / Third-Parties"])
def get_third_party(
    tp_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    _admin: Admin = Depends(get_current_admin),
):
    tp = db.query(ThirdParty).filter_by(id=tp_id).first()
    if not tp:
        raise HTTPException(404, "Third party not found")
    return _tp_to_public(tp)


@app.patch("/admin/third-parties/{tp_id}", response_model=ThirdPartyPublic, tags=["Admin / Third-Parties"])
def update_third_party(
    body: ThirdPartyUpdate,
    request: Request,
    tp_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    tp = db.query(ThirdParty).filter_by(id=tp_id).first()
    if not tp:
        raise HTTPException(404, "Third party not found")

    data = body.model_dump(exclude_unset=True)

    # Validate private key if being changed
    if "private_key" in data and data["private_key"]:
        _validate_private_key_or_raise(data["private_key"])
        tp.private_key_enc = encrypt(data.pop("private_key"))
    if "app_secret" in data and data["app_secret"]:
        tp.app_secret_enc = encrypt(data.pop("app_secret"))

    changed_fields = list(data.keys())
    for field, value in data.items():
        setattr(tp, field, value)

    db.commit(); db.refresh(tp)
    logger.info(f"ThirdParty updated id={tp.id}")
    log_activity(
        db, action=Actions.THIRD_PARTY_UPDATED,
        third_party_id=tp.id, actor_type="admin", actor_id=admin.id,
        description=f"Updated fields: {', '.join(changed_fields) or '(secrets only)'}",
        request=request, metadata={"fields": changed_fields}, commit=True,
    )
    return _tp_to_public(tp)


@app.delete("/admin/third-parties/{tp_id}", tags=["Admin / Third-Parties"])
def delete_third_party(
    request: Request,
    tp_id: int = Path(..., ge=1),
    hard: bool = Query(False, description="If true, fully remove the row (DESTRUCTIVE)."),
    db: Session = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    tp = db.query(ThirdParty).filter_by(id=tp_id).first()
    if not tp:
        raise HTTPException(404, "Third party not found")
    name, appid = tp.name, tp.appid
    if hard:
        # Log first (FK will null out third_party_id on log? No — we keep the log; orphaned tp_id is fine)
        log_activity(
            db, action=Actions.THIRD_PARTY_DELETED,
            third_party_id=tp.id, actor_type="admin", actor_id=admin.id,
            description=f"Hard-deleted third party '{name}' (appid={appid})",
            request=request, commit=True,
        )
        db.delete(tp); db.commit()
        return {"deleted": True, "hard": True}
    tp.is_active = False
    db.commit()
    log_activity(
        db, action=Actions.THIRD_PARTY_DISABLED,
        third_party_id=tp.id, actor_type="admin", actor_id=admin.id,
        description=f"Disabled third party '{name}' (appid={appid})",
        request=request, commit=True,
    )
    return {"deleted": True, "hard": False}


@app.post("/admin/third-parties/{tp_id}/rotate-token", tags=["Admin / Third-Parties"])
def rotate_token(
    request: Request,
    tp_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    tp = db.query(ThirdParty).filter_by(id=tp_id).first()
    if not tp:
        raise HTTPException(404, "Third party not found")
    raw, h, preview = generate_api_token()
    tp.token_hash, tp.token_preview = h, preview
    db.commit()
    logger.info(f"Token rotated for third party id={tp.id}")
    log_activity(
        db, action=Actions.TOKEN_ROTATED,
        third_party_id=tp.id, actor_type="admin", actor_id=admin.id,
        description=f"Token rotated for '{tp.name}'",
        request=request, commit=True,
    )
    return {"api_token": raw, "token_preview": preview, "rotated_at": datetime.now(timezone.utc)}


@app.post("/admin/payments/{order_id}/refresh", tags=["Admin / Monitoring"])
def admin_refresh_payment(
    order_id: str = Path(...),
    db: Session = Depends(get_db),
    _admin: Admin = Depends(get_current_admin),
):
    """Force-reconcile one payment via D-Money queryOrder. Returns the
    raw D-Money response so you can see exactly what they replied with."""
    p = db.query(Payment).filter_by(merch_order_id=order_id).first()
    if not p:
        raise HTTPException(404, "Payment not found")
    try:
        tp = db.query(ThirdParty).filter_by(id=p.third_party_id, is_active=True).first()
        if not tp:
            raise HTTPException(404, "No active third party for this payment")
        gateway = DmoneyPaymentGateway.from_third_party(tp, decrypt)
        data = gateway.query_order(merch_order_id=order_id)
        biz = data.get("biz_content") or {}
        if isinstance(biz, str):
            try: biz = json.loads(biz)
            except Exception: biz = {}
        raw_ts = biz.get("trade_status") or ""
        new_status = _normalize_status(raw_ts)
        old_status = p.status
        if new_status and new_status != "PENDING" and new_status != p.status:
            p.status = new_status
            db.commit()
        return {
            "order_id":         order_id,
            "old_status":       old_status,
            "new_status":       p.status,
            "raw_trade_status": raw_ts,
            "dmoney_response":  data,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"queryOrder failed: {e}")


@app.get("/admin/payments", tags=["Admin / Monitoring"])
def admin_list_payments(
    db: Session = Depends(get_db),
    _admin: Admin = Depends(get_current_admin),
    third_party_id: Optional[int] = Query(None),
    status_filter:  Optional[str] = Query(None, alias="status"),
    limit: int = Query(100, le=500),
):
    q = db.query(Payment).order_by(Payment.id.desc())
    if third_party_id:
        q = q.filter_by(third_party_id=third_party_id)
    if status_filter:
        q = q.filter_by(status=status_filter.upper())
    return [
        {
            "id": p.id, "third_party_id": p.third_party_id,
            "order_id": p.merch_order_id, "amount": p.total_amount,
            "currency": p.currency, "status": p.status, "title": p.title,
            "created_at": p.created_at,
        }
        for p in q.limit(limit).all()
    ]


def _activity_row(a: ActivityLog) -> dict:
    meta = None
    if a.meta_json:
        try: meta = json.loads(a.meta_json)
        except Exception: meta = None
    return {
        "id": a.id,
        "third_party_id": a.third_party_id,
        "actor_type": a.actor_type,
        "actor_id":   a.actor_id,
        "action":     a.action,
        "description": a.description,
        "order_id":   a.order_id,
        "ip_address": a.ip_address,
        "user_agent": a.user_agent,
        "metadata":   meta,
        "created_at": a.created_at,
    }


@app.get("/admin/activity", tags=["Admin / Activity"])
def admin_list_activity(
    db: Session = Depends(get_db),
    _admin: Admin = Depends(get_current_admin),
    third_party_id: Optional[int] = Query(None),
    action:    Optional[str] = Query(None, description="e.g. payment_created, webhook_received"),
    actor_type: Optional[str] = Query(None),
    order_id:  Optional[str] = Query(None),
    since:     Optional[datetime] = Query(None),
    limit:     int = Query(100, le=1000),
    offset:    int = Query(0,   ge=0),
):
    q = db.query(ActivityLog).order_by(ActivityLog.id.desc())
    if third_party_id: q = q.filter_by(third_party_id=third_party_id)
    if action:         q = q.filter_by(action=action)
    if actor_type:     q = q.filter_by(actor_type=actor_type)
    if order_id:       q = q.filter_by(order_id=order_id)
    if since:          q = q.filter(ActivityLog.created_at >= since)
    total = q.count()
    rows = q.offset(offset).limit(limit).all()
    return {
        "total": total, "limit": limit, "offset": offset,
        "items": [_activity_row(a) for a in rows],
    }


@app.get(
    "/admin/third-parties/{tp_id}/activity",
    tags=["Admin / Activity"],
)
def admin_third_party_activity(
    tp_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    _admin: Admin = Depends(get_current_admin),
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
):
    if not db.query(ThirdParty).filter_by(id=tp_id).first():
        raise HTTPException(404, "Third party not found")
    q = db.query(ActivityLog).filter_by(third_party_id=tp_id).order_by(ActivityLog.id.desc())
    total = q.count()
    rows = q.offset(offset).limit(limit).all()
    return {
        "total": total, "limit": limit, "offset": offset,
        "items": [_activity_row(a) for a in rows],
    }


@app.get("/api/activity", tags=["Third-Party API"])
def tp_my_activity(
    db: Session = Depends(get_db),
    tp: ThirdParty = Depends(get_current_third_party),
    action:   Optional[str] = Query(None),
    order_id: Optional[str] = Query(None),
    since:    Optional[datetime] = Query(None),
    limit:    int = Query(100, le=500),
    offset:   int = Query(0, ge=0),
):
    """Lets a third party self-audit their own movements."""
    q = db.query(ActivityLog).filter_by(third_party_id=tp.id).order_by(ActivityLog.id.desc())
    if action:   q = q.filter_by(action=action)
    if order_id: q = q.filter_by(order_id=order_id)
    if since:    q = q.filter(ActivityLog.created_at >= since)
    total = q.count()
    rows = q.offset(offset).limit(limit).all()
    return {
        "total": total, "limit": limit, "offset": offset,
        "items": [_activity_row(a) for a in rows],
    }


def _notif_row(n: PaymentNotification) -> dict:
    raw = None
    if n.raw_payload:
        try: raw = json.loads(n.raw_payload)
        except Exception: raw = n.raw_payload
    return {
        "id":                n.id,
        "third_party_id":    n.third_party_id,
        "merch_order_id":    n.merch_order_id,
        "payment_order_id":  n.payment_order_id,
        "appid":             n.appid,
        "merch_code":        n.merch_code,
        "trade_status":      n.trade_status,
        "total_amount":      n.total_amount,
        "trans_currency":    n.trans_currency,
        "notify_time":       n.notify_time,
        "trans_end_time":    n.trans_end_time,
        "callback_info":     n.callback_info,
        "sign":              n.sign,
        "sign_type":         n.sign_type,
        "raw_payload":       raw,
        "processed":         n.processed,
        "received_at":       n.received_at,
    }


@app.get("/admin/notifications", tags=["Admin / Monitoring"])
def admin_list_notifications(
    db: Session = Depends(get_db),
    _admin: Admin = Depends(get_current_admin),
    third_party_id: Optional[int] = Query(None),
    order_id:       Optional[str] = Query(None, description="merch_order_id"),
    trade_status:   Optional[str] = Query(None),
    limit:          int = Query(100, le=500),
    offset:         int = Query(0,   ge=0),
):
    """Raw D-Money webhooks received by the platform."""
    q = db.query(PaymentNotification).order_by(PaymentNotification.id.desc())
    if third_party_id:
        q = q.filter_by(third_party_id=third_party_id)
    if order_id:
        q = q.filter_by(merch_order_id=order_id)
    if trade_status:
        q = q.filter(PaymentNotification.trade_status.ilike(trade_status))
    total = q.count()
    rows  = q.offset(offset).limit(limit).all()
    return {
        "total": total, "limit": limit, "offset": offset,
        "items": [_notif_row(n) for n in rows],
    }


@app.get("/admin/notifications/{notif_id}", tags=["Admin / Monitoring"])
def admin_get_notification(
    notif_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    _admin: Admin = Depends(get_current_admin),
):
    """Full detail for one D-Money notification + every forward attempt."""
    n = db.query(PaymentNotification).filter_by(id=notif_id).first()
    if not n:
        raise HTTPException(404, "Notification not found")
    deliveries = (
        db.query(WebhookDelivery)
        .filter_by(notification_id=n.id)
        .order_by(WebhookDelivery.id.asc())
        .all()
    )
    return {
        "notification": _notif_row(n),
        "deliveries": [
            {
                "id": d.id, "attempt": d.attempt, "target_url": d.target_url,
                "status_code": d.status_code, "delivered": d.delivered,
                "request_body":  (json.loads(d.request_body) if d.request_body else None),
                "response_body": d.response_body,
                "error": d.error, "created_at": d.created_at,
            } for d in deliveries
        ],
    }


@app.get("/api/payments/{order_id}/notifications", tags=["Third-Party API"])
def tp_payment_notifications(
    order_id: str = Path(...),
    db: Session = Depends(get_db),
    tp: ThirdParty = Depends(get_current_third_party),
):
    """Third party can pull the full notification history for one of THEIR orders,
    including the exact payload that was forwarded to their notify_url."""
    if not db.query(Payment).filter_by(third_party_id=tp.id, merch_order_id=order_id).first():
        raise HTTPException(404, "Payment not found")

    notifs = (
        db.query(PaymentNotification)
        .filter_by(third_party_id=tp.id, merch_order_id=order_id)
        .order_by(PaymentNotification.id.asc())
        .all()
    )
    out = []
    for n in notifs:
        dels = (
            db.query(WebhookDelivery)
            .filter_by(notification_id=n.id)
            .order_by(WebhookDelivery.id.asc())
            .all()
        )
        out.append({
            "notification": _notif_row(n),
            "deliveries": [
                {
                    "attempt": d.attempt, "target_url": d.target_url,
                    "status_code": d.status_code, "delivered": d.delivered,
                    "forwarded_payload": (json.loads(d.request_body) if d.request_body else None),
                    "response_body": d.response_body, "error": d.error,
                    "created_at": d.created_at,
                } for d in dels
            ],
        })
    return {"order_id": order_id, "count": len(out), "notifications": out}


@app.get("/admin/webhooks/deliveries", tags=["Admin / Monitoring"])
def admin_webhook_deliveries(
    db: Session = Depends(get_db),
    _admin: Admin = Depends(get_current_admin),
    third_party_id: Optional[int] = Query(None),
    limit: int = Query(100, le=500),
):
    q = db.query(WebhookDelivery).order_by(WebhookDelivery.id.desc())
    if third_party_id:
        q = q.filter_by(third_party_id=third_party_id)
    return [
        {
            "id": d.id, "third_party_id": d.third_party_id,
            "notification_id": d.notification_id,
            "target_url": d.target_url, "attempt": d.attempt,
            "status_code": d.status_code, "delivered": d.delivered,
            "error": d.error, "created_at": d.created_at,
        }
        for d in q.limit(limit).all()
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Third-party API (auth = lifetime token)
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/api/me", tags=["Third-Party API"])
def tp_me(tp: ThirdParty = Depends(get_current_third_party)):
    return {
        "id": tp.id, "name": tp.name, "company": tp.company,
        "appid": tp.appid, "merch_code": tp.merch_code,
        "notify_url": tp.notify_url, "redirect_url": tp.redirect_url,
        "is_active": tp.is_active,
    }


@app.post("/api/payments", response_model=PaymentResponse, tags=["Third-Party API"])
def tp_create_payment(
    body: CreatePaymentBody,
    request: Request,
    db: Session = Depends(get_db),
    tp: ThirdParty = Depends(get_current_third_party),
):
    # Compute total + title from single or bulk
    if body.items:
        total = sum(it.amount for it in body.items)
        if len(body.items) == 1:
            inferred_title = body.items[0].description
        else:
            inferred_title = f"{len(body.items)} items ({body.items[0].description}...)"
        items_payload = [it.model_dump() for it in body.items]
    else:
        total          = float(body.amount)
        inferred_title = body.description
        items_payload  = None

    title = _clean_title(body.title or inferred_title or "Payment")

    try:
        gateway = DmoneyPaymentGateway.from_third_party(tp, decrypt)
    except Exception as e:
        logger.error(f"Failed to build gateway for tp={tp.id}: {e}")
        raise HTTPException(500, "Gateway misconfigured for this third party")

    try:
        # D-Money calls OUR /payment/notify; we forward to tp.notify_url
        result = gateway.create_payment(
            amount        = total,
            title         = title,
            order_id      = body.order_id,
            currency      = body.currency,
            timeout       = body.timeout,
            notify_url    = PLATFORM_NOTIFY_URL,
            redirect_url  = body.redirect_url or tp.redirect_url,
            callback_info = body.callback_info,
            language      = body.language,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        logger.error(f"create_payment failed tp={tp.id}: {e}")
        raise HTTPException(502, str(e))

    payment = Payment(
        third_party_id  = tp.id,
        merch_order_id  = result["order_id"],
        prepay_id       = result.get("prepay_id"),
        checkout_url    = result.get("checkout_url"),
        total_amount    = total,
        currency        = body.currency,
        title           = title,
        items_json      = json.dumps(items_payload) if items_payload else None,
        status          = "PENDING",
        callback_info   = body.callback_info,
        language        = body.language,
        timeout_express = body.timeout,
    )
    db.add(payment)
    log_activity(
        db, action=Actions.PAYMENT_CREATED,
        third_party_id=tp.id, actor_type="third_party",
        description=f"{('Bulk' if items_payload else 'Single')} payment: {title} — {total} {body.currency}",
        order_id=result["order_id"], request=request,
        metadata={
            "amount": total, "currency": body.currency,
            "item_count": len(items_payload) if items_payload else 1,
        },
    )
    db.commit()

    return {
        "success":      True,
        "order_id":     result["order_id"],
        "prepay_id":    result.get("prepay_id"),
        "checkout_url": result.get("checkout_url"),
        "amount":       total,
        "currency":     body.currency,
        "title":        title,
        "status":       "PENDING",
    }


@app.get("/api/payments", tags=["Third-Party API"])
def tp_list_payments(
    db: Session = Depends(get_db),
    tp: ThirdParty = Depends(get_current_third_party),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, le=500),
):
    q = db.query(Payment).filter_by(third_party_id=tp.id).order_by(Payment.id.desc())
    if status_filter:
        q = q.filter_by(status=status_filter.upper())
    return [
        {
            "order_id": p.merch_order_id, "amount": p.total_amount,
            "currency": p.currency, "status": p.status, "title": p.title,
            "checkout_url": p.checkout_url, "created_at": p.created_at,
        }
        for p in q.limit(limit).all()
    ]


@app.get("/api/payments/{order_id}", response_model=PaymentDetail, tags=["Third-Party API"])
def tp_get_payment(
    request: Request,
    order_id: str = Path(...),
    db: Session = Depends(get_db),
    tp: ThirdParty = Depends(get_current_third_party),
):
    p = db.query(Payment).filter_by(third_party_id=tp.id, merch_order_id=order_id).first()
    if not p:
        raise HTTPException(404, "Payment not found")

    log_activity(
        db, action=Actions.PAYMENT_QUERIED,
        third_party_id=tp.id, actor_type="third_party",
        order_id=order_id, request=request,
        description=f"Queried payment {order_id} (status={p.status})",
    )

    # If still pending, try a live query against D-Money to refresh status
    if p.status == "PENDING":
        try:
            gateway = DmoneyPaymentGateway.from_third_party(tp, decrypt)
            data = gateway.query_order(merch_order_id=order_id)
            biz  = data.get("biz_content") or {}
            if isinstance(biz, str):
                try: biz = json.loads(biz)
                except Exception: biz = {}
            raw_ts = biz.get("trade_status") or ""
            new_status = _normalize_status(raw_ts)
            if new_status and new_status != "PENDING" and new_status != p.status:
                old_status = p.status
                p.status = new_status
                log_activity(
                    db, action=Actions.PAYMENT_STATUS_CHANGED,
                    third_party_id=tp.id, actor_type="system",
                    order_id=order_id,
                    description=f"Status {old_status} → {new_status} (live query)",
                    metadata={"from": old_status, "to": new_status, "source": "query_order"},
                )
        except Exception as e:
            logger.warning(f"Live query refresh failed order={order_id}: {e}")
    db.commit()

    items = None
    if p.items_json:
        try: items = json.loads(p.items_json)
        except Exception: items = None

    return {
        "order_id": p.merch_order_id, "prepay_id": p.prepay_id,
        "checkout_url": p.checkout_url, "amount": p.total_amount,
        "currency": p.currency, "title": p.title, "status": p.status,
        "items": items, "callback_info": p.callback_info,
        "created_at": p.created_at, "updated_at": p.updated_at,
    }


# ──────────────────────────────────────────────────────────────────────────────
# D-Money webhook receiver → forward to third party
# ──────────────────────────────────────────────────────────────────────────────
def _forward_webhook_task(notification_id: int, third_party_id: int, target_url: str, payload: dict):
    """Forward an idempotent payload to the third party, retry up to 3 times."""
    body_str = json.dumps(payload)
    body = body_str.encode("utf-8")
    backoff = [0, 5, 30]   # 3 attempts
    order_id = payload.get("order_id")
    db = SessionLocal()
    try:
        for attempt, wait in enumerate(backoff, start=1):
            if wait:
                time.sleep(wait)
            row = WebhookDelivery(
                notification_id=notification_id,
                third_party_id=third_party_id,
                order_id=order_id,
                target_url=target_url,
                attempt=attempt,
                request_body=body_str,
            )
            try:
                resp = requests.post(
                    target_url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    timeout=15,
                )
                row.status_code   = resp.status_code
                row.response_body = resp.text[:2000]
                row.delivered     = 200 <= resp.status_code < 300
                db.add(row); db.commit()
                if row.delivered:
                    logger.info(f"Webhook delivered tp={third_party_id} → {target_url}")
                    log_activity(
                        db, action=Actions.WEBHOOK_FORWARDED,
                        third_party_id=third_party_id, actor_type="system",
                        order_id=order_id,
                        description=f"Forwarded to {target_url} (HTTP {resp.status_code}) on attempt {attempt}",
                        metadata={"attempt": attempt, "status_code": resp.status_code},
                        commit=True,
                    )
                    return
                logger.warning(
                    f"Webhook attempt {attempt} HTTP {resp.status_code} tp={third_party_id}"
                )
            except Exception as e:
                row.error = str(e)[:500]
                db.add(row); db.commit()
                logger.warning(f"Webhook attempt {attempt} error tp={third_party_id}: {e}")
        logger.error(f"Webhook gave up after 3 attempts tp={third_party_id} → {target_url}")
        log_activity(
            db, action=Actions.WEBHOOK_FAILED,
            third_party_id=third_party_id, actor_type="system",
            order_id=order_id,
            description=f"Failed to deliver webhook to {target_url} after 3 attempts",
            metadata={"attempts": 3, "target_url": target_url},
            commit=True,
        )
    finally:
        db.close()


# D-Money trade_status values, normalised to our internal vocabulary.
# Per D-Money docs the canonical values are: Paying, Completed, Expired, Failure.
# Real webhook samples also use PAY_SUCCESS / PAYED — handle both casings.
_STATUS_MAP = {
    "PAY_SUCCESS": "PAID",
    "PAYSUCCESS":  "PAID",
    "COMPLETED":   "PAID",
    "PAYED":       "PAID",        # legacy spelling some integrations emit
    "SUCCESS":     "PAID",
    "PAYING":      "PAYING",      # user authorised but not finalised yet
    "PENDING":     "PENDING",
    "EXPIRED":     "EXPIRED",
    "FAILURE":     "FAILED",
    "FAILED":      "FAILED",
    "FAIL":        "FAILED",
    "CANCEL":      "CANCELLED",
    "CANCELLED":   "CANCELLED",
}


def _normalize_status(dmoney_status: Optional[str]) -> str:
    """Map a raw D-Money trade_status to our canonical internal value."""
    if not dmoney_status:
        return "PENDING"
    return _STATUS_MAP.get(dmoney_status.upper().strip(), dmoney_status.upper())


_DMONEY_ACK = {"code": "0", "msg": "Success", "result": "SUCCESS"}


def _process_dmoney_notification(body: dict, request: Request, bg: BackgroundTasks) -> str:
    """Process a D-Money notification (from either POST body or GET query
    string). Returns the canonical status so the caller can decide what to
    return to D-Money (ack JSON) or to the user (success/failed page)."""
    merch_order_id   = body.get("merch_order_id", "unknown")
    payment_order_id = body.get("payment_order_id")
    appid            = body.get("appid")
    raw_status       = body.get("trade_status") or ""
    trade_status     = raw_status.upper()
    canonical        = _normalize_status(raw_status)

    logger.info(f"D-Money notify appid={appid} order={merch_order_id} "
                f"raw_status={raw_status!r} canonical={canonical}")

    db = SessionLocal()
    try:
        # Idempotency: skip if we've already seen this payment_order_id
        if payment_order_id:
            existing = db.query(PaymentNotification).filter_by(
                payment_order_id=payment_order_id
            ).first()
            if existing:
                logger.info(f"Duplicate notify ignored payment_order_id={payment_order_id}")
                return canonical

        # Identify third party (by appid, fallback to order lookup)
        tp = None
        if appid:
            tp = db.query(ThirdParty).filter_by(appid=appid).first()
        if not tp:
            pay = db.query(Payment).filter_by(merch_order_id=merch_order_id).first()
            if pay:
                tp = db.query(ThirdParty).filter_by(id=pay.third_party_id).first()

        notif = PaymentNotification(
            third_party_id   = tp.id if tp else None,
            merch_order_id   = merch_order_id,
            payment_order_id = payment_order_id,
            appid            = appid,
            notify_time      = body.get("notify_time"),
            merch_code       = body.get("merch_code"),
            total_amount     = body.get("total_amount"),
            trans_currency   = body.get("trans_currency"),
            trade_status     = trade_status,
            trans_end_time   = body.get("trans_end_time"),
            callback_info    = body.get("callback_info"),
            sign             = body.get("sign"),
            sign_type        = body.get("sign_type"),
            raw_payload      = json.dumps(body),
            processed        = bool(tp),
        )
        db.add(notif)

        pay = db.query(Payment).filter_by(merch_order_id=merch_order_id).first()
        old_status = pay.status if pay else None
        if pay and canonical and canonical != old_status:
            pay.status = canonical

        db.add(NotificationLog(
            third_party_id = tp.id if tp else None,
            merch_order_id = merch_order_id,
            message        = f"Webhook received: {raw_status} → {canonical}",
            data           = json.dumps(body),
            type           = "payment_notification",
        ))

        log_activity(
            db, action=Actions.WEBHOOK_RECEIVED,
            third_party_id=tp.id if tp else None, actor_type="dmoney",
            order_id=merch_order_id, request=request,
            description=f"D-Money webhook: trade_status={raw_status} → {canonical}"
                        + ("" if tp else " (UNKNOWN APPID)"),
            metadata={"trade_status": raw_status, "canonical": canonical,
                      "appid": appid, "payment_order_id": payment_order_id,
                      "transport": body.get("_transport", "POST")},
        )
        if pay and canonical and canonical != old_status:
            log_activity(
                db, action=Actions.PAYMENT_STATUS_CHANGED,
                third_party_id=tp.id if tp else pay.third_party_id, actor_type="dmoney",
                order_id=merch_order_id,
                description=f"Status {old_status} → {canonical} (webhook)",
                metadata={"from": old_status, "to": canonical, "source": "webhook"},
            )

        db.commit()
        db.refresh(notif)

        if tp and tp.notify_url:
            forward_payload = {
                "order_id":         merch_order_id,
                "status":           canonical,
                "amount":           body.get("total_amount"),
                "currency":         body.get("trans_currency"),
                "trade_status":     raw_status,
                "trans_end_time":   body.get("trans_end_time"),
                "notify_time":      body.get("notify_time"),
                "payment_order_id": payment_order_id,
                "merch_code":       body.get("merch_code"),
                "callback_info":    body.get("callback_info"),
                "appid":            appid,
                "received_at":      datetime.now(timezone.utc).isoformat(),
            }
            bg.add_task(
                _forward_webhook_task,
                notif.id, tp.id, tp.notify_url, forward_payload,
            )
        elif not tp:
            logger.warning(f"Webhook for unknown appid={appid} order={merch_order_id} — stored only")

    except Exception as e:
        db.rollback()
        logger.error(f"Error processing webhook: {e}")
    finally:
        db.close()

    return canonical


@app.post("/payment/notify", tags=["Webhooks"])
async def payment_notify_post(request: Request, bg: BackgroundTasks):
    """D-Money production: POST a JSON body to this endpoint."""
    try:
        body = await request.json()
    except Exception:
        return _DMONEY_ACK
    body["_transport"] = "POST"
    _process_dmoney_notification(body, request, bg)
    return _DMONEY_ACK


@app.get("/payment/notify", tags=["Webhooks"])
async def payment_notify_get(request: Request, bg: BackgroundTasks):
    """D-Money TEST sandbox redirects the user's browser HERE with payment
    data as query-string params (GET). We process the data exactly like the
    POST version, then send the buyer to a friendly success/failed page."""
    body = dict(request.query_params)
    body["_transport"] = "GET"
    canonical = _process_dmoney_notification(body, request, bg)

    if canonical == "PAID":
        return RedirectResponse(url="/payment/success", status_code=303)
    if canonical in ("FAILED", "CANCELLED", "EXPIRED"):
        return RedirectResponse(url="/payment/failed", status_code=303)
    return RedirectResponse(url="/payment/success", status_code=303)


# Manual retry of a delivery (admin)
@app.post("/admin/webhooks/deliveries/{notif_id}/retry", tags=["Admin / Monitoring"])
def admin_retry_delivery(
    notif_id: int,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    _admin: Admin = Depends(get_current_admin),
):
    notif = db.query(PaymentNotification).filter_by(id=notif_id).first()
    if not notif:
        raise HTTPException(404, "Notification not found")
    if not notif.third_party_id:
        raise HTTPException(400, "No third party associated with this notification")
    tp = db.query(ThirdParty).filter_by(id=notif.third_party_id).first()
    if not tp:
        raise HTTPException(404, "Third party not found")

    payload = {
        "order_id":         notif.merch_order_id,
        "status":           _normalize_status(notif.trade_status),
        "amount":           notif.total_amount,
        "currency":         notif.trans_currency,
        "trade_status":     notif.trade_status,
        "trans_end_time":   notif.trans_end_time,
        "notify_time":      notif.notify_time,
        "payment_order_id": notif.payment_order_id,
        "merch_code":       notif.merch_code,
        "callback_info":    notif.callback_info,
        "appid":            notif.appid,
        "received_at":      datetime.now(timezone.utc).isoformat(),
        "retry":            True,
    }
    bg.add_task(_forward_webhook_task, notif.id, tp.id, tp.notify_url, payload)
    return {"queued": True, "target": tp.notify_url}


# ──────────────────────────────────────────────────────────────────────────────
# Public pages
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/payment/success", response_class=HTMLResponse, tags=["Pages"])
def payment_success():
    return HTMLResponse(_page("✅", "Payment Successful",
                              "Your payment has been processed successfully via D-Money.",
                              "#059669"))


@app.get("/payment/failed", response_class=HTMLResponse, tags=["Pages"])
def payment_failed():
    return HTMLResponse(_page("❌", "Payment Failed",
                              "Something went wrong with your payment. Please try again.",
                              "#dc2626"))


def _page(icon, title, message, color):
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
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
</style></head>
<body><div class="card"><div class="icon">{icon}</div><h2>{title}</h2><p>{message}</p></div></body>
</html>"""
