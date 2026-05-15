"""Authentication helpers.

- Admin: bcrypt password + signed JWT (configurable TTL).
- Third party: opaque lifetime token (Bearer), looked up by SHA-256 hash.
"""
import os
from datetime import datetime, timezone, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from db import get_db
from models import Admin, ThirdParty
from crypto_utils import hash_token, TOKEN_PREFIX


# ── JWT config ────────────────────────────────────────────────────────────────
_JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
if not _JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET env var is not set. Generate one with:\n"
        '  python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )
_JWT_ALG       = "HS256"
_JWT_TTL_HOURS = int(os.getenv("JWT_TTL_HOURS", "24"))

_bearer = HTTPBearer(auto_error=True)


# ── Passwords ────────────────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# ── Admin JWT ────────────────────────────────────────────────────────────────
def create_admin_jwt(admin_id: int, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub":   str(admin_id),
        "email": email,
        "role":  "admin",
        "iat":   int(now.timestamp()),
        "exp":   int((now + timedelta(hours=_JWT_TTL_HOURS)).timestamp()),
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALG)


def _decode_jwt(token: str) -> dict:
    return jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALG])


# ── FastAPI dependencies ─────────────────────────────────────────────────────
def get_current_admin(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Admin:
    try:
        payload = _decode_jwt(creds.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")

    if payload.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")

    admin = db.query(Admin).filter_by(id=int(payload["sub"]), is_active=True).first()
    if not admin:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Admin not found or disabled")
    return admin


def get_current_third_party(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> ThirdParty:
    token = creds.credentials.strip()
    if not token.startswith(TOKEN_PREFIX):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API token format")

    tp = db.query(ThirdParty).filter_by(
        token_hash=hash_token(token),
        is_active=True,
    ).first()
    if not tp:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked API token")
    return tp
