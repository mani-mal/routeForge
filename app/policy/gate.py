from __future__ import annotations

import uuid

from app.config.schema import Settings
from app.models import ChatRequest, ExecutionPolicyContext
from app.policy.context import build_context
from app.policy.detectors import Detector


class Gate:
    def __init__(self, settings: Settings, detector: Detector) -> None:
        self._settings = settings
        self._detector = detector

    def evaluate(self, request_id: str, tenant_id: str, request: ChatRequest
                 ) -> tuple[ExecutionPolicyContext, str, dict[str, str]]:
        # scan the latest user message (the live prompt) + prior content
        text = "\n".join(m.content for m in request.messages)
        entities = self._detector.analyze(text)
        redacted, mapping = self._detector.anonymize(text, entities)
        map_id = f"map-{uuid.uuid4().hex[:8]}" if mapping else None
        ctx = build_context(self._settings, request_id, tenant_id, entities, map_id)
        return ctx, redacted, mapping
