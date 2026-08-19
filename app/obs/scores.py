"""Feedback scores → per-app Langfuse project via REST (F1 Phase D / B16).

Applications call `POST /v1/feedback` to attach human/eval scores (route
correctness, preferred model, failure type, quality, corrected output) to a
trace they already saw in their own Langfuse project. This posts straight to
that app's project via the Langfuse public REST API — it never routes through
the shared/default project, and it never posts anywhere when the app has no
provisioned project (fail-closed: no project, no post; see
`app.apps.langfuse_provision.load_app_langfuse`).
"""
from __future__ import annotations

import logging

log = logging.getLogger("serving.obs")

SCORE_NAMES = frozenset({
    "route_correct", "preferred_model", "failure_type", "quality",
    "corrected_output",
})


async def post_score(app_langfuse, trace_id: str, name: str, value,
                      comment: str | None = None, client=None) -> bool:
    """POST one score to `{app_langfuse.host}/api/public/scores`.

    Returns True on a 2xx response, False otherwise (no project, non-2xx, or
    any transport error) — this must never raise, since a feedback/telemetry
    failure must not break the caller's request. `client` is an injectable
    `httpx.AsyncClient` (tests); when omitted one is built and closed here,
    mirroring `LangfuseProvisioner` in `app.apps.langfuse_provision`.
    """
    if app_langfuse is None:
        log.info("feedback dropped: no project for trace %s", trace_id)
        return False

    import httpx

    body: dict = {"traceId": trace_id, "name": name}
    if isinstance(value, str):
        body["value"] = value
        body["dataType"] = "CATEGORICAL"
    else:
        body["value"] = value
        body["dataType"] = "NUMERIC"
    if comment is not None:
        body["comment"] = comment

    close = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)
    try:
        r = await client.post(
            f"{app_langfuse.host}/api/public/scores",
            auth=(app_langfuse.public_key, app_langfuse.secret_key),
            json=body)
        if 200 <= r.status_code < 300:
            return True
        log.warning("feedback score post failed for trace %s: HTTP %s",
                    trace_id, r.status_code)
        return False
    except Exception:
        log.warning("feedback score post failed for trace %s", trace_id,
                    exc_info=True)
        return False
    finally:
        if close:
            await client.aclose()
