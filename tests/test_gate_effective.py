from app.config.loader import load_settings
from app.models import ChatRequest, Message
from app.policy.detectors import RegexDetector
from app.policy.effective import EffectiveDetector
from app.policy.gate import Gate
from app.policy.recognizers import CustomRecognizer
from app.apps.privacy import SensitivityRuleView


def test_gate_uses_supplied_detector_and_rules():
    S = load_settings()
    gate = Gate(S, RegexDetector())
    det = EffectiveDetector(RegexDetector(), [CustomRecognizer("MRN", r"\bMRN-\d{7}\b", 0.9)])
    rules = [SensitivityRuleView(frozenset({"MRN"}), "restricted", "high", "onprem_only", True),
             SensitivityRuleView(frozenset(), "public", "low", "any", False)]
    req = ChatRequest(model="auto", messages=[Message(role="user", content="patient MRN-1234567")])
    ctx, _red, _map = gate.evaluate("r1", "acme", req, detector=det, rules=rules)
    assert "MRN" in ctx.detected_entities and ctx.sensitivity == "restricted"
    assert ctx.onprem_only is True
