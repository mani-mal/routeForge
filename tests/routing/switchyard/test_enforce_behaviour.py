"""B108: per-app Switchyard `enforce`, end to end through `/v1/chat/completions`.

Boots the real stack (create_app, TestClient, the litellm fakes) with a
deployment ceiling and a spy in place of the real advisor, onboards nothing
new (the test pool: on-prem `local-8b` cheap, `local-32b` mid) and switches
the two static CLOUD candidates on (`azure-gpt-mid`, `bedrock-frontier`), so
"never reaches cloud" is proved against cloud candidates that really are in
the pool rather than absent from it.

Covers:
* enforce moves the served tier both ways (efficient -> cheap on a long
  request; capable -> higher on a short one), and never below the floor
  (a high-risk / `onprem_only` request, a medium-risk one);
* `onprem_only` never reaches cloud: non-stream, stream, failover and a
  semantic-cache hit;
* an app at `off` under an `enforce` ceiling is untouched; an app with no
  setting under a `shadow` ceiling still shadows;
* deterministic sampling; every advisor fallback is today's routing with
  `applied=false`; a live enforced B80 floor wins (`fallback_cause:
  quality_floor`);
* the admin API: write validation and the capped `effective` mode.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
import app.routing.switchyard as switchyard_pkg
from app.config.loader import load_settings
from app.gateway.backends import litellm_backend
from app.models import ExecutionPolicyContext
from app.pipeline import Pipeline
from app.routing.adaptive import sampled
from tests._litellm_fake import CALLS, _FakeStreamChunk, make_response
from tests.routing.switchyard._shadow_harness import (
    SSN, SpyAdvisor, columns, fallback, install_span_capture, normalize, outcomes,
    proposal, root_spans, switchyard_attrs, usage_rows,
)
from tests.test_admin_api import _login

LONG = {"messages": [{"role": "user", "content": "Summarise this log:\n" + "line ok\n" * 1500}]}
SHORT = {"messages": [{"role": "user", "content": "What is the capital of France?"}]}
HIGH = {"messages": [{"role": "user", "content": f"Patient SSN {SSN}: summarise the plan."}]}
MEDIUM = {"messages": [{"role": "user", "content": "Email jane@example.com about Q3."}]}

_CLOUD = ("azure", "bedrock")
_TIER = {"local-8b": "cheap", "local-32b": "mid", "azure-gpt-mid": "mid",
         "bedrock-frontier": "frontier"}


def _settings(ceiling: str, *, semantic_cache: bool = False):
    s = load_settings()
    sy = s.routing.switchyard.model_copy(update={"mode": ceiling, "timeout_ms": 2000})
    s = s.model_copy(update={"routing": s.routing.model_copy(update={"switchyard": sy})})
    prov = dict(s.serving.providers)
    for p in _CLOUD:
        prov[p] = prov[p].model_copy(update={"enabled": True})
    serving_update: dict = {"providers": prov}
    if semantic_cache:
        serving_update["semantic_cache"] = s.serving.semantic_cache.model_copy(
            update={"enabled": True})
    return s.model_copy(update={"serving": s.serving.model_copy(update=serving_update)})


def _fake(fail: tuple[str, ...] = ()):
    """One litellm fake for both shapes; raises for any model whose wire name
    contains one of `fail`."""
    async def _call(**kwargs):
        CALLS.append(kwargs)
        if any(f in kwargs.get("model", "") for f in fail):
            raise RuntimeError("forced endpoint failure")
        if kwargs.get("stream"):
            async def _gen():
                for d in ["Hel", "lo"]:
                    yield _FakeStreamChunk(content=d)
                yield _FakeStreamChunk(empty_choices=True,
                                       usage={"prompt_tokens": 12, "completion_tokens": 7})
            return _gen()
        return make_response(content="ok")
    return _call


@contextmanager
def _client(monkeypatch, ceiling: str, advisor: SpyAdvisor | None, *,
            semantic_cache: bool = False, fail: tuple[str, ...] = ()):
    monkeypatch.setattr(app_main, "load_settings",
                        lambda: _settings(ceiling, semantic_cache=semantic_cache))
    monkeypatch.setattr(litellm_backend, "acompletion", _fake(fail))
    if advisor is not None:
        monkeypatch.setattr(switchyard_pkg, "SwitchyardAdvisor", lambda cfg: advisor)
        monkeypatch.setattr(switchyard_pkg, "installed_version", lambda: "0.4.0a1")
    CALLS.clear()
    app = app_main.create_app()
    mem = install_span_capture(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.mem = mem  # type: ignore[attr-defined]
        _login(c)
        yield c


def _app(c, app_id: str, **policy) -> str:
    """Create an application with `policy` and return a live token for it."""
    r = c.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code == 200, r.text
    tok = c.post(f"/admin/applications/{app_id}/tokens", json={"name": "t"})
    assert tok.status_code == 200, tok.text
    if policy:
        r = c.put(f"/admin/applications/{app_id}/policy", json=policy)
        assert r.status_code == 200, r.text
    return tok.json()["token"]


def _post(c, token: str, req: dict):
    return c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                  json=req)


def _served(body: dict) -> dict:
    return [r for r in body["trace"] if r.get("step") == "router" and r.get("chosen")][-1]


def _snapshot_advisor(c, request_id: str) -> dict:
    snap = c.get(f"/api/requests/{request_id}").json()
    return _served(snap)["advisor"]


def _wire_models() -> list[str]:
    return [k.get("model", "") for k in CALLS]


def _no_cloud_call() -> bool:
    return not any(m.startswith(("azure/", "bedrock/")) for m in _wire_models())


# --------------------------------------------------------------------------- #
# enforce moves the tier, inside policy
# --------------------------------------------------------------------------- #
def test_efficient_serves_a_long_low_risk_request_cheap(monkeypatch):
    with _client(monkeypatch, "enforce", SpyAdvisor(proposal(tier="cheap"))) as c:
        today = _post(c, _app(c, "today", adaptive_routing="off"), LONG).json()
        tok = _app(c, "enf", adaptive_routing="enforce")
        r = _post(c, tok, LONG)
        assert r.status_code == 200, r.text
        body = r.json()
        adv = _snapshot_advisor(c, body["id"])
        row = columns([o for o in outcomes(c) if o.request_id == body["id"]][0])
        attrs = switchyard_attrs(root_spans(c)[-1])
    assert _served(today)["chosen_tier"] in ("mid", "frontier")
    assert _served(body)["chosen_tier"] == "cheap"
    assert body["model"] == "local-8b"
    # Named on the client's policy block (an enum); the record is not.
    assert body["policy"]["adaptive_routing"] == "enforce"
    assert "adaptive_routing" not in today["policy"]
    assert all("advisor" not in rec for rec in body["trace"])
    assert adv["applied"] is True and adv["mode"] == "enforce" and adv["agreed"] is True
    assert row["switchyard_applied"] is True and row["switchyard_mode"] == "enforce"
    assert row["route_reason"] == "advisor"        # not "other"
    assert attrs["sp.switchyard.applied"] is True
    assert list(attrs["langfuse.trace.tags"]) == [
        "switchyard", "switchyard:enforce", "switchyard:agreed"]


def test_capable_raises_a_short_request(monkeypatch):
    with _client(monkeypatch, "enforce",
                 SpyAdvisor(proposal(tier="frontier", role="capable"))) as c:
        today = _post(c, _app(c, "today", adaptive_routing="off"), SHORT).json()
        body = _post(c, _app(c, "enf", adaptive_routing="enforce"), SHORT).json()
        row = columns([o for o in outcomes(c) if o.request_id == body["id"]][0])
    assert _served(today)["chosen_tier"] == "cheap"
    assert _served(body)["chosen_tier"] == "frontier"
    assert body["model"] == "bedrock-frontier"
    # The advisor raised it -- not misattributed to the risk floor.
    assert row["route_reason"] == "advisor"


def test_efficient_on_a_high_risk_onprem_only_request_stays_at_the_floor(monkeypatch):
    with _client(monkeypatch, "enforce", SpyAdvisor(proposal(tier="cheap"))) as c:
        body = _post(c, _app(c, "enf", adaptive_routing="enforce"), HIGH).json()
        adv = _snapshot_advisor(c, body["id"])
    assert body["policy"]["onprem_only"] is True
    # High risk floors to frontier, clamped by placement to the best on-prem.
    assert body["model"] == "local-32b"
    assert adv["applied"] is True and adv["agreed"] is False
    assert _no_cloud_call()


def test_efficient_on_a_medium_risk_request_stays_at_the_floor(monkeypatch):
    with _client(monkeypatch, "enforce", SpyAdvisor(proposal(tier="cheap"))) as c:
        body = _post(c, _app(c, "enf", adaptive_routing="enforce"), MEDIUM).json()
    assert _served(body)["chosen_tier"] == "mid"


# --------------------------------------------------------------------------- #
# onprem_only never reaches cloud
# --------------------------------------------------------------------------- #
def test_onprem_only_capable_non_stream_stays_onprem(monkeypatch):
    with _client(monkeypatch, "enforce",
                 SpyAdvisor(proposal(tier="frontier", role="capable"))) as c:
        body = _post(c, _app(c, "enf", adaptive_routing="enforce"), HIGH).json()
    assert body["policy"]["onprem_only"] is True
    assert _served(body)["chosen_provider"] == "onprem"
    assert body["model"] == "local-32b"
    assert _no_cloud_call()


def test_onprem_only_capable_stream_stays_onprem(monkeypatch):
    """`onprem_only` is floored to `verified_then_stream`; whichever path
    serves it, the model is on-prem."""
    with _client(monkeypatch, "enforce",
                 SpyAdvisor(proposal(tier="frontier", role="capable"))) as c:
        tok = _app(c, "enf", adaptive_routing="enforce", stream_mode="stream_unverified")
        r = _post(c, tok, {**HIGH, "stream": True})
        assert r.status_code == 200, r.text
        frames = [json.loads(b.strip()[len("data: "):])
                  for b in r.text.strip("\n").split("\n\n")
                  if b.strip() != "data: [DONE]"]
    assert {f["model"] for f in frames} == {"local-32b"}
    assert _no_cloud_call()


def test_real_token_stream_takes_the_advisor(monkeypatch):
    """`run_stream` threads `advisor_tier` too: a long request on a
    `stream_unverified` enforce app is streamed from the cheap model."""
    with _client(monkeypatch, "enforce", SpyAdvisor(proposal(tier="cheap"))) as c:
        tok = _app(c, "enf", adaptive_routing="enforce", stream_mode="stream_unverified")
        r = _post(c, tok, {**LONG, "stream": True})
        assert r.status_code == 200, r.text
    assert _wire_models() and all("local-8b" in m for m in _wire_models())


def test_onprem_only_failover_never_reaches_cloud(monkeypatch):
    """The on-prem floor model fails. Failover may not step below the floor
    (cheap) and may never reach cloud: the request fails, it is not served
    elsewhere."""
    with _client(monkeypatch, "enforce",
                 SpyAdvisor(proposal(tier="frontier", role="capable")),
                 fail=("local-32b",)) as c:
        r = _post(c, _app(c, "enf", adaptive_routing="enforce"), HIGH)
    assert r.status_code == 502, r.text
    assert _no_cloud_call()
    assert all("local-32b" in m for m in _wire_models())


def test_failover_reselects_with_the_advisor_tier(monkeypatch):
    """A low-risk app with a `capable` proposal: the frontier model fails,
    the re-select keeps the advisor's target and climbs/steps within policy
    -- here to the cloud mid peer or on-prem mid, never below the hard floor."""
    with _client(monkeypatch, "enforce",
                 SpyAdvisor(proposal(tier="frontier", role="capable")),
                 fail=("bedrock",)) as c:
        body = _post(c, _app(c, "enf", adaptive_routing="enforce"), SHORT).json()
    assert _served(body)["chosen_tier"] == "mid"
    assert _wire_models()[0].startswith("bedrock/")


def test_onprem_only_semantic_cache_hit_stays_onprem(monkeypatch):
    """A cloud-served answer is cached for a `residency: any` app; an
    `onprem_only` app asking the same thing must never be handed it (the
    cache's own eligibility re-check against THIS request's policy set -- the
    advisor's target plays no part in it), and its own on-prem answer is what
    a later hit serves. (High-risk requests bypass the cache by default, so
    `onprem_only` here comes from the app's residency.)"""
    spy = SpyAdvisor(proposal(tier="frontier", role="capable"))
    with _client(monkeypatch, "enforce", spy, semantic_cache=True) as c:
        cloud_tok = _app(c, "cloud", adaptive_routing="enforce")
        onprem_tok = _app(c, "onprem", adaptive_routing="enforce", residency="onprem_only")
        cloud = _post(c, cloud_tok, SHORT).json()
        n_cloud_calls = len(CALLS)
        first = _post(c, onprem_tok, SHORT).json()
        second = _post(c, onprem_tok, SHORT).json()
        onprem_calls = _wire_models()[n_cloud_calls:]
    assert cloud["model"] == "bedrock-frontier"
    assert first["policy"]["onprem_only"] is True
    assert first["model"] == second["model"] == "local-32b"
    assert any(r.get("step") == "cache" and r.get("hit") for r in second["trace"])
    assert not any(m.startswith(("azure/", "bedrock/")) for m in onprem_calls)
    assert len(spy.calls) == 2          # the two misses; no advice on a hit


# --------------------------------------------------------------------------- #
# who gets what
# --------------------------------------------------------------------------- #
def test_app_off_under_an_enforce_ceiling_is_untouched(monkeypatch):
    with _client(monkeypatch, "off", None) as c:
        base = _post(c, _app(c, "a", adaptive_routing="off"), LONG).json()
        base_usage = [columns(u) for u in usage_rows(c)]
    spy = SpyAdvisor(proposal(tier="cheap"))
    with _client(monkeypatch, "enforce", spy) as c:
        body = _post(c, _app(c, "a", adaptive_routing="off"), LONG).json()
        usage = [columns(u) for u in usage_rows(c)]
        span = root_spans(c)[-1]
        row = columns(outcomes(c)[-1])
    assert spy.calls == []
    assert normalize(body) == normalize(base)
    assert normalize(usage) == normalize(base_usage)
    assert switchyard_attrs(span) == {}
    assert row["switchyard_mode"] is None and row["switchyard_applied"] is None


def test_app_with_no_setting_under_a_shadow_ceiling_still_shadows(monkeypatch):
    spy = SpyAdvisor(proposal(tier="cheap"))
    with _client(monkeypatch, "shadow", spy) as c:
        body = _post(c, _app(c, "a"), LONG).json()
        adv = _snapshot_advisor(c, body["id"])
    assert len(spy.calls) == 1
    assert adv["mode"] == "shadow" and adv["applied"] is False
    assert _served(body)["chosen_tier"] != "cheap"
    assert "adaptive_routing" not in body["policy"]


def test_enforce_app_under_a_shadow_ceiling_is_capped(monkeypatch):
    with _client(monkeypatch, "shadow", SpyAdvisor(proposal(tier="cheap"))) as c:
        body = _post(c, _app(c, "a", adaptive_routing="enforce"), LONG).json()
        adv = _snapshot_advisor(c, body["id"])
        eff = c.get("/admin/applications/a").json()["effective"]
    assert adv["mode"] == "shadow" and adv["applied"] is False
    assert _served(body)["chosen_tier"] != "cheap"
    assert eff["adaptive_routing"] == "shadow"


def test_sampling_is_deterministic_per_request(monkeypatch):
    with _client(monkeypatch, "enforce", SpyAdvisor(proposal(tier="cheap"))) as c:
        tok = _app(c, "s", adaptive_routing="enforce", adaptive_routing_sample_rate=0.5)
        bodies = [_post(c, tok, LONG).json() for _ in range(16)]
        rows = {o.request_id: columns(o) for o in outcomes(c)}
    applied = []
    for b in bodies:
        want = sampled(b["id"], 0.5)
        assert rows[b["id"]]["switchyard_applied"] is want
        assert rows[b["id"]]["switchyard_mode"] == "enforce"
        assert (_served(b)["chosen_tier"] == "cheap") is want
        applied.append(want)
    assert any(applied) and not all(applied)   # 16 ids: both sides reached


# --------------------------------------------------------------------------- #
# fallbacks are today's routing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("result", [
    fallback("timeout"), fallback("error"), fallback("invalid_role"),
    fallback("unconvertible"), fallback("routing_call_refused"), "raises",
])
def test_every_fallback_is_todays_routing(monkeypatch, result):
    advisor = (SpyAdvisor(raises=RuntimeError("boom")) if result == "raises"
               else SpyAdvisor(result))
    with _client(monkeypatch, "enforce", advisor) as c:
        base = _post(c, _app(c, "base", adaptive_routing="off"), LONG).json()
        base_calls = list(CALLS)
        CALLS.clear()
        body = _post(c, _app(c, "enf", adaptive_routing="enforce"), LONG).json()
        adv = _snapshot_advisor(c, body["id"])
        row = columns([o for o in outcomes(c) if o.request_id == body["id"]][0])
    assert body["model"] == base["model"]
    assert ([{k: v for k, v in call.items() if k != "metadata"} for call in CALLS]
            == [{k: v for k, v in call.items() if k != "metadata"} for call in base_calls])
    assert body["usage"] == base["usage"]
    assert adv["applied"] is False and adv["fallback_cause"]
    assert row["switchyard_applied"] is False and row["switchyard_fallback_cause"]


def test_enforced_quality_floor_wins_over_the_advisor():
    """`_advise` with a live B80 floor in `enforce`: the proposal is recorded
    with `fallback_cause: quality_floor` and NOT passed to the router."""
    import asyncio
    spy = SpyAdvisor(proposal(tier="cheap"))
    ctx = ExecutionPolicyContext(
        request_id="req-1", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=frozenset({"onprem"}), allowed_models=frozenset({"local-8b"}),
        max_cost_usd=1.0, max_latency_ms=1000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None,
        quality_floor=0.7, quality_floor_mode="enforce", adaptive_routing="enforce")
    fake_self = SimpleNamespace(_advisor=spy)
    internals = {"outcome_ctx": {}}
    advice, tier = asyncio.run(Pipeline._advise(fake_self, "req-1", ctx, [], internals))
    assert tier is None
    assert advice["applied"] is False and advice["fallback_cause"] == "quality_floor"
    assert advice["tier"] == "cheap"          # the proposal is still recorded
    assert internals["outcome_ctx"]["switchyard"]["fallback_cause"] == "quality_floor"
    # Floor in shadow: the seam applies.
    ctx2 = ctx.model_copy(update={"quality_floor_mode": "shadow"})
    _advice, tier = asyncio.run(Pipeline._advise(fake_self, "req-1", ctx2, [], None))
    assert tier == "cheap"


def test_advise_off_and_no_advisor_return_nothing():
    import asyncio
    spy = SpyAdvisor(proposal(tier="cheap"))
    ctx = ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=frozenset(), allowed_models=frozenset(), max_cost_usd=1.0,
        max_latency_ms=1000, risk_class="low", require_verification=False,
        detected_entities=(), redaction_map_id=None)          # adaptive_routing off
    assert asyncio.run(Pipeline._advise(SimpleNamespace(_advisor=spy), "r", ctx, [],
                                        None)) == (None, None)
    assert spy.calls == []
    on = ctx.model_copy(update={"adaptive_routing": "enforce"})
    assert asyncio.run(Pipeline._advise(SimpleNamespace(_advisor=None), "r", on, [],
                                        None)) == (None, None)


# --------------------------------------------------------------------------- #
# admin API
# --------------------------------------------------------------------------- #
def test_admin_write_validation_and_effective_mode(monkeypatch):
    with _client(monkeypatch, "shadow", SpyAdvisor(proposal())) as c:
        _app(c, "a")
        put = lambda body: c.put("/admin/applications/a/policy", json=body)  # noqa: E731
        for bad in ({"adaptive_routing": "ENFORCE"}, {"adaptive_routing": "on"},
                    {"adaptive_routing": 1}, {"adaptive_routing_sample_rate": 1.5},
                    {"adaptive_routing_sample_rate": -0.1},
                    {"adaptive_routing_sample_rate": "0.5"},
                    {"adaptive_routing_sample_rate": True}):
            r = put(bad)
            assert r.status_code == 422, (bad, r.text)
            assert "adaptive_routing" in r.text
        v0 = c.get("/admin/applications/a").json()
        assert v0["effective"]["adaptive_routing"] == "shadow"     # inherited, capped
        assert v0["effective"]["adaptive_routing_sample_rate"] == 1.0
        # enforce above a shadow ceiling is accepted, and shown capped.
        r = put({"adaptive_routing": "enforce", "adaptive_routing_sample_rate": 0.25})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["policy"]["adaptive_routing"] == "enforce"
        assert d["policy"]["adaptive_routing_sample_rate"] == 0.25
        assert d["effective"]["adaptive_routing"] == "shadow"
        assert d["effective"]["adaptive_routing_sample_rate"] == 0.25
        r = put({"adaptive_routing": "off"})
        assert r.json()["effective"]["adaptive_routing"] == "off"
        r = put({"reset": ["adaptive_routing", "adaptive_routing_sample_rate"]})
        assert r.status_code == 200, r.text
        assert r.json()["policy"]["adaptive_routing"] is None
        assert r.json()["effective"]["adaptive_routing"] == "shadow"


def test_setting_is_folded_into_app_policy_version(monkeypatch):
    with _client(monkeypatch, "enforce", SpyAdvisor(proposal())) as c:
        tok = _app(c, "a")
        v1 = _post(c, tok, SHORT).json()["policy"]["app_policy_version"]
        c.put("/admin/applications/a/policy", json={"adaptive_routing": "enforce"})
        v2 = _post(c, tok, SHORT).json()["policy"]["app_policy_version"]
        c.put("/admin/applications/a/policy", json={"adaptive_routing_sample_rate": 0.5})
        v3 = _post(c, tok, SHORT).json()["policy"]["app_policy_version"]
    assert len({v1, v2, v3}) == 3


def test_setting_never_moves_placement_or_protection(monkeypatch):
    """Rule 2a: the per-axis certificate on the response is identical for an
    app at every adaptive_routing value."""
    keys = ("placement", "placement_sources", "model_input_mode", "client_output_mode",
            "telemetry_io_mode", "detection_mode", "stream_mode", "allowed_providers",
            "onprem_only", "sensitivity", "detected_entities")
    seen = []
    with _client(monkeypatch, "enforce", SpyAdvisor(proposal(tier="cheap"))) as c:
        for i, mode in enumerate(("off", "shadow", "enforce")):
            tok = _app(c, f"a{i}", adaptive_routing=mode)
            pol = _post(c, tok, HIGH).json()["policy"]
            seen.append({k: pol.get(k) for k in keys})
    assert seen[0] == seen[1] == seen[2]
    assert seen[0]["onprem_only"] is True
