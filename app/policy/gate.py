from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.config.schema import Settings
from app.models import ChatRequest, ExecutionPolicyContext
from app.policy.context import build_context
from app.policy.detectors import Detector

if TYPE_CHECKING:
    from app.apps.policy import AppPolicyView


class Gate:
    def __init__(self, settings: Settings, detector: Detector) -> None:
        self._settings = settings
        self._detector = detector

    def evaluate(self, request_id: str, tenant_id: str, request: ChatRequest,
                 app_policy: "AppPolicyView | None" = None,
                 detector: Detector | None = None,
                 rules: list | None = None,
                 ) -> tuple[ExecutionPolicyContext, str, dict[str, str]]:
        # `detector`/`rules` let a caller supply the app's EffectiveDetector +
        # effective_rules (per-app custom recognizers/classification) instead
        # of the process-wide default -- see app/pipeline.py.
        det = detector or self._detector
        # scan the latest user message (the live prompt) + prior content
        text = "\n".join(m.content for m in request.messages)
        entities = det.analyze(text)
        redacted, mapping = det.anonymize(text, entities)
        map_id = f"map-{uuid.uuid4().hex[:8]}" if mapping else None
        ctx = build_context(self._settings, request_id, tenant_id, entities, map_id,
                            app_policy=app_policy, app_rules=rules)
        return ctx, redacted, mapping
