"""B87: a row the registry reports as disabled is rejected whatever its provider."""
import pytest

from app.config.schema import PoolCandidate
from app.policy.feasible import (
    REASON_DISABLED, DataHandling, candidate_rejection, is_disabled,
)


class _Registry:
    def __init__(self, disabled: set[str]):
        self._disabled = disabled

    def is_enabled(self, slug: str) -> bool:
        return slug not in self._disabled

    def is_allowed_for_app(self, slug: str, app_id: str | None) -> bool:
        return True


@pytest.mark.parametrize("provider", ["onprem", "azure", "bedrock"])
def test_disabled_row_of_each_provider_kind_is_rejected(provider):
    c = PoolCandidate(name=f"m-{provider}", provider=provider, tier="cheap")
    reg = _Registry({c.name})
    assert is_disabled(c, reg)
    r = candidate_rejection(
        c, allowed_providers=frozenset({"onprem", "azure", "bedrock"}),
        onprem_only=False, in_scope=lambda _c: True, requested_model=None,
        exclude=frozenset(), registry=reg, tenant_id="acme",
        data_handling=DataHandling())
    assert r is not None and r.reason == REASON_DISABLED


@pytest.mark.parametrize("provider", ["onprem", "azure", "bedrock"])
def test_enabled_row_is_not_disabled(provider):
    c = PoolCandidate(name="x", provider=provider, tier="cheap")
    assert not is_disabled(c, _Registry(set()))
    assert not is_disabled(c, None)
