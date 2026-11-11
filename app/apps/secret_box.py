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
import json
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


def _key_bytes() -> bytes | None:
    """ENCRYPTION_KEY decoded (hex if it parses as hex, else its UTF-8
    bytes), before any padding; None when unset."""
    key = os.environ.get("ENCRYPTION_KEY")
    if not key:
        return None
    try:
        return bytes.fromhex(key)
    except ValueError:
        return key.encode()


#: The 422 text for an endpoint key that cannot be stored, by cause.
NO_KEY_MESSAGE = "set ENCRYPTION_KEY to store endpoint keys, or use api_key_env"
SHORT_KEY_MESSAGE = ("ENCRYPTION_KEY must be at least 32 bytes (64 hex characters) to "
                     "store endpoint keys; set a stronger one, or use api_key_env")


def endpoint_key_encryption_problem() -> str | None:
    """Why an endpoint key cannot be stored right now, or None if it can.
    Stricter than `fernet()`, which zero-pads a short ENCRYPTION_KEY for the
    historical Langfuse path: an endpoint key is refused under a key that
    decodes to fewer than 32 bytes (e.g. "changeme")."""
    raw = _key_bytes()
    if raw is None:
        return NO_KEY_MESSAGE
    if len(raw) < 32:
        return SHORT_KEY_MESSAGE
    if fernet() is None:     # pragma: no cover - cryptography missing
        return NO_KEY_MESSAGE
    return None


def fernet():
    """Fernet built from ENCRYPTION_KEY (the Langfuse 64-hex / 32-byte key).
    Returns None if unset or unusable. A short key is zero-padded (Langfuse
    path compatibility); `encrypt_required` refuses one."""
    raw = _key_bytes()
    if raw is None:
        return None
    try:
        from cryptography.fernet import Fernet
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
    falls back to plaintext -- or when it decodes to fewer than 32 bytes."""
    problem = endpoint_key_encryption_problem()
    if problem is not None:
        raise EncryptionUnavailable(problem)
    return fernet().encrypt(plaintext.encode()).decode("ascii")


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


def _forms(secret: str) -> set[str]:
    """The secret as it may appear in error text: verbatim, and escaped the
    way repr() of a str or bytes and JSON would escape it (an HTTP library
    that reprs an illegal header value prints `\\n`, not a newline)."""
    forms = {secret, repr(secret)[1:-1], json.dumps(secret)[1:-1]}
    try:
        forms.add(repr(secret.encode())[2:-1])
    except Exception:   # pragma: no cover - defensive
        pass
    return {f for f in forms if len(f) >= _MIN_SCRUB_LEN}


def scrub_secrets(text: str) -> str:
    """`text` with every registered secret (and its escaped forms) replaced
    by `SCRUBBED`. Longest first, so a key that contains another registered
    key is removed whole."""
    if not text:
        return text
    with _known_lock:
        known = list(_known)
    for f in sorted({f for s in known for f in _forms(s)}, key=len, reverse=True):
        if f in text:
            text = text.replace(f, SCRUBBED)
    return text


class UpstreamError(RuntimeError):
    """Stands in for an upstream exception whose text still carried a live
    secret after in-place scrubbing. Keeps `status_code` so failure
    classification (B36) is unchanged."""

    def __init__(self, message: str, status_code=None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _scrub_in_place(exc: BaseException) -> None:
    exc.args = tuple(scrub_secrets(a) if isinstance(a, str) else a for a in exc.args)
    for attr in ("message", "detail", "text"):
        v = getattr(exc, attr, None)
        if isinstance(v, str):
            try:
                setattr(exc, attr, scrub_secrets(v))
            except Exception:
                pass


def _leaks(text: str) -> bool:
    return scrub_secrets(text) != text


def scrubbed_exception(exc: BaseException) -> BaseException:
    """`exc` with every registered secret removed from its text and from its
    `__cause__`/`__context__` chain (which a logged traceback prints).
    Scrubs in place, so the exception type -- which the gateway and the HTTP
    mapping classify on -- is kept. If the text still leaks (a `__str__` that
    is not built from args/message), returns an `UpstreamError` with the
    scrubbed text instead, chained to nothing."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(seen) < 16:
        seen.add(id(cur))
        _scrub_in_place(cur)
        cur = cur.__cause__ or cur.__context__
    chain_leaks = False
    cur, seen = exc, set()
    while cur is not None and id(cur) not in seen and len(seen) < 16:
        seen.add(id(cur))
        try:
            chain_leaks = chain_leaks or _leaks(str(cur)) or _leaks(repr(cur))
        except Exception:
            chain_leaks = True
        cur = cur.__cause__ or cur.__context__
    if not chain_leaks:
        return exc
    try:
        text = scrub_secrets(str(exc))
    except Exception:
        text = type(exc).__name__
    out = UpstreamError(f"{type(exc).__name__}: {text}",
                        status_code=getattr(exc, "status_code", None))
    out.__suppress_context__ = True
    return out
