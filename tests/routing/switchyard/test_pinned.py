"""B115: a request that pins a model is never advised.

A pinned request (a slug, or a per-app `model_aliases` alias already resolved
to one) gives the router no tier choice, so the advisor has nothing to say.
The pipeline must not call it (no latency spent) and must record a
content-free `pinned` fallback: no role or tier, `applied` false, `agreed`
null, Langfuse tag `switchyard:fallback`. `auto` is advised exactly as
before. Shadow and enforce alike; `off` records nothing.

Driven through the real `/v1/chat/completions` stack (the B108 enforce
harness: cloud providers on, a spy in place of the real advisor) on the
bootstrap `default` app, with the spy proposing `capable`/frontier -- a tier
that, were it applied, would move an `auto` request to `bedrock-frontier`.
"""
from __future__ import annotations

import json

import pytest

from tests._litellm_fake import CALLS
from tests.routing.switchyard._shadow_harness import (
    DEMO, SpyAdvisor, columns, normalize, outcomes, proposal, root_spans,
    switchyard_attrs,
)
from tests.routing.switchyard.test_enforce_behaviour import (
    SHORT, _client, _served, _snapshot_advisor,
)

ALIAS = "role.planner"
PIN = "local-8b"
# (what the client sends as `model`, the slug it pins)
PINS = [pytest.param(PIN, id="slug"), pytest.param(ALIAS, id="alias")]


def _configure(c, **policy) -> None:
    r = c.put("/admin/applications/default/policy",
              json={"model_aliases": {ALIAS: PIN}, **policy})
    assert r.status_code == 200, r.text


def _frames(text: str) -> list[dict]:
    return [json.loads(b.strip()[len("data: "):])
            for b in text.strip("\n").split("\n\n") if b.strip() != "data: [DONE]"]


def _run(monkeypatch, ceiling: str, model: str, *, stream: bool = False, **policy) -> dict:
    spy = SpyAdvisor(proposal(tier="frontier", role="capable"))
    with _client(monkeypatch, ceiling, None if ceiling == "off" else spy) as c:
        if stream:
            policy.setdefault("stream_mode", "stream_unverified")
        _configure(c, **policy)
        r = c.post("/v1/chat/completions", headers=DEMO,
                   json={**SHORT, "model": model, "stream": stream})
        assert r.status_code == 200, r.text
        if stream:
            body = _frames(r.text)
            request_id = body[0]["id"]
        else:
            body = r.json()
            request_id = body["id"]
        rows = [columns(o) for o in outcomes(c) if o.request_id == request_id]
        spans = root_spans(c)
        out = {"body": body, "row": rows[0], "spy": spy,
               "wire": [{k: v for k, v in call.items() if k != "metadata"}
                        for call in CALLS],
               "attrs": switchyard_attrs(spans[-1]) if spans else {}}
        if ceiling != "off" and policy.get("adaptive_routing") != "off":
            out["advisor"] = _snapshot_advisor(c, request_id)
        return out


def _assert_pinned(out: dict, mode: str) -> None:
    assert out["spy"].calls == []                       # never asked, no latency spent
    row, adv, attrs = out["row"], out["advisor"], out["attrs"]
    assert row["switchyard_fallback_cause"] == "pinned"
    assert row["switchyard_mode"] == mode
    assert row["switchyard_role"] is None and row["switchyard_tier"] is None
    assert row["switchyard_agreed"] is None and row["switchyard_applied"] is False
    assert row["requested_model"] == PIN
    assert adv["fallback_cause"] == "pinned"
    assert adv["role"] is None and adv["tier"] is None and adv["evidence"] == {}
    assert adv["agreed"] is None and adv["applied"] is False
    assert attrs["sp.switchyard.fallback_cause"] == "pinned"
    assert attrs["sp.switchyard.applied"] is False
    assert "sp.switchyard.agreed" not in attrs and "sp.switchyard.tier" not in attrs
    assert list(attrs["langfuse.trace.tags"]) == [
        "switchyard", f"switchyard:{mode}", "switchyard:fallback"]


# --------------------------------------------------------------------------- #
# shadow
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model", PINS)
def test_shadow_pinned_is_not_advised_and_body_is_identical_to_off(monkeypatch, model):
    base = _run(monkeypatch, "off", model)
    CALLS.clear()
    out = _run(monkeypatch, "shadow", model)
    _assert_pinned(out, "shadow")
    assert out["body"]["model"] == PIN
    assert normalize(out["body"]) == normalize(base["body"])
    assert out["wire"] == base["wire"]
    # `off` records nothing.
    assert base["row"]["switchyard_mode"] is None
    assert base["row"]["switchyard_fallback_cause"] is None
    assert base["attrs"] == {}


def test_shadow_auto_is_advised_as_before(monkeypatch):
    out = _run(monkeypatch, "shadow", "auto")
    assert len(out["spy"].calls) == 1
    assert out["row"]["switchyard_fallback_cause"] is None
    assert out["row"]["switchyard_tier"] == "frontier"
    assert out["row"]["switchyard_agreed"] is False      # served cheap, proposed frontier
    assert out["row"]["requested_model"] is None
    assert "switchyard:overridden" in out["attrs"]["langfuse.trace.tags"]


# --------------------------------------------------------------------------- #
# enforce
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model", PINS)
def test_enforce_pinned_is_not_advised_and_router_untouched(monkeypatch, model):
    base = _run(monkeypatch, "enforce", model, adaptive_routing="off")
    CALLS.clear()
    out = _run(monkeypatch, "enforce", model, adaptive_routing="enforce")
    _assert_pinned(out, "enforce")
    # A `capable` proposal applied would have served bedrock-frontier.
    assert out["body"]["model"] == base["body"]["model"] == PIN
    assert _served(out["body"])["chosen_tier"] == "cheap"
    assert out["wire"] == base["wire"]
    assert out["body"]["usage"] == base["body"]["usage"]
    assert out["row"]["route_reason"] != "advisor"


def test_enforce_auto_is_advised_and_applied_as_before(monkeypatch):
    out = _run(monkeypatch, "enforce", "auto", adaptive_routing="enforce")
    assert len(out["spy"].calls) == 1
    assert out["row"]["switchyard_applied"] is True
    assert out["body"]["model"] == "bedrock-frontier"


# --------------------------------------------------------------------------- #
# stream
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model", PINS)
def test_shadow_stream_pinned_is_not_advised(monkeypatch, model):
    base = _run(monkeypatch, "off", model, stream=True)
    CALLS.clear()
    out = _run(monkeypatch, "shadow", model, stream=True)
    _assert_pinned(out, "shadow")
    assert {f["model"] for f in out["body"]} == {PIN}
    assert normalize(out["body"]) == normalize(base["body"])
    assert out["wire"] == base["wire"]


@pytest.mark.parametrize("model", PINS)
def test_enforce_stream_pinned_is_not_advised(monkeypatch, model):
    out = _run(monkeypatch, "enforce", model, stream=True, adaptive_routing="enforce")
    _assert_pinned(out, "enforce")
    assert {f["model"] for f in out["body"]} == {PIN}
    assert out["wire"] and all(PIN in w["model"] for w in out["wire"])


def test_stream_auto_is_advised_as_before(monkeypatch):
    out = _run(monkeypatch, "shadow", "auto", stream=True)
    assert len(out["spy"].calls) == 1
    assert out["row"]["switchyard_tier"] == "frontier"
    assert out["row"]["switchyard_fallback_cause"] is None
