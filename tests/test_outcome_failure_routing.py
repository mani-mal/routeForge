"""B70 items 1-2 through the real HTTP boundary: a request that reached the
router and then failed on the backend keeps its routing detail on the
`backend_failed` outcome row (eligible set, tier target, rejected, scoring,
attempt chain) -- buffered and streamed -- and a request whose only eligible
endpoint failed is told apart from a generic outage (`route_reason:
no_failover`, "no failover candidate" in the 502)."""
from __future__ import annotations

import pytest

from app.gateway.backends import litellm_backend
from tests._litellm_fake import _FakeStreamChunk
from tests.onboarding.harness import onboard
from tests.test_outcome_hooks import DEMO, HELLO, _client, _failing_backend, _only


def test_backend_failed_row_keeps_the_routing_decision(monkeypatch):
    attempted = _failing_backend(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 502, r.text
        assert "no failover candidate" not in r.text     # two endpoints tried
        row = _only(c)
    assert row.status == "backend_failed"
    assert row.attempts == attempted and len(attempted) == 2
    assert row.tier_target == "cheap"
    assert row.eligible == [attempted[0]]
    assert row.scoring and row.scoring["candidates"][0]["name"] == attempted[0]
    assert any(x["name"] == attempted[1] for x in row.rejected)
    assert row.route_reason is None
    assert row.chosen is None and row.cost_usd == 0.0


def test_only_eligible_endpoint_failing_is_a_no_failover_row(monkeypatch):
    attempted = _failing_backend(monkeypatch)
    with _client() as c:
        app = onboard(c, "nofail", allowed_tiers=["cheap"])
        r = app.chat(HELLO)
        assert r.status_code == 502, r.text
        assert "no failover candidate" in r.text
        row = _only(c)
    assert len(attempted) == 1
    assert row.status == "backend_failed"
    assert row.route_reason == "no_failover"
    assert row.eligible == attempted and row.attempts == attempted
    assert row.tier_target == "cheap"


@pytest.mark.parametrize("mode", ["stream_unverified", "incremental_verify"])
def test_streamed_backend_failed_row_keeps_the_routing_decision(monkeypatch, mode):
    attempted: list[str] = []

    async def _dies_mid_stream(model, call):
        attempted.append(model)

        async def _gen():
            yield _FakeStreamChunk(content="partial ")
            raise RuntimeError("backend connection reset")
        return _gen()

    monkeypatch.setattr(litellm_backend, "_acompletion", _dies_mid_stream)
    with _client() as c:
        app = onboard(c, f"sfail-{mode}", stream_mode=mode)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**HELLO, "stream": True}) as r:
            assert r.status_code == 200
            "".join(r.iter_text())
        row = _only(c)
    assert row.status == "backend_failed"
    assert row.attempts == attempted
    assert row.tier_target == "cheap"
    assert row.eligible == attempted
    assert row.scoring is not None
    assert row.chosen is None
