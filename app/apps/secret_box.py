"""Small-secret encryption at rest (ENCRYPTION_KEY / Fernet) and scrubbing of
live secrets out of error text.

Shared by the two places RouteForge stores a third-party credential:

- the per-app Langfuse secret key (`app/apps/langfuse_provision.py`), which
  keeps its historical dev fallback of storing plaintext when ENCRYPTION_KEY
  is unset;
- an onboarded endpoint's API key (B68, `onboarded_models.api_key_enc`), which
  has NO plaintext fallback: `encrypt_required` raises instead, and the admin
  route answers 422.

`register_secret` / `scrub_secrets` exist because an upstream error message is
not under our control: a provider (or LiteLLM) may echo request kwargs, headers
or the key itself. Every endpoint key RouteForge decrypts or is given is
registered here, and the error surfaces that log or return upstream text pass
it through `scrub_secrets` first.
"""
from __future__ import annotations

import base64
import logging
import os
import threading

log = logging.getLogger("serving.obs")

#: What a scrubbed secret is replaced with in error text and logs.
SCRUBBED = "<redacted-key>"
# A registered value shorter than this is not scrubbed: replacing every
# occurrence of a 3-character string would mangle unrelated text, and a key
# that short is not a secret worth the name.
_MIN_SCRUB_LEN = 6


def fernet():
    """Fernet built from ENCRYPTION_KEY (the Langfuse 64-hex / 32-byte key).
    Returns None if unset or unusable."""
    key = os.environ.get("ENCRYPTION_KEY")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        try:
            raw = bytes.fromhex(key)
        except ValueError:
            raw = key.encode()
        raw = raw[:32].ljust(32, b"\0")
        return Fernet(base64.urlsafe_b64encode(raw))
    except Exception:  # pragma: no cover - defensive
        log.warning("could not build Fernet from ENCRYPTION_KEY")
        return None


class EncryptionUnavailable(RuntimeError):
    """No usable ENCRYPTION_KEY: a secret that must never be stored in clear
    cannot be stored at all."""


def encrypt_required(plaintext: str) -> str:
    """Fernet token (ASCII text) for `plaintext`. Raises
    `EncryptionUnavailable` when ENCRYPTION_KEY is unset or unusable -- never
    falls back to plaintext."""
    f = fernet()
    if f is None:
        raise EncryptionUnavailable("ENCRYPTION_KEY is not set")
    return f.encrypt(plaintext.encode()).decode("ascii")


def decrypt_or_none(token: str | bytes | None) -> str | None:
    """The plaintext behind a token from `encrypt_required`, or None when
    there is no token, no usable ENCRYPTION_KEY, or the token does not decrypt
    under the current key (rotated / different key). Never raises, never
    returns the token itself."""
    if not token:
        return None
    f = fernet()
    if f is None:
        return None
    try:
        raw = token.encode() if isinstance(token, str) else bytes(token)
        return f.decrypt(raw).decode()
    except Exception:
        return None


_known: set[str] = set()
_known_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    """Remember a live secret so `scrub_secrets` removes it from error text.
    Process-local; a rotated-away key stays registered (still a secret)."""
    if value and len(value) >= _MIN_SCRUB_LEN:
        with _known_lock:
            _known.add(value)


def scrub_secrets(text: str) -> str:
    """`text` with every registered secret replaced by `SCRUBBED`. Longest
    first, so a key that contains another registered key is removed whole."""
    if not text:
        return text
    with _known_lock:
        known = sorted(_known, key=len, reverse=True)
    for s in known:
        if s in text:
            text = text.replace(s, SCRUBBED)
    return text
