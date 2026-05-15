"""Encryption (Fernet) for sensitive third-party credentials, and lifetime
API token generation/hashing.

Sensitive fields stored encrypted:
  - ThirdParty.app_secret_enc
  - ThirdParty.private_key_enc

The encryption key (MASTER_ENCRYPTION_KEY) MUST be set as an env var and kept
out of source control. Lose the key → lose the ability to decrypt stored creds.
"""
import os
import hashlib
import secrets
from typing import Tuple

from cryptography.fernet import Fernet, InvalidToken

TOKEN_PREFIX = "tp_live_"


def _master_key() -> bytes:
    key = os.getenv("MASTER_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "MASTER_ENCRYPTION_KEY env var is not set.\n"
            "Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"\n'
            "Add the output to your .env as MASTER_ENCRYPTION_KEY=..."
        )
    return key.encode()


def encrypt(plain: str) -> str:
    return Fernet(_master_key()).encrypt(plain.encode()).decode()


def decrypt(cipher: str) -> str:
    try:
        return Fernet(_master_key()).decrypt(cipher.encode()).decode()
    except InvalidToken:
        raise RuntimeError(
            "Failed to decrypt stored credential — wrong MASTER_ENCRYPTION_KEY "
            "or the data is corrupted."
        )


# ── Lifetime API token ───────────────────────────────────────────────────────

def generate_api_token() -> Tuple[str, str, str]:
    """Generate a new lifetime token for a third party.

    Returns (raw_token, sha256_hex, preview).
    `raw_token` is what we return to the admin once (never again — only the hash is stored).
    """
    raw  = TOKEN_PREFIX + secrets.token_urlsafe(32)        # ~44 chars after the prefix
    hash_hex = hashlib.sha256(raw.encode()).hexdigest()
    preview  = raw[:16] + "…" + raw[-4:]
    return raw, hash_hex, preview


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
