"""B104/B105 behaviour through the real `/v1/chat/completions` stack.

* `off` is untouched: no advisor is constructed or called, and no
  `sp.switchyard.*` attribute, trace tag, outcome value or DecisionRecord
  `advisor` field appears.
* Failure isolation: an advisor that raises, a recording step that raises,
  and every fallback cause leave the request exactly as `off` serves it, with
  the fallback recorded.
* Langfuse tags: `switchyard`, the mode, and `agreed` | `overridden` |
  `fallback`, from the fixed vocabulary only.
* Not on a semantic-cache hit.

Most tests use a spy in place of the real `SwitchyardAdvisor`, so they need no
upstream package; the ones that need it `importorskip("switchyard")`.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

import pytest

import app.routing.switchyard as switchyard_pkg
from app.obs import export as export_mod
from app.obs.export import SWITCHYARD_TAGS
from tests.test_admin_api import _login
from tests.routing.switchyard._shadow_harness import (
    DEMO, SWITCHYARD_OUTCOME_COLUMNS, SpyAdvisor, client, columns, fallback,
    install_fake, normalize, outcomes, proposal, root_spans, switchyard_attrs,
    usage_rows,
)

HELLO = {"messages": [{"role": "user", "content": "What is the capital of France?"}]}


def _one(monkeypatch, mode: str, advisor: SpyAdvisor | None = None, req=HELLO, **kw):
    install_fake(monkeypatch)
    with client(monkeypatch, mode, advisor=advisor, **kw) as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=req)
        assert r.status_code == 200, r.text
        spans = root_spans(c)
        assert len(spans) == 1
        # Operator copy: the console snapshot of the same request.
        return {"body": r.json(), "span": spans[0],
                "snapshot": _snapshot(c, r.json()["id"]),
                "usage": [columns(u) for u in usage_rows(c)],
                "outcome": columns(outcomes(c)[0]), "app": c.app}


def _snapshot(c, request_id: str) -> dict:
    _login(c)
    snap = c.get(f"/api/requests/{request_id}")
    assert snap.status_code == 200, snap.text
    return snap.json()


def _served_hop(body: dict) -> dict:
    return [r for r in body["trace"] if r.get("step") == "router" and r.get("chosen")][-1]


# --------------------------------------------------------------------------- #
# off is untouched
# --------------------------------------------------------------------------- #
def test_off_never_constructs_or_calls_the_advisor(monkeypatch):
    built: list = []
    monkeypatch.setattr(switchyard_pkg, "SwitchyardAdvisor",
                        lambda cfg: built.append(cfg) or SpyAdvisor(proposal()))
    out = _one(monkeypatch, "off")
    assert built == []
    assert out["app"].state.switchyard_advisor is None
    assert out["app"].state.pipeline._advisor is None
    assert switchyard_attrs(out["span"]) == {}
    assert not any(k.startswith("sp.switchyard") or k.startswith("langfuse.trace.tags")
                   for k in out["span"].attributes)
    assert all("advisor" not in rec for rec in out["body"]["trace"])
    assert all("advisor" not in rec for rec in out["snapshot"]["trace"])
    assert all(out["outcome"][c] is None for c in SWITCHYARD_OUTCOME_COLUMNS)


def test_off_boot_does_not_import_upstream_switchyard():
    """A fresh process booting the app with the shipped (off) config never
    loads the upstream package."""
    code = ("import sys; from app.main import create_app; create_app(); "
            "bad = [m for m in ('switchyard', 'switchyard_rust') if m in sys.modules]; "
            "assert not bad, bad")
    env = {**os.environ, "SP_PII_DETECTION": "regex", "SP_SEED_TEST_MODELS": "1",
           "DATABASE_URL": "sqlite+aiosqlite://"}
    env.pop("SP_CONFIG_OVERLAY_DIR", None)
    subprocess.run([sys.executable, "-c", code], check=True, env=env,
                   cwd=os.path.dirname(os.path.dirname(os.path.dirname(
                       os.path.dirname(os.path.abspath(__file__))))))


# --------------------------------------------------------------------------- #
# shadow records once, after compression, on the post-gate messages
# --------------------------------------------------------------------------- #
def test_shadow_calls_once_with_the_post_gate_messages(monkeypatch):
    spy = SpyAdvisor(proposal())
    ssn = "123-45-6789"
    out = _one(monkeypatch, "shadow", spy,
               req={"messages": [{"role": "user", "content": f"my SSN is {ssn}"}]})
    assert len(spy.calls) == 1
    seen = repr(spy.calls[0])
    # Rule 1: the advisor sees the model-facing (redacted) view, never raw.
    assert ssn not in seen
    assert out["body"]["policy"]["onprem_only"] is True


# --------------------------------------------------------------------------- #
# Langfuse tags + trace attributes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("result, outcome_tag, agreed", [
    (proposal(tier="cheap"), "switchyard:agreed", True),
    (proposal(tier="frontier", role="capable"), "switchyard:overridden", False),
    (fallback("timeout"), "switchyard:fallback", None),
])
def test_trace_tags_and_attributes(monkeypatch, result, outcome_tag, agreed):
    out = _one(monkeypatch, "shadow", SpyAdvisor(result))
    # The plain question is served on the cheap tier.
    assert _served_hop(out["body"])["chosen_tier"] == "cheap"
    attrs = switchyard_attrs(out["span"])
    tags = list(attrs["langfuse.trace.tags"])
    assert tags == ["switchyard", "switchyard:shadow", outcome_tag]
    assert set(tags) <= SWITCHYARD_TAGS
    assert attrs["sp.switchyard.mode"] == "shadow"
    assert attrs["sp.switchyard.version"] == "0.4.0a1"
    assert attrs["sp.switchyard.algorithm"] == "stage_router"
    assert isinstance(attrs["sp.switchyard.latency_ms"], float)
    if agreed is None:
        assert attrs["sp.switchyard.fallback_cause"] == "timeout"
        assert "sp.switchyard.agreed" not in attrs
        assert "sp.switchyard.tier" not in attrs
    else:
        assert attrs["sp.switchyard.agreed"] is agreed
        assert attrs["sp.switchyard.tier"] == result.proposal.tier
        assert attrs["sp.switchyard.role"] == result.proposal.role
        assert attrs["sp.switchyard.confidence"] == pytest.approx(0.8)
    # The same record on the outcome row and the served DecisionRecord.
    o = out["outcome"]
    assert o["switchyard_mode"] == "shadow"
    assert o["switchyard_agreed"] is agreed
    # Never on the client body; on the operator's snapshot.
    assert all("advisor" not in rec for rec in out["body"]["trace"])
    adv = _served_hop(out["snapshot"])["advisor"]
    assert adv["agreed"] is agreed and adv["mode"] == "shadow"
    if agreed is None:
        assert o["switchyard_fallback_cause"] == "timeout" == adv["fallback_cause"]
    else:
        assert o["switchyard_tier"] == result.proposal.tier == adv["tier"]
        assert adv["evidence"] == {"source": "dimensions"}


def test_enforce_ceiling_shadows_an_app_with_no_setting(monkeypatch):
    """B108: a deployment `enforce` is a ceiling. An app with no
    `adaptive_routing` inherits it capped at `shadow`: recorded as `shadow`,
    never applied, and served exactly as `off` serves it."""
    base = _one(monkeypatch, "off")
    # A "capable" proposal would raise the tier if it were acted on.
    out = _one(monkeypatch, "enforce", SpyAdvisor(proposal(tier="frontier", role="capable")))
    assert out["body"]["model"] == base["body"]["model"]
    assert normalize(out["body"]) == normalize(base["body"])
    attrs = switchyard_attrs(out["span"])
    assert attrs["sp.switchyard.mode"] == "shadow"
    assert attrs["sp.switchyard.applied"] is False
    assert out["outcome"]["switchyard_applied"] is False
    assert list(attrs["langfuse.trace.tags"]) == [
        "switchyard", "switchyard:shadow", "switchyard:overridden"]


# --------------------------------------------------------------------------- #
# failure isolation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cause", ["timeout", "error", "unconvertible",
                                   "invalid_role", "routing_call_refused"])
def test_each_fallback_leaves_the_request_unchanged(monkeypatch, cause):
    base = _one(monkeypatch, "off")
    out = _one(monkeypatch, "shadow", SpyAdvisor(fallback(cause)))
    assert normalize(out["body"]) == normalize(base["body"])
    assert normalize(out["usage"]) == normalize(base["usage"])
    assert out["outcome"]["switchyard_fallback_cause"] == cause
    assert out["outcome"]["switchyard_agreed"] is None
    assert _served_hop(out["snapshot"])["advisor"]["fallback_cause"] == cause
    assert "switchyard:fallback" in out["span"].attributes["langfuse.trace.tags"]


def test_advisor_that_raises_is_a_recorded_fallback(monkeypatch, caplog):
    base = _one(monkeypatch, "off")
    with caplog.at_level(logging.ERROR, logger="serving.routing.switchyard"):
        out = _one(monkeypatch, "shadow", SpyAdvisor(raises=RuntimeError("boom")))
    assert normalize(out["body"]) == normalize(base["body"])
    assert out["outcome"]["switchyard_fallback_cause"] == "error"
    assert any("switchyard advisor raised" in r.getMessage() for r in caplog.records)


class _BrokenResult:
    """An advisor result whose recording raises."""

    def to_record(self):
        raise KeyError("recording bug")


def test_a_recording_bug_never_touches_the_request(monkeypatch, caplog):
    base = _one(monkeypatch, "off")
    with caplog.at_level(logging.ERROR, logger="serving.routing.switchyard"):
        out = _one(monkeypatch, "shadow", SpyAdvisor(_BrokenResult()))
    assert normalize(out["body"]) == normalize(base["body"])
    assert normalize(out["usage"]) == normalize(base["usage"])
    assert switchyard_attrs(out["span"]) == {}
    assert any("recording failed" in r.getMessage() for r in caplog.records)


def test_a_post_routing_recording_bug_never_touches_the_request(monkeypatch, caplog):
    from app.pipeline import Pipeline
    base = _one(monkeypatch, "off")
    # The served-hop lookup inside `_record_advice` raises.
    monkeypatch.setattr(Pipeline, "_record_advice",
                        staticmethod(_wrap_raising(Pipeline._record_advice)))
    with caplog.at_level(logging.ERROR, logger="serving.routing.switchyard"):
        out = _one(monkeypatch, "shadow", SpyAdvisor(proposal()))
    assert normalize(out["body"]) == normalize(base["body"])
    assert any("recording failed" in r.getMessage() for r in caplog.records)


def _wrap_raising(real):
    """`_record_advice` with a `records` list whose iteration raises -- a bug
    inside the method's own guarded block."""
    class _Exploding(list):
        def __iter__(self):
            raise RuntimeError("bug")

    def _call(request_id, advice, records, internals):
        return real(request_id, advice, _Exploding(records), internals)
    return _call


def test_a_tag_bug_drops_only_the_switchyard_attributes(monkeypatch, caplog):
    def _boom(advice):
        raise ValueError("bad tag")
    monkeypatch.setattr(export_mod, "_switchyard_tags", _boom)
    base = _one(monkeypatch, "off")
    with caplog.at_level(logging.ERROR, logger="serving.obs"):
        out = _one(monkeypatch, "shadow", SpyAdvisor(proposal()))
    assert normalize(out["body"]) == normalize(base["body"])
    # The rest of the trace still exported.
    assert out["span"].attributes["sp.model"] == base["span"].attributes["sp.model"]
    assert "langfuse.trace.tags" not in out["span"].attributes
    assert any("switchyard trace attributes dropped" in r.getMessage()
               for r in caplog.records)


def test_real_advisor_timeout_is_a_recorded_fallback(monkeypatch):
    pytest.importorskip("switchyard")
    import asyncio

    from app.routing.switchyard.advisor import SwitchyardAdvisor

    async def _slow(self, request):
        await asyncio.sleep(1)
    monkeypatch.setattr(SwitchyardAdvisor, "_run", _slow)
    base = _one(monkeypatch, "off")
    out = _one(monkeypatch, "shadow", timeout_ms=5)
    assert normalize(out["body"]) == normalize(base["body"])
    assert out["outcome"]["switchyard_fallback_cause"] == "timeout"
    attrs = switchyard_attrs(out["span"])
    assert attrs["sp.switchyard.fallback_cause"] == "timeout"
    # B104: a fallback still says which advisor build ran.
    assert attrs["sp.switchyard.version"] == "0.4.0a1"
    assert attrs["sp.switchyard.algorithm"] == "stage_router"


# --------------------------------------------------------------------------- #
# not on semantic-cache hits
# --------------------------------------------------------------------------- #
def test_not_called_on_a_semantic_cache_hit(monkeypatch):
    spy = SpyAdvisor(proposal())
    install_fake(monkeypatch)
    with client(monkeypatch, "shadow", advisor=spy, semantic_cache=True) as c:
        first = c.post("/v1/chat/completions", headers=DEMO, json=HELLO).json()
        second = c.post("/v1/chat/completions", headers=DEMO, json=HELLO).json()
        first_snap = _snapshot(c, first["id"])
        second_snap = _snapshot(c, second["id"])
        rows = outcomes(c)
        spans = root_spans(c)
    assert any(r.get("step") == "cache" and r.get("hit") for r in second["trace"]), \
        second["trace"]
    assert len(spy.calls) == 1               # the miss only
    assert "advisor" in _served_hop(first_snap)
    assert all("advisor" not in r for r in second_snap["trace"])
    hit = next(r for r in rows if r.cache_hit)
    assert all(getattr(hit, col) is None for col in SWITCHYARD_OUTCOME_COLUMNS)
    assert "sp.switchyard.mode" in spans[0].attributes
    assert not any(k.startswith("sp.switchyard") or k == "langfuse.trace.tags"
                   for k in spans[1].attributes)
