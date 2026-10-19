"""API token generation and hashing.

Tokens look like `rf_live_<url-safe-random>`. Only the sha256 hash is ever
persisted; the plaintext is returned once at creation and never stored. sha256
(not a slow KDF) is deliberate: tokens are high-entropy random strings, not
human passwords, so there is nothing to brute-force and per-request auth must be
fast. Admin *passwords* use argon2 (see app/apps/admin_auth.py, Phase 3).
"""
from __future__ import annotations

import hashlib
import secrets

_TOKEN_ENTROPY_BYTES = 24


def new_token(env: str = "live") -> str:
    """A fresh opaque token, e.g. `rf_live_x7Kp…`."""
    return f"rf_{env}_{secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)}"


def new_service_credential() -> str:
    """A fresh service credential, e.g. `rf_svc_x7Kp…`. A distinct prefix so
    one is never mistaken for (or pasted as) an app token."""
    return f"rf_svc_{secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)}"

def hash_token(raw: str) -> str:
    """sha256 hex of the raw token — the value stored and looked up."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def token_prefix(raw: str) -> str:
    """A short, safe-to-display prefix for the UI (never the full token)."""
    if len(raw) <= 12:
        return raw
    return raw[:12] + "…"
