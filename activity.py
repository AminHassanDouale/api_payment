"""Activity-log helper for tracking third-party movements and admin actions."""
import json
import logging
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from models import ActivityLog

logger = logging.getLogger("DmoneyAPI.activity")


# Canonical action names — keep this list in sync with the UI filter dropdown.
class Actions:
    # Third-party initiated
    PAYMENT_CREATED        = "payment_created"
    PAYMENT_QUERIED        = "payment_queried"
    PAYMENT_LISTED         = "payment_listed"
    TOKEN_USED             = "token_used"          # reserved (not logged today — too noisy)

    # System / D-Money initiated
    WEBHOOK_RECEIVED       = "webhook_received"
    WEBHOOK_FORWARDED      = "webhook_forwarded"
    WEBHOOK_FAILED         = "webhook_failed"
    PAYMENT_STATUS_CHANGED = "payment_status_changed"

    # Admin initiated
    THIRD_PARTY_CREATED    = "third_party_created"
    THIRD_PARTY_UPDATED    = "third_party_updated"
    THIRD_PARTY_DISABLED   = "third_party_disabled"
    THIRD_PARTY_DELETED    = "third_party_deleted"
    TOKEN_ROTATED          = "token_rotated"
    ADMIN_LOGIN            = "admin_login"
    ADMIN_REGISTERED       = "admin_registered"


def _client_ip(request: Optional[Request]) -> Optional[str]:
    if not request:
        return None
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def log_activity(
    db: Session,
    *,
    action: str,
    third_party_id: Optional[int] = None,
    actor_type: str = "third_party",
    actor_id:   Optional[int] = None,
    description: Optional[str] = None,
    order_id:   Optional[str] = None,
    request:    Optional[Request] = None,
    metadata:   Optional[dict] = None,
    commit:     bool = False,
) -> ActivityLog:
    """Add an ActivityLog row. Caller commits unless commit=True."""
    entry = ActivityLog(
        third_party_id=third_party_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        description=(description or "")[:2000] if description else None,
        order_id=order_id,
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent", "")[:256] if request else None),
        meta_json=json.dumps(metadata, default=str) if metadata else None,
    )
    db.add(entry)
    if commit:
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to commit activity log: {e}")
    return entry
