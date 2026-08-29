from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.config.schema import Settings
from app.models import ChatRequest, ExecutionPolicyContext
from app.policy.context import build_context
from app.policy.detectors import Detector

if TYPE_CHECKING:
    from app.apps.policy import AppPolicyView
    from app.apps.registry import ModelRegistry


class Gate:
    def __init__(self, settings: Settings, detector: Detector,
                 registry: "ModelRegistry | None" = None) -> None:
        self._settings = settings
        self._detector = detector
        self._registry = registry

    def evaluate(self, request_id: str, tenant_id: str, request: ChatRequest,
                 app_policy: "AppPolicyView | None" = None,
                 detector: Detector | None = None,
                 rules: list | None = None,
                 ) -> tuple[ExecutionPolicyContext, str, dict[str, str]]:
        # `detector`/`rules` let a caller supply the app's EffectiveDetector +
        # effective_rules (per-app custom recognizers/classification) instead
        # of the process-wide default -- see app/pipeline.py.
        det = detector or self._detector
        # scan the latest user message (the live prompt) + prior content.
        # `content or ""`: a tool-calling round-trip includes an assistant
        # message whose content is null (it only carries tool_calls) — joining
        # None would crash the gate before any request is served.
        text = "\n".join(m.content or "" for m in request.messages)
        entities = det.analyze(text)
        redacted, mapping = det.anonymize(text, entities)
        map_id = f"map-{uuid.uuid4().hex[:8]}" if mapping else None
        ctx = build_context(self._settings, request_id, tenant_id, entities, map_id,
                            app_policy=app_policy, app_rules=rules,
                            registry=self._registry)
        ctx = ctx.model_copy(update={
            "requested_model": None if request.model == "auto" else request.model,
        })
        return ctx, redacted, mapping
