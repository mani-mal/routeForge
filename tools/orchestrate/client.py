"""External-training-orchestrator client (tools.orchestrate).

RouteForge's side of a handshake with an EXTERNAL training service that runs
on the operator's own GPUs, in a separate repo/process. This module
TRIGGERS a distill/fine-tune job (`submit_job`) and TRACKS its status
(`get_status`) over plain HTTP — it runs NO training loop itself, hosts no
training or inference compute, and never streams a token. The external
service owns everything about *how* training happens; RouteForge only ever
POSTs a spec and GETs a status. See CLAUDE.md rule 4 ("orchestrated, not
hosted").

Mirrors the outbound-HTTP idiom of `app.obs.scores`: an injectable `client`
(an `httpx.AsyncClient`-shaped object) so tests never touch the network, and
credentials resolved from env-var NAMES at call time — never a literal
secret in code, a CLI arg, or a file (CLAUDE.md: "No secrets in code or
compose. `.env` only.").
"""
from __future__ import annotations

import os

from pydantic import BaseModel


class TrainingJobSpec(BaseModel):
    """The documented contract POSTed to `{base_url}/jobs`.

    `base_model_ref` is a reference the EXTERNAL trainer understands —
    RouteForge does not constrain it to a `ModelRegistry` slug; the external
    service owns training, not RouteForge. `dataset_ref` is likewise a
    REFERENCE to a residency-local exported dataset the external service
    already has access to: RouteForge never reads or forwards the raw
    dataset content, and never fetches it from Langfuse in-process.
    """

    base_model_ref: str
    method: str
    dataset_ref: str
    hyperparams: dict = {}
    notes: str = ""


class TrainingJobStatus(BaseModel):
    """The parsed response of `GET {base_url}/jobs/{external_ref}`."""

    external_ref: str
    state: str  # submitted | running | succeeded | failed
    result_endpoint: str | None = None
    result_slug: str | None = None
    error: str | None = None


class OrchestratorError(Exception):
    """A friendly, actionable error for a failed call to the external
    training orchestrator (missing credentials, network failure, non-2xx,
    or a malformed response). This is an offline CLI job, so raising here is
    fine — unlike `app.obs.scores`, which must never raise into a live
    request path."""


def resolve_credentials(endpoint_env: str, token_env: str) -> tuple[str, str]:
    """Resolve the orchestrator base_url + token from env-var NAMES,
    `os.environ.get(...)` at call time — never a literal. Raises
    `OrchestratorError` with an actionable message on a missing/empty env;
    the resolved token is a live secret and must never be logged, printed,
    or persisted (see `tools.orchestrate.jobs`, which never stores it)."""
    base_url = os.environ.get(endpoint_env)
    if not base_url:
        raise OrchestratorError(
            f"set ${endpoint_env} to the external training orchestrator's base URL")
    token = os.environ.get(token_env)
    if not token:
        raise OrchestratorError(
            f"set ${token_env} to the external training orchestrator's auth token")
    return base_url, token


class TrainingOrchestratorClient:
    """Trigger + track client for the external training orchestrator.

    RouteForge runs NO training compute here: `submit_job` POSTs a spec and
    returns the external job id; `get_status` GETs the current state.
    Nothing in this class streams tokens, runs a loop, or calls a model —
    it is two plain HTTP calls.
    """

    def __init__(self, base_url: str, token: str, *, client=None) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._injected_client = client

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    async def submit_job(self, spec: TrainingJobSpec) -> str:
        """POST `spec` to `{base_url}/jobs`; return the external job id/ref."""
        client, close = self._client()
        try:
            resp = await client.post(
                f"{self._base_url}/jobs",
                headers=self._headers(),
                json=spec.model_dump(mode="json"))
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise OrchestratorError(
                f"failed to submit training job to {self._base_url}: {e}") from e
        finally:
            if close:
                await client.aclose()

        ref = data.get("job_id") or data.get("id") or data.get("external_ref")
        if not ref:
            raise OrchestratorError(
                f"orchestrator response is missing a job id: {data!r}")
        return str(ref)

    async def get_status(self, external_ref: str) -> TrainingJobStatus:
        """GET `{base_url}/jobs/{external_ref}`; return the parsed status."""
        client, close = self._client()
        try:
            resp = await client.get(
                f"{self._base_url}/jobs/{external_ref}", headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise OrchestratorError(
                f"failed to fetch status for job {external_ref!r} from "
                f"{self._base_url}: {e}") from e
        finally:
            if close:
                await client.aclose()

        return TrainingJobStatus(
            external_ref=external_ref,
            state=data.get("state", "unknown"),
            result_endpoint=data.get("result_endpoint"),
            result_slug=data.get("result_slug"),
            error=data.get("error"))

    def _client(self):
        """Return `(client, close_when_done)`. `close_when_done` is False
        for an injected client — the caller (test) owns its lifecycle."""
        if self._injected_client is not None:
            return self._injected_client, False
        import httpx

        return httpx.AsyncClient(timeout=30.0), True
