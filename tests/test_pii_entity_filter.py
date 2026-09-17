"""Per-app PII entity denylist + score threshold (§4.2b).

The failure class: Presidio tags a benign business identifier as PII and redacts
it *inside* the trust boundary, destroying a value the consuming application
depends on -- e.g. a shop-order number `133212` classified `US_DRIVER_LICENSE`,
or a revision date `4/5/21` classified `DATE_TIME`. This is not app-specific:
any deployment whose consumers carry structured identifiers (order numbers, part
numbers, ticket ids, dates) can hit it. The only lever that existed was
`pii_detection: off`, which disables the whole gate AND is clamped away for
onprem_only apps.

This adds the narrow lever: an app names the false-positive entity TYPES to
detect-but-not-redact (and/or a confidence floor) and keeps every other
recognizer -- while staying onprem_only. The load-bearing invariant is that
US_SSN can NEVER be suppressed this way (CLAUDE.md rules 3/8).
"""
from __future__ import annotations

import asyncio

import pytest

from app.apps.policy import AppPolicyView
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Entity, Message
from app.pipeline import Pipeline
from app.policy.detectors import Detector, make_detector
from app.policy.effective import PROTECTED_ENTITIES, FilteredDetector
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry


# --- FilteredDetector unit behavior (deterministic, no Presidio needed) ------

class _StubDetector:
    """A base detector returning a fixed entity list, so FilteredDetector's
    keep/drop logic can be asserted in isolation."""

    def __init__(self, ents: list[Entity]) -> None:
        self._ents = ents

    def analyze(self, text: str) -> list[Entity]:
        return list(self._ents)

    def anonymize(self, text, entities):
        return text, {}

    def warmup(self) -> None:
        return None


def _e(t: str, score: float | None = None, start: int = 0, end: int = 1) -> Entity:
    return Entity(type=t, start=start, end=end, value="x", score=score)


def test_denylist_drops_named_types_only():
    base = _StubDetector([_e("US_DRIVER_LICENSE"), _e("DATE_TIME"), _e("PERSON")])
    det = FilteredDetector(base, denylist=["US_DRIVER_LICENSE", "DATE_TIME"])
    kept = {e.type for e in det.analyze("t")}
    assert kept == {"PERSON"}  # PERSON (not denylisted) survives


def test_denylist_can_never_suppress_us_ssn():
    base = _StubDetector([_e("US_SSN", score=1.0), _e("PERSON")])
    # Even if an operator names US_SSN, it is stripped from the denylist.
    det = FilteredDetector(base, denylist=["US_SSN"])
    kept = {e.type for e in det.analyze("t")}
    assert "US_SSN" in kept
    assert "US_SSN" in PROTECTED_ENTITIES


def test_score_threshold_drops_low_confidence_but_never_ssn_or_unscored():
    base = _StubDetector([
        _e("DATE_TIME", score=0.6),          # below 0.8 -> dropped
        _e("PERSON", score=0.85),            # above 0.8 -> kept
        _e("US_SSN", score=1.0),             # protected -> always kept
        _e("EMAIL_ADDRESS", score=None),     # unscored -> never threshold-dropped
    ])
    det = FilteredDetector(base, score_threshold=0.8)
    kept = {e.type for e in det.analyze("t")}
    assert kept == {"PERSON", "US_SSN", "EMAIL_ADDRESS"}


def test_no_config_is_a_passthrough():
    ents = [_e("PERSON"), _e("DATE_TIME", score=0.5)]
    det = FilteredDetector(_StubDetector(ents), denylist=None, score_threshold=None)
    assert det.analyze("t") == ents


# --- pii_exempt_patterns: type-independent value-shape exemption ------------
#
# Presidio assigns the SAME false-positive value DIFFERENT entity types
# depending on surrounding whitespace (a compact drawing/order number scores
# PHONE_NUMBER, the spaced form scores DATE_TIME) -- chasing types is
# whack-a-mole. pii_exempt_patterns matches the entity's VALUE instead, so it
# drops the false positive no matter which type Presidio picked.

def _v(t: str, value: str, score: float | None = None) -> Entity:
    return Entity(type=t, start=0, end=len(value), value=value, score=score)


def test_exempt_pattern_drops_matching_value_regardless_of_type():
    """The whole point: the same value, tagged two different types, is dropped
    by ONE value-shape pattern -- type-independent."""
    base = _StubDetector([
        _v("DATE_TIME", "137961-01 01"),
        _v("PHONE_NUMBER", "137961-01 01"),
    ])
    det = FilteredDetector(base, exempt_patterns=[r"\d{6}-\d{2} \d{2}"])
    assert det.analyze("t") == []


def test_exempt_pattern_can_never_drop_us_ssn():
    base = _StubDetector([_v("US_SSN", "123-45-6789", score=1.0)])
    det = FilteredDetector(base, exempt_patterns=[r"\d{3}-\d{2}-\d{4}"])
    kept = {e.type for e in det.analyze("t")}
    assert "US_SSN" in kept


def test_exempt_pattern_is_fullmatch_not_search():
    base = _StubDetector([_v("DATE_TIME", "137961-01 01 EXTRA")])
    det = FilteredDetector(base, exempt_patterns=[r"\d{6}-\d{2} \d{2}"])
    kept = {e.type for e in det.analyze("t")}
    assert kept == {"DATE_TIME"}  # trailing text breaks fullmatch -> kept


def test_exempt_pattern_leaves_non_matching_values_kept():
    base = _StubDetector([_v("EMAIL_ADDRESS", "a@b.com")])
    det = FilteredDetector(base, exempt_patterns=[r"\d{6}-\d{2} \d{2}"])
    kept = {e.type for e in det.analyze("t")}
    assert kept == {"EMAIL_ADDRESS"}


# --- end-to-end through a real Pipeline --------------------------------------

def _settings(mode: str = "regex"):
    s = load_settings()
    return s.model_copy(update={
        "serving": s.serving.model_copy(update={"pii_detection": mode})})


def _pipeline(settings) -> Pipeline:
    det = make_detector(settings.serving.pii_detection)
    registry = make_test_registry(settings)
    co = settings.serving.context_optimization
    gate = Gate(settings, det, registry=registry)
    gateway = Gateway(settings, _DeterministicLiteLLM(settings),
                      Router(settings, registry=registry), registry)
    return Pipeline(settings, gate, det, gateway,
                    co_cfg_provider=lambda: co, ccr=CCRStore(3600), registry=registry)


def _run(pipe: Pipeline, content: str, app_policy=None) -> dict:
    req = ChatRequest(messages=[Message(role="user", content=content)])
    return asyncio.run(pipe.run("req-1", "acme", req, app_policy=app_policy))


def test_denylisted_entity_survives_but_ssn_still_redacted_onprem_only():
    """Portable (regex base): an app denylists PHONE_NUMBER, so a phone number
    passes through un-redacted, while an SSN in the SAME prompt is STILL
    redacted (US_SSN is protected) and residency stays onprem_only."""
    pipe = _pipeline(_settings("regex"))
    app_policy = AppPolicyView(
        residency="onprem_only", pii_entity_denylist=frozenset({"PHONE_NUMBER"}))

    out = _run(pipe, "call me at 415-555-1212, my ssn is 482-73-1946",
               app_policy=app_policy)

    sanitized = out["sanitized_prompt"]
    assert "415-555-1212" in sanitized       # PHONE_NUMBER denylisted -> survives
    assert "482-73-1946" not in sanitized     # US_SSN protected -> still redacted
    assert "<US_SSN_1>" in sanitized
    assert out["policy"]["onprem_only"] is True          # residency untouched
    assert out["policy"]["allowed_providers"] == ["onprem"]


def test_denylist_leaves_ssn_even_when_ssn_itself_is_denylisted():
    """Belt-and-suspenders end to end: naming US_SSN in the denylist does not
    un-redact it -- the protection holds at the pipeline level, not just in the
    admin validator."""
    pipe = _pipeline(_settings("regex"))
    app_policy = AppPolicyView(pii_entity_denylist=frozenset({"US_SSN"}))
    out = _run(pipe, "ssn 482-73-1946", app_policy=app_policy)
    assert "482-73-1946" not in out["sanitized_prompt"]
    assert "<US_SSN_1>" in out["sanitized_prompt"]


def test_business_identifiers_survive_with_denylist_presidio():
    """Representative reproduction: with Presidio on, a setup-sheet header's
    shop-order number and revision date come back mangled. Denylisting
    US_DRIVER_LICENSE + DATE_TIME lets both survive while the gate (and its
    onprem_only floor) stays on."""
    pytest.importorskip("presidio_analyzer")
    pipe = _pipeline(_settings("presidio"))
    header = ("Customer: MIDDLETOWN TUBE WORKS INC.  S.O. 133212-01.  "
              "REV A: DEEPER COUNTERBORES FOR SIZING SR'S 4/5/21 TR")

    # Baseline: without the denylist Presidio destroys the two field values.
    base_out = _run(pipe, header, app_policy=AppPolicyView(residency="onprem_only"))
    assert "133212" not in base_out["sanitized_prompt"] or \
        "4/5/21" not in base_out["sanitized_prompt"]

    # With the denylist both field values pass through, onprem_only intact.
    ok = _run(pipe, header, app_policy=AppPolicyView(
        residency="onprem_only",
        pii_entity_denylist=frozenset({"US_DRIVER_LICENSE", "DATE_TIME"})))
    assert "133212" in ok["sanitized_prompt"]
    assert "4/5/21" in ok["sanitized_prompt"]
    assert ok["policy"]["onprem_only"] is True


# --- admin API validation ----------------------------------------------------

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}


def _login(client):
    assert client.post("/admin/login", json=_ADMIN).status_code == 200


def _mkapp(client, app_id):
    assert client.post("/admin/applications",
                       json={"id": app_id, "name": app_id}).status_code == 200


def test_admin_denylist_round_trips(client):
    _login(client)
    _mkapp(client, "dl-ok")
    r = client.put("/admin/applications/dl-ok/policy",
                   json={"pii_entity_denylist": ["US_DRIVER_LICENSE", "DATE_TIME"],
                         "pii_score_threshold": 0.8})
    assert r.status_code == 200, r.text
    pol = r.json()["policy"]
    assert set(pol["pii_entity_denylist"]) == {"US_DRIVER_LICENSE", "DATE_TIME"}
    assert pol["pii_score_threshold"] == 0.8


def test_admin_rejects_ssn_in_denylist(client):
    _login(client)
    _mkapp(client, "dl-ssn")
    r = client.put("/admin/applications/dl-ssn/policy",
                   json={"pii_entity_denylist": ["US_SSN"]})
    assert r.status_code == 422
    assert "US_SSN" in r.text


def test_admin_rejects_unknown_entity(client):
    _login(client)
    _mkapp(client, "dl-bad")
    r = client.put("/admin/applications/dl-bad/policy",
                   json={"pii_entity_denylist": ["NOT_A_REAL_TYPE"]})
    assert r.status_code == 422


def test_admin_rejects_threshold_out_of_range(client):
    _login(client)
    _mkapp(client, "dl-thr")
    r = client.put("/admin/applications/dl-thr/policy",
                   json={"pii_score_threshold": 1.5})
    assert r.status_code == 422


def test_admin_rejects_invalid_exempt_pattern_regex(client):
    _login(client)
    _mkapp(client, "ep-bad")
    r = client.put("/admin/applications/ep-bad/policy",
                   json={"pii_exempt_patterns": ["("]})
    assert r.status_code == 422


def test_admin_exempt_patterns_round_trip_and_move_app_policy_version(client, monkeypatch):
    """Valid patterns round-trip through the admin API PUT (200), and applying
    them moves app_policy_version on the response `/v1/chat/completions`
    consumers actually probe -- the same stale-probe guarantee the denylist
    gets, extended to the value-shape lever."""
    from app.gateway.backends import litellm_backend
    from tests._litellm_fake import CALLS, make_recording_acompletion, make_response

    token = _configure_app(client, "ep-ver", {"residency": "onprem_only"})

    CALLS.clear()
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="ok")))

    def _probe() -> str:
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"messages": [{"role": "user", "content": "ping"}]})
        assert r.status_code == 200, r.text
        v = r.json()["policy"]["app_policy_version"]
        assert v
        return v

    before = _probe()
    after_put = client.put("/admin/applications/ep-ver/policy",
                           json={"pii_exempt_patterns": [r"\d{6}-\d{2} \d{2}"]})
    assert after_put.status_code == 200, after_put.text
    assert after_put.json()["policy"]["pii_exempt_patterns"] == [r"\d{6}-\d{2} \d{2}"]
    after = _probe()
    assert before != after, "policy changed but app_policy_version did not move"


def test_admin_denylist_allowed_for_onprem_only_app(client):
    """The whole point of §4.2b: unlike pii_detection:off (clamped away for
    onprem_only), a denylist is accepted while residency stays onprem_only."""
    _login(client)
    _mkapp(client, "dl-onprem")
    r = client.put("/admin/applications/dl-onprem/policy",
                   json={"residency": "onprem_only",
                         "pii_entity_denylist": ["DATE_TIME"]})
    assert r.status_code == 200, r.text
    assert r.json()["policy"]["residency"] == "onprem_only"


# --- HTTP consumer contract: the boundary any client application actually hits -
#
# These are the tests whose ABSENCE let §4.2a ship. They drive the full
# `/v1/chat/completions` stack with a token, a configured policy, and a backend
# that records exactly what reached the model -- so "a legitimate business
# identifier this app exempted was destroyed before egress" is a failing test,
# not a customer bug report. Mirrors tests/test_openai_compat.py's recording
# pattern (the wire-shape/tool-calling half of the same contract).

def _configure_app(client, app_id: str, policy: dict) -> str:
    """Create an app, set its policy, return a working token."""
    _login(client)
    token = client.post("/admin/applications",
                        json={"id": app_id, "name": app_id}).json()["token"]
    assert client.put(f"/admin/applications/{app_id}/policy",
                      json=policy).status_code == 200, "policy write failed"
    return token


def test_http_denylisted_identifier_survives_to_egress_ssn_does_not(client, monkeypatch):
    """Full HTTP path: an app that has denylisted PHONE_NUMBER sends a prompt
    carrying a phone number (a benign business value it needs intact) AND an
    SSN. What reaches the model must contain the phone verbatim and must NOT
    contain the SSN -- the exact shape of the §4.2a failure, asserted at the
    boundary client applications call."""
    from app.gateway.backends import litellm_backend
    from tests._litellm_fake import CALLS, make_recording_acompletion, make_response

    token = _configure_app(client, "phone-app", {
        "residency": "onprem_only", "pii_entity_denylist": ["PHONE_NUMBER"]})

    CALLS.clear()
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="ok")))

    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"messages": [{"role": "user",
                          "content": "call 415-555-1212 re acct; ssn 482-73-1946"}]})
    assert r.status_code == 200, r.text

    wire = "".join(str(m.get("content") or "") for m in CALLS[-1]["messages"])
    assert "415-555-1212" in wire        # denylisted PHONE_NUMBER survived to the model
    assert "482-73-1946" not in wire      # US_SSN still redacted -- protection holds


def test_http_setup_sheet_identifiers_survive(client, monkeypatch):
    """The literal §4.2a reproduction over HTTP: an app on Presidio that has
    denylisted US_DRIVER_LICENSE + DATE_TIME sends a manufacturing setup-sheet
    header; the shop-order number and revision date must reach the model
    intact."""
    pytest.importorskip("presidio_analyzer")
    from app.gateway.backends import litellm_backend
    from tests._litellm_fake import CALLS, make_recording_acompletion, make_response

    token = _configure_app(client, "setup-sheet-app", {
        "residency": "onprem_only", "pii_detection": "presidio",
        "pii_entity_denylist": ["US_DRIVER_LICENSE", "DATE_TIME"]})

    CALLS.clear()
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="ok")))

    header = ("Customer: MIDDLETOWN TUBE WORKS INC.  S.O. 133212-01.  "
              "REV A: DEEPER COUNTERBORES FOR SIZING SR'S 4/5/21 TR")
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"messages": [{"role": "user", "content": header}]})
    assert r.status_code == 200, r.text

    wire = "".join(str(m.get("content") or "") for m in CALLS[-1]["messages"])
    assert "133212" in wire     # shop-order number reached the model intact
    assert "4/5/21" in wire      # revision date reached the model intact


# --- app_policy_version: staleness detection ---------------------------------
#
# The debugging trap this closes: a red conformance-probe banner that was true
# *before* an operator applied a denylist is byte-for-byte indistinguishable
# from a live failure, because the response `policy` block carried no version of
# the per-app policy. Diagnosing the last such case cost a manual cross-check of
# pod access-log timestamps against the config-commit time. These tests assert
# the response now carries `app_policy_version`, and that it CHANGES the instant
# the app's policy changes -- so any consumer (a client's own probe included)
# can tell a stale result from a live one.

def test_app_policy_version_is_stable_and_reacts_to_changes():
    """Unit: equal policies hash equal; any decision-relevant change flips it;
    the denylist is a set, so member ORDER must not matter."""
    a = AppPolicyView(residency="onprem_only",
                      pii_entity_denylist=frozenset({"US_DRIVER_LICENSE", "DATE_TIME"}))
    # Same policy, denylist built in the other order -> same version.
    b = AppPolicyView(residency="onprem_only",
                      pii_entity_denylist=frozenset({"DATE_TIME", "US_DRIVER_LICENSE"}))
    assert a.version() == b.version()
    assert a.version()  # non-empty

    # Each of these is a different policy and must produce a different version.
    assert AppPolicyView(residency="onprem_only").version() != a.version()  # denylist gone
    assert a.version() != AppPolicyView(
        residency="onprem_only",
        pii_entity_denylist=frozenset({"US_DRIVER_LICENSE"})).version()      # denylist shrank
    assert a.version() != AppPolicyView(
        residency="onprem_only",
        pii_entity_denylist=a.pii_entity_denylist,
        pii_score_threshold=0.8).version()                                   # floor added
    assert a.version() != AppPolicyView(
        residency="private_cloud_ok",
        pii_entity_denylist=a.pii_entity_denylist).version()                 # residency changed


def test_app_policy_version_reacts_to_exempt_patterns_order_independent():
    """pii_exempt_patterns is decision-relevant (moves the version) and, like
    the denylist, is a set -- member ORDER must not matter."""
    with_patterns = AppPolicyView(
        residency="onprem_only",
        pii_exempt_patterns=frozenset({r"\d{6}-\d{2} \d{2}", r"[A-Z]{2}-\d+"}))
    without_patterns = AppPolicyView(residency="onprem_only")
    assert with_patterns.version() != without_patterns.version()

    same_patterns_other_order = AppPolicyView(
        residency="onprem_only",
        pii_exempt_patterns=frozenset({r"[A-Z]{2}-\d+", r"\d{6}-\d{2} \d{2}"}))
    assert with_patterns.version() == same_patterns_other_order.version()


def test_response_policy_block_carries_app_policy_version():
    """The stamp reaches the response `policy` block a probe reads, and equals
    the app policy's own version()."""
    pipe = _pipeline(_settings("regex"))
    ap = AppPolicyView(residency="onprem_only",
                       pii_entity_denylist=frozenset({"PHONE_NUMBER"}))
    out = _run(pipe, "hello", app_policy=ap)
    assert out["policy"]["app_policy_version"] == ap.version()
    assert out["policy"]["app_policy_version"]  # non-empty


def test_http_app_policy_version_changes_when_denylist_changes(client, monkeypatch):
    """End to end, the guarantee that closes the stale-probe trap: probe an app,
    change its denylist over the admin API, probe again -- the response's
    app_policy_version must differ, so a cached earlier result is detectably
    stale."""
    from app.gateway.backends import litellm_backend
    from tests._litellm_fake import CALLS, make_recording_acompletion, make_response

    token = _configure_app(client, "ver-app", {
        "residency": "onprem_only", "pii_entity_denylist": ["PHONE_NUMBER"]})

    CALLS.clear()
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="ok")))

    def _probe() -> str:
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"messages": [{"role": "user", "content": "ping"}]})
        assert r.status_code == 200, r.text
        v = r.json()["policy"]["app_policy_version"]
        assert v, "app_policy_version missing from response policy block"
        return v

    before = _probe()
    # Operator applies a new denylist out-of-band (the exact real-world action).
    assert client.put("/admin/applications/ver-app/policy",
                      json={"pii_entity_denylist": ["PHONE_NUMBER", "DATE_TIME"]}
                      ).status_code == 200
    after = _probe()
    assert before != after, "policy changed but app_policy_version did not move"
