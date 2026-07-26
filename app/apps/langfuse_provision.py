"""Per-application Langfuse provisioning + secret encryption (Phase 5).

On application creation, routeForge asks Langfuse (via its organization-scoped
admin API) to create a dedicated *project* and project API keys, so each
application owner sees only their own traces. The project secret key is
encrypted at rest with the same ENCRYPTION_KEY the Langfuse stack already uses.

All of this is **best-effort**: if provisioning isn't configured (no org key)
or Langfuse is unreachable, the application is created anyway and its traces
fall back to the shared project — serving is never blocked by telemetry setup.
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("serving.obs")


# --- secret encryption ------------------------------------------------------

def _fernet():
    """Fernet built from ENCRYPTION_KEY (the Langfuse 64-hex / 32-byte key).
    Returns None if unset — callers then store the secret unencrypted (dev)."""
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
        log.warning("could not build Fernet from ENCRYPTION_KEY (storing plaintext)")
        return None


def encrypt_secret(plaintext: str) -> bytes:
    f = _fernet()
    return f.encrypt(plaintext.encode()) if f else plaintext.encode()


def decrypt_secret(blob: bytes) -> str:
    f = _fernet()
    if f is None:
        return bytes(blob).decode()
    try:
        return f.decrypt(bytes(blob)).decode()
    except Exception:
        # Stored without encryption (ENCRYPTION_KEY was absent at write time).
        return bytes(blob).decode()


# --- the per-app Langfuse identity ------------------------------------------

@dataclass(frozen=True)
class AppLangfuse:
    project_id: str
    public_key: str
    secret_key: str
    host: str


def load_app_langfuse(app) -> AppLangfuse | None:
    """Build an AppLangfuse from a stored Application row, or None if it has no
    provisioned project. Host comes from LANGFUSE_HOST (the OTLP ingestion host)."""
    if not (app.langfuse_project_id and app.langfuse_public_key
            and app.langfuse_secret_key_enc):
        return None
    host = os.environ.get("LANGFUSE_HOST", "")
    if not host:
        return None
    return AppLangfuse(
        project_id=app.langfuse_project_id,
        public_key=app.langfuse_public_key,
        secret_key=decrypt_secret(app.langfuse_secret_key_enc),
        host=host,
    )


# --- provisioning against the Langfuse org API ------------------------------

class LangfuseProvisioner:
    """Creates a Langfuse project + API keys via the organization-scoped admin
    API. Configured from env: LANGFUSE_HOST + LANGFUSE_ORG_PUBLIC_KEY +
    LANGFUSE_ORG_SECRET_KEY (an org-scoped key pair, distinct from a project's).
    """

    def __init__(self, host: str | None = None, org_public: str | None = None,
                 org_secret: str | None = None, client=None) -> None:
        self._host = (host or os.environ.get("LANGFUSE_HOST") or "").rstrip("/")
        self._pub = org_public or os.environ.get("LANGFUSE_ORG_PUBLIC_KEY")
        self._sec = org_secret or os.environ.get("LANGFUSE_ORG_SECRET_KEY")
        self._client = client  # injectable httpx.AsyncClient (tests)

    def is_configured(self) -> bool:
        return bool(self._host and self._pub and self._sec)

    async def provision(self, app_id: str, name: str) -> AppLangfuse | None:
        if not self.is_configured():
            return None
        import httpx
        auth = (self._pub, self._sec)
        client = self._client
        close = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=15.0)
        try:
            r = await client.post(
                f"{self._host}/api/public/projects", auth=auth,
                json={"name": name, "metadata": {"routeforge_app": app_id}})
            r.raise_for_status()
            proj = r.json()
            project_id = proj.get("id") or proj.get("projectId")
            if not project_id:
                return None
            k = await client.post(
                f"{self._host}/api/public/projects/{project_id}/apiKeys", auth=auth,
                json={"note": f"routeforge:{app_id}"})
            k.raise_for_status()
            kd = k.json()
            pub, sec = kd.get("publicKey"), kd.get("secretKey")
            if not (pub and sec):
                return None
            return AppLangfuse(project_id=project_id, public_key=pub,
                               secret_key=sec, host=self._host)
        except Exception:
            log.warning("Langfuse project provisioning failed for app %r "
                        "(non-fatal; traces fall back to the shared project)", app_id)
            return None
        finally:
            if close:
                await client.aclose()
