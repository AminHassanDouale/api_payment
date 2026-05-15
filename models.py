"""Database models — multi-tenant D-Money API platform."""
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Boolean, ForeignKey, Float,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _now():
    return datetime.now(timezone.utc)


# ── Admin ─────────────────────────────────────────────────────────────────────
class Admin(Base):
    __tablename__ = "admins"

    id            = Column(Integer, primary_key=True)
    email         = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    name          = Column(String(255), nullable=True)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=_now)


# ── Third Party (tenant) ──────────────────────────────────────────────────────
class ThirdParty(Base):
    """A registered enterprise that uses the platform to take D-Money payments."""
    __tablename__ = "third_parties"

    id      = Column(Integer, primary_key=True)
    name    = Column(String(255), nullable=False)            # e.g. "Scolapp"
    email   = Column(String(255), nullable=True, index=True)
    company = Column(String(255), nullable=True)
    phone   = Column(String(64),  nullable=True)

    # ── D-Money credentials (secrets are Fernet-encrypted at rest) ────────────
    appid             = Column(String(64),  unique=True, nullable=False, index=True)
    app_key           = Column(String(255), nullable=False)
    app_secret_enc    = Column(Text,        nullable=False)
    private_key_enc   = Column(Text,        nullable=False)  # PEM or base64-DER, encrypted
    merch_code        = Column(String(32),  nullable=False)  # "short code"
    business_type     = Column(String(64),  default="OnlineMerchant")

    # D-Money endpoints (test vs prod)
    dmoney_base_url           = Column(String(255), nullable=False)
    dmoney_query_base_url     = Column(String(255), nullable=True)
    dmoney_checkout_base_url  = Column(String(255), nullable=True)

    # Where THIS third party wants to receive forwarded webhooks / redirects
    notify_url   = Column(Text, nullable=False)
    redirect_url = Column(Text, nullable=False)

    # ── Lifetime API token ────────────────────────────────────────────────────
    token_hash    = Column(String(128), unique=True, nullable=False, index=True)
    token_preview = Column(String(32),  nullable=True)        # e.g. "tp_live_AbCdEf…" (display only)

    is_active  = Column(Boolean,  default=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


# ── Payment ───────────────────────────────────────────────────────────────────
class Payment(Base):
    """One row per payment a third party initiates through the platform."""
    __tablename__ = "payments"

    id               = Column(Integer, primary_key=True)
    third_party_id   = Column(Integer, ForeignKey("third_parties.id"), nullable=False, index=True)
    merch_order_id   = Column(String(64), unique=True, nullable=False, index=True)
    prepay_id        = Column(String(128), nullable=True)
    checkout_url     = Column(Text, nullable=True)
    total_amount     = Column(Float, nullable=False)
    currency         = Column(String(8), default="DJF")
    title            = Column(String(255), nullable=False)
    items_json       = Column(Text, nullable=True)            # bulk: JSON list of {amount, description}
    status           = Column(String(32), default="PENDING")  # PENDING|PAID|FAILED|CANCELLED
    callback_info    = Column(Text, nullable=True)            # passthrough metadata for third party
    language         = Column(String(8), default="en")
    timeout_express  = Column(String(16), default="120m")
    created_at       = Column(DateTime, default=_now)
    updated_at       = Column(DateTime, default=_now, onupdate=_now)

    third_party = relationship("ThirdParty")


# ── Payment Notification (D-Money webhook) ────────────────────────────────────
class PaymentNotification(Base):
    """One row per D-Money webhook received."""
    __tablename__ = "payment_notifications"

    id                = Column(Integer, primary_key=True)
    third_party_id    = Column(Integer, ForeignKey("third_parties.id"), nullable=True, index=True)
    merch_order_id    = Column(String(64), index=True, nullable=True)
    payment_order_id  = Column(String(64), unique=True, nullable=True)   # idempotency
    appid             = Column(String(64), nullable=True, index=True)
    notify_time       = Column(String(32), nullable=True)
    merch_code        = Column(String(32), nullable=True)
    total_amount      = Column(String(32), nullable=True)
    trans_currency    = Column(String(8),  nullable=True)
    trade_status      = Column(String(32), nullable=True)
    trans_end_time    = Column(String(32), nullable=True)
    callback_info     = Column(Text, nullable=True)
    sign              = Column(Text, nullable=True)
    sign_type         = Column(String(32), nullable=True)
    raw_payload       = Column(Text, nullable=True)
    received_at       = Column(DateTime, default=_now)
    processed         = Column(Boolean, default=False)


# ── Webhook delivery (platform → third party) ────────────────────────────────
class WebhookDelivery(Base):
    """Each attempt to forward a payment notification to a third-party notify_url."""
    __tablename__ = "webhook_deliveries"

    id              = Column(Integer, primary_key=True)
    notification_id = Column(Integer, ForeignKey("payment_notifications.id"), nullable=False)
    third_party_id  = Column(Integer, ForeignKey("third_parties.id"),         nullable=False, index=True)
    order_id        = Column(String(64), nullable=True, index=True)
    target_url      = Column(Text,    nullable=False)
    attempt         = Column(Integer, default=1)
    request_body    = Column(Text,    nullable=True)   # exact JSON we POSTed to the third party
    status_code     = Column(Integer, nullable=True)
    response_body   = Column(Text,    nullable=True)
    error           = Column(Text,    nullable=True)
    delivered       = Column(Boolean, default=False)
    created_at      = Column(DateTime, default=_now)


class NotificationLog(Base):
    """Audit trail for any event tied to a payment/webhook."""
    __tablename__ = "notification_logs"

    id             = Column(Integer, primary_key=True)
    third_party_id = Column(Integer, ForeignKey("third_parties.id"), nullable=True)
    merch_order_id = Column(String(64), index=True, nullable=True)
    message        = Column(Text,    nullable=False)
    data           = Column(Text,    nullable=True)
    type           = Column(String(32), default="general")
    created_at     = Column(DateTime, default=_now)


# ── Activity log (suivi des mouvements) ──────────────────────────────────────
class ActivityLog(Base):
    """High-level audit trail of third-party activity and admin actions.

    Use this to answer questions like:
      - "What did third party X do today?"
      - "When was the last successful payment for tenant Y?"
      - "Who rotated this token, and when?"
    """
    __tablename__ = "activity_logs"

    id             = Column(Integer, primary_key=True)
    third_party_id = Column(Integer, ForeignKey("third_parties.id"), nullable=True, index=True)
    actor_type     = Column(String(16), default="third_party", index=True)  # third_party | admin | dmoney | system
    actor_id       = Column(Integer, nullable=True)                          # admin id when actor_type=admin
    action         = Column(String(64), nullable=False, index=True)          # see ACTIONS in activity.py
    description    = Column(Text, nullable=True)
    order_id       = Column(String(64), nullable=True, index=True)
    ip_address     = Column(String(64), nullable=True)
    user_agent     = Column(String(256), nullable=True)
    meta_json      = Column(Text, nullable=True)                              # extra JSON context
    created_at     = Column(DateTime, default=_now, index=True)
