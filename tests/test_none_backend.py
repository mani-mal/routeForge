import copy

from app.config.loader import load_settings
from app.policy.context import build_context
import pytest


def _settings(backend):
    s = copy.deepcopy(load_settings())
    return s.model_copy(update={"serving": s.serving.model_copy(update={"inference_backend": backend})})


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_none_backend_drops_onprem_for_onprem_only_request():
    # An onprem_only request under inference_backend=none has NO allowed provider
    # (on-prem serving disabled, cloud excluded by residency) -> must be rejected.
    from app.models import Entity
    s = _settings("none")
    ssn = [Entity(type="US_SSN", start=0, end=11, value="x")]
    ctx = build_context(s, "r", "hospital", ssn, None)
    assert ctx.onprem_only is True
    assert ctx.allowed_providers == frozenset()   # nothing to serve on -> reject downstream


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_none_backend_public_request_unaffected_when_cloud_enabled():
    # a public request can still use cloud if a cloud provider is enabled
    s = _settings("none")
    prov = dict(s.serving.providers)
    prov["azure"] = prov["azure"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    ctx = build_context(s, "r", "acme", [], None)
    assert "onprem" not in ctx.allowed_providers   # on-prem off
    assert "azure" in ctx.allowed_providers
