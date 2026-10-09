"""B72: the trajectory-scoped policy envelope (app/policy/trajectory.py).

A placement raised by a detected identifier used to exist for one request. The
envelope carries the entity TYPES a trajectory has seen to its later calls, so
a client that sends a summary without the identifier is still routed as the
restricted conversation it is. These tests state the properties over generated
EVENT SEQUENCES, then pin the three-turn conversation end to end."""
import asyncio
import datetime as dt

import pytest
from hypothesis import given, settings as hsettings, strategies as st

from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.models import ChatRequest, Message
from app.policy.compile import compile_policy
from app.policy.detectors import RegexDetector
from app.policy.gate import Gate
from app.policy.trajectory import (
    ANONYMOUS_KEY, InMemoryTrajectoryStore, Trajectory, parse_trajectory_id,
    resolve_trajectory_mode, trajectory_key)

SSN = "482-73-1946"
SETTINGS = load_settings()
GATE = Gate(SETTINGS, RegexDetector())
T0 = dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc)


def _policy(mode):
    return AppPolicyView(residency="any", trajectory_mode=mode, pii_detection="regex")


async def _call(store, app_id, traj_id, mode, text, now):
    """One request through the same three steps the pipeline runs."""
    eff = compile_policy(_policy(mode), SETTINGS)
    tid, source = parse_trajectory_id(traj_id)
    handle = Trajectory(store, app_id, tid, source)
    inherited = await handle.inherited(eff.trajectory_mode, now)
    ctx, _, eff2 = GATE.evaluate("req", app_id, ChatRequest(
        messages=[Message(role="user", content=text)]),
        app_policy=_policy(mode), eff=eff, inherited_entity_types=inherited)
    await handle.observe(eff.trajectory_mode, ctx.request_entities, now)
    return ctx, eff2


def _run(coro):
    return asyncio.run(coro)


# --- parsing and keys --------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, (None, "none")), ("", (None, "none")),
    ("task-42", ("task-42", "client")), ("  a.b:c_1  ", ("a.b:c_1", "client")),
    ("has space", (None, "invalid")), ("x" * 129, (None, "invalid")),
    ("semi;colon", (None, "invalid")),
])
def test_parse_trajectory_id(raw, expected):
    assert parse_trajectory_id(raw) == expected


def test_key_is_scoped_to_the_application_and_hides_the_id():
    assert trajectory_key("a", "t1") != trajectory_key("b", "t1")
    assert "t1" not in trajectory_key("a", "t1")
    assert trajectory_key("a", None) == ANONYMOUS_KEY


@pytest.mark.parametrize("stored,expected", [
    (None, ("off", True)), ("off", ("off", True)), ("header", ("header", True)),
    ("sticky", ("sticky", True)), ("HEADER", ("sticky", False)), (1, ("sticky", False)),
])
def test_unparseable_mode_denies_to_the_protective_end(stored, expected):
    assert resolve_trajectory_mode(stored) == expected


def test_a_stored_bad_mode_is_reported_by_the_compiler():
    eff = compile_policy(_policy("nonsense"), SETTINGS)
    assert eff.trajectory_mode == "sticky"
    assert ("trajectory_mode", "nonsense") in eff.rejected


# --- the three-turn conversation ---------------------------------------------

def _conversation(mode, last_id):
    async def go():
        store = InMemoryTrajectoryStore()
        t1, _ = await _call(store, "app", "conv-1", mode, "what is a deductible?", T0)
        t2, _ = await _call(store, "app", "conv-1", mode, f"tool result: ssn {SSN}", T0)
        t3, _ = await _call(store, "app", last_id, mode,
                            "summary so far: the claim is open. draft a note.", T0)
        return t1, t2, t3
    return _run(go())


def test_off_mode_loses_the_restriction_on_a_compacted_turn():
    """Today's behaviour, pinned: the gap the envelope exists to close."""
    t1, t2, t3 = _conversation("off", "conv-1")
    assert (t1.onprem_only, t2.onprem_only, t3.onprem_only) == (False, True, False)
    assert t3.inherited_entities == ()


def test_header_mode_keeps_the_restriction_on_a_compacted_turn():
    t1, t2, t3 = _conversation("header", "conv-1")
    assert (t1.onprem_only, t2.onprem_only, t3.onprem_only) == (False, True, True)
    assert "US_SSN" in t3.inherited_entities
    assert "US_SSN" in t3.detected_entities      # what replay reads
    assert t3.sensitivity == t2.sensitivity
    assert t3.require_verification == t2.require_verification


def test_header_mode_is_escaped_by_a_new_id_and_sticky_is_not():
    """The stated limit of `header`, and what `sticky` is for."""
    assert _conversation("header", "conv-2")[2].onprem_only is False
    assert _conversation("header", None)[2].onprem_only is False
    assert _conversation("sticky", "conv-2")[2].onprem_only is True
    assert _conversation("sticky", None)[2].onprem_only is True


def test_inherited_placement_is_named_on_the_compiled_policy():
    _, _, t3 = _conversation("header", "conv-1")
    async def go():
        store = InMemoryTrajectoryStore()
        await _call(store, "app", "c", "header", f"ssn {SSN}", T0)
        return await _call(store, "app", "c", "header", "a clean follow-up", T0)
    ctx, eff = _run(go())
    assert ctx.onprem_only is True
    assert eff.cross_axis_rules_fired, "the rule that raised placement must be named"
    assert eff.placement.residency == "onprem_only"


def test_envelope_expires_one_lifetime_after_the_type_was_last_detected():
    async def go():
        store = InMemoryTrajectoryStore(ttl_seconds=3600)
        m = lambda n: T0 + dt.timedelta(minutes=n)
        await _call(store, "app", "c", "header", f"ssn {SSN}", m(0))
        held, _ = await _call(store, "app", "c", "header", "clean", m(59))
        # an inherited type does not renew itself: 61 minutes after the SSN
        # was last DETECTED the envelope is gone, however many calls inherited it
        gone, _ = await _call(store, "app", "c", "header", "clean", m(61))
        # detecting it again starts a new lifetime
        await _call(store, "app", "c", "header", f"ssn {SSN}", m(70))
        await _call(store, "app", "c", "header", f"ssn {SSN}", m(110))
        again, _ = await _call(store, "app", "c", "header", "clean", m(165))
        late, _ = await _call(store, "app", "c", "header", "clean", m(175))
        return held, gone, again, late
    held, gone, again, late = _run(go())
    assert (held.onprem_only, gone.onprem_only) == (True, False)
    assert (again.onprem_only, late.onprem_only) == (True, False)


def test_sticky_inheritance_does_not_renew_or_copy_the_envelope():
    """A call that only INHERITS writes nothing, so one trajectory's
    restriction is not copied into another's row and kept alive there."""
    async def go():
        store = InMemoryTrajectoryStore(ttl_seconds=3600)
        await _call(store, "app", "a", "sticky", f"ssn {SSN}", T0)
        for n in range(1, 12):                       # steady clean traffic on another id
            await _call(store, "app", "b", "sticky", "clean", T0 + dt.timedelta(minutes=5 * n))
        return store, await _call(store, "app", "b", "sticky", "clean", T0 + dt.timedelta(minutes=61))
    store, (ctx, _) = _run(go())
    assert len(store._rows) == 1
    assert ctx.onprem_only is False


def test_concurrent_writers_of_one_trajectory_lose_nothing():
    """Append-only: two requests of one trajectory arriving together cannot
    overwrite each other (the lost update a read-modify-write would allow)."""
    async def go():
        store = InMemoryTrajectoryStore()
        await asyncio.gather(store.add("app", "k", frozenset({"US_SSN"}), T0),
                             store.add("app", "k", frozenset({"EMAIL_ADDRESS"}), T0),
                             store.add("app", "k", frozenset({"US_SSN"}), T0))
        return await store.read("app", "k", T0)
    assert _run(go()) == frozenset({"US_SSN", "EMAIL_ADDRESS"})


def test_db_store_is_append_only_and_prunes_expired_rows(tmp_path):
    from sqlalchemy import func, select
    from app.apps.db import Database
    from app.apps.models import TrajectoryEnvelope
    from app.policy.trajectory import DbTrajectoryStore, prune_envelopes

    async def go():
        db = Database(url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await db.ensure_ready()
        store = DbTrajectoryStore(db, ttl_seconds=3600)
        await asyncio.gather(store.add("app", "k", frozenset({"US_SSN"}), T0),
                             store.add("app", "k", frozenset({"EMAIL_ADDRESS"}), T0))
        await store.add("app", "k", frozenset({"US_SSN"}), T0)           # covered: no row
        await store.add("other", "k", frozenset({"PERSON"}), T0)
        both = await store.read("app", "k", T0)
        app_wide = await store.read("app", None, T0)
        expired = await store.read("app", "k", T0 + dt.timedelta(hours=2))
        async with db.session() as s:
            n_before = (await s.execute(select(func.count()).select_from(TrajectoryEnvelope))).scalar()
            pruned = await prune_envelopes(s, T0 + dt.timedelta(hours=2))
            await s.commit()
            n_after = (await s.execute(select(func.count()).select_from(TrajectoryEnvelope))).scalar()
        await db.dispose()
        return both, app_wide, expired, n_before, pruned, n_after
    both, app_wide, expired, n_before, pruned, n_after = _run(go())
    assert both == app_wide == frozenset({"US_SSN", "EMAIL_ADDRESS"})
    assert expired == frozenset()
    assert (n_before, pruned, n_after) == (3, 3, 0)


def test_recorded_key_names_the_envelope_actually_used():
    h = Trajectory(None, "app", "c1", "client")
    anon = Trajectory(None, "app", None, "none")
    assert h.recorded_key("off") is None and anon.recorded_key("off") is None
    assert h.recorded_key("header") == trajectory_key("app", "c1")
    assert anon.recorded_key("header") is None
    assert anon.recorded_key("sticky") == ANONYMOUS_KEY


def test_a_store_that_cannot_be_read_fails_the_call():
    class Broken:
        async def read(self, *a):
            raise RuntimeError("db down")
        async def add(self, *a):
            raise RuntimeError("db down")
    with pytest.raises(RuntimeError):
        _run(_call(Broken(), "app", "c", "header", "clean", T0))
    # `off` never touches the store, so an app without the feature is unaffected.
    ctx, _ = _run(_call(Broken(), "app", "c", "off", "clean", T0))
    assert ctx.onprem_only is False


def test_missing_store_fails_closed_unless_the_mode_is_off():
    with pytest.raises(AttributeError):
        _run(_call(None, "app", "c", "sticky", "clean", T0))
    assert _run(_call(None, "app", "c", "off", "clean", T0))[0].onprem_only is False


# --- properties over generated event sequences --------------------------------

EVENT = st.tuples(st.sampled_from(["a", "b"]),                 # application
                  st.sampled_from([None, "x", "y"]),           # trajectory id
                  st.booleans())                                # carries an SSN?
SEQ = st.lists(EVENT, min_size=1, max_size=12)


async def _play(mode, events):
    store = InMemoryTrajectoryStore()
    out = []
    for app_id, tid, has_ssn in events:
        ctx, eff = await _call(store, app_id, tid, mode,
                               f"ssn {SSN}" if has_ssn else "a clean message", T0)
        out.append((app_id, tid, has_ssn, ctx, eff))
    return out


@hsettings(max_examples=60, deadline=None)
@given(SEQ)
def test_off_mode_is_exactly_the_per_request_behaviour(events):
    for _, _, has_ssn, ctx, _ in _run(_play("off", events)):
        assert ctx.onprem_only is has_ssn
        assert ctx.inherited_entities == ()


@hsettings(max_examples=80, deadline=None)
@given(SEQ)
def test_header_mode_accumulates_per_trajectory_and_never_across(events):
    seen: set[tuple[str, str]] = set()
    for app_id, tid, has_ssn, ctx, _ in _run(_play("header", events)):
        restricted_before = tid is not None and (app_id, tid) in seen
        # monotone accumulation: restricted before => restricted now;
        # and nothing but this trajectory's own history can restrict it.
        assert ctx.onprem_only is (has_ssn or restricted_before)
        assert ("US_SSN" in ctx.inherited_entities) is restricted_before
        if has_ssn and tid is not None:
            seen.add((app_id, tid))


@hsettings(max_examples=80, deadline=None)
@given(SEQ)
def test_sticky_mode_restricts_the_whole_application_and_only_it(events):
    seen: set[str] = set()
    for app_id, _, has_ssn, ctx, _ in _run(_play("sticky", events)):
        assert ctx.onprem_only is (has_ssn or app_id in seen)
        if has_ssn:
            seen.add(app_id)


@hsettings(max_examples=60, deadline=None)
@given(SEQ, st.sampled_from(["header", "sticky"]))
def test_the_envelope_never_changes_a_protection_axis(events, mode):
    """NI-1 along a sequence: inheritance moves placement and the rule's own
    signals, and no protection value."""
    base = compile_policy(_policy(mode), SETTINGS).protection
    for *_, eff in _run(_play(mode, events)):
        assert eff.protection == base


@hsettings(max_examples=60, deadline=None)
@given(SEQ, st.sampled_from(["header", "sticky"]))
def test_an_envelope_only_ever_tightens_placement(events, mode):
    """Against the same events with no envelope, no call is less restricted."""
    with_env = _run(_play(mode, events))
    without = _run(_play("off", events))
    for (*_, a, _), (*_, b, _) in zip(with_env, without):
        assert a.onprem_only or not b.onprem_only
        assert set(b.detected_entities) <= set(a.detected_entities)


# --- end to end: POST /v1/chat/completions, the DB store, the outcome row -----

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}


def _app(client, app_id, policy):
    assert client.post("/admin/login", json=_ADMIN).status_code == 200
    created = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert created.status_code == 200, created.text
    put = client.put(f"/admin/applications/{app_id}/policy", json=policy)
    assert put.status_code == 200, put.text
    return created.json()["token"]


def _ask(client, token, text, trajectory=None):
    headers = {"Authorization": f"Bearer {token}"}
    if trajectory is not None:
        headers["x-routeforge-trajectory"] = trajectory
    r = client.post("/v1/chat/completions", headers=headers,
                    json={"messages": [{"role": "user", "content": text}]})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def backend(monkeypatch):
    from tests._litellm_fake import make_recording_acompletion, make_response
    from app.gateway.backends import litellm_backend
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="ok")))


def test_http_three_turn_conversation_with_and_without_the_envelope(client, backend):
    on = _app(client, "traj-on", {"pii_detection": "regex", "trajectory_mode": "header"})
    off = _app(client, "traj-off", {"pii_detection": "regex"})
    for token, expect_t3 in ((on, True), (off, False)):
        t1 = _ask(client, token, "what is a deductible?", "conv-1")
        t2 = _ask(client, token, f"tool result: ssn {SSN}", "conv-1")
        t3 = _ask(client, token, "summary: the claim is open. draft a note.", "conv-1")
        assert t1["policy"]["onprem_only"] is False
        assert t2["policy"]["onprem_only"] is True
        assert t3["policy"]["onprem_only"] is expect_t3
        assert ("US_SSN" in t3["policy"]["inherited_entity_types"]) is expect_t3
        assert SSN not in str(t3)
    # header mode: a different id, or none, is a different (or no) trajectory
    assert _ask(client, on, "another clean note", "conv-2")["policy"]["onprem_only"] is False
    assert _ask(client, on, "another clean note")["policy"]["onprem_only"] is False
    assert t3["policy"]["trajectory_mode"] == "off"


def test_http_sticky_mode_survives_a_dropped_or_changed_id(client, backend):
    token = _app(client, "traj-sticky", {"pii_detection": "regex", "trajectory_mode": "sticky"})
    assert _ask(client, token, "a clean question", "c1")["policy"]["onprem_only"] is False
    assert _ask(client, token, f"ssn {SSN}", "c1")["policy"]["onprem_only"] is True
    assert _ask(client, token, "a clean note", "c2")["policy"]["onprem_only"] is True
    assert _ask(client, token, "a clean note")["policy"]["onprem_only"] is True
    assert _ask(client, token, "a clean note", "not a valid id!")["policy"]["onprem_only"] is True


def test_http_envelopes_do_not_cross_applications(client, backend):
    a = _app(client, "traj-a", {"pii_detection": "regex", "trajectory_mode": "sticky"})
    b = _app(client, "traj-b", {"pii_detection": "regex", "trajectory_mode": "sticky"})
    _ask(client, a, f"ssn {SSN}", "shared-id")
    assert _ask(client, b, "a clean note", "shared-id")["policy"]["onprem_only"] is False


def test_admin_refuses_an_unknown_trajectory_mode(client):
    _app(client, "traj-bad", {"pii_detection": "regex"})
    r = client.put("/admin/applications/traj-bad/policy", json={"trajectory_mode": "always"})
    assert r.status_code == 422


def test_the_mode_changes_the_app_policy_version(client, backend):
    token = _app(client, "traj-ver", {"pii_detection": "regex"})
    v0 = _ask(client, token, "hello")["policy"]["app_policy_version"]
    client.put("/admin/applications/traj-ver/policy", json={"trajectory_mode": "header"})
    assert _ask(client, token, "hello")["policy"]["app_policy_version"] != v0


def test_outcome_row_records_a_hash_and_types_only(client, backend):
    token = _app(client, "traj-row", {"pii_detection": "regex", "trajectory_mode": "header"})
    _ask(client, token, f"ssn {SSN}", "conv-secret-name")
    _ask(client, token, "a clean note", "conv-secret-name")
    r = client.get("/admin/outcomes/export", params={"application_id": "traj-row"})
    assert r.status_code == 200, r.text
    import json
    rows = [json.loads(l)["outcome"] for l in r.text.splitlines() if l.strip()]
    assert len(rows) == 2
    last = max(rows, key=lambda o: o["ts"])
    assert last["inherited_entity_types"] == ["US_SSN"]
    assert "US_SSN" in last["detected_entity_types"]          # replay reads this
    assert last["trajectory_key"] == trajectory_key("traj-row", "conv-secret-name")
    assert "conv-secret-name" not in r.text and SSN not in r.text
