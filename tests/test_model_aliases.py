"""Per-application pinned model aliases, through the real HTTP boundary.

An alias is sent as `model`, swapped for its slug before the gate sees it, and
from there is an ordinary slug pin. The recording backend proves which slug
reached the model; the outcome row and export prove what was recorded.
"""
from __future__ import annotations

import json

import pytest

from app.apps.policy import AppPolicyView
from tests._litellm_fake import CALLS
from tests.onboarding.harness import (
    add_model, install_dual_fake, onboard,
)
from tests.test_admin_api import _login
from tests.test_outcome_hooks import (
    HELLO, _client, _only, _outcomes, _with_azure_enabled,
)
from tests.conftest import v1_error

PLANNER = "role.planner"
WORKER = "role.patch-worker"


@pytest.fixture(autouse=True)
def _calls():
    CALLS.clear()
    yield
    CALLS.clear()


def _put_policy(c, app_id: str, body: dict):
    _login(c)
    return c.put(f"/admin/applications/{app_id}/policy", json=body)


def _with_aliases(c, app_id: str = "alias-app", aliases=None):
    add_model(c, "slug-a")
    add_model(c, "slug-b")
    app = onboard(c, app_id)
    r = _put_policy(c, app_id, {"model_aliases": aliases or {PLANNER: "slug-a"}})
    assert r.status_code == 200, r.text
    return app


def test_alias_resolves_and_the_backend_receives_the_slug(monkeypatch):
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = _with_aliases(c)
        r = app.chat({**HELLO, "model": PLANNER})
        assert r.status_code == 200, r.text
        assert CALLS[-1]["model"].endswith("slug-a")
        assert PLANNER not in CALLS[-1]["model"]


def test_row_and_export_record_alias_and_resolved_slug(monkeypatch):
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = _with_aliases(c)
        assert app.chat({**HELLO, "model": PLANNER}).status_code == 200
        row = _only(c)
        assert row.requested_alias == PLANNER
        assert row.requested_model == "slug-a"
        assert row.chosen == "slug-a"
        _login(c)
        out = c.get("/admin/outcomes/export")
        lines = [json.loads(x) for x in out.text.strip().splitlines()]
        assert lines[0]["outcome"]["requested_alias"] == PLANNER
        assert lines[0]["outcome"]["requested_model"] == "slug-a"


def test_slug_pin_and_auto_record_no_alias(monkeypatch):
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = _with_aliases(c)
        assert app.chat({**HELLO, "model": "slug-b"}).status_code == 200
        assert app.chat({**HELLO, "model": "auto"}).status_code == 200
        assert [r.requested_alias for r in _outcomes(c)] == [None, None]
        assert CALLS[0]["model"].endswith("slug-b")


def test_repointing_an_alias_changes_the_slug_and_the_policy_version(monkeypatch):
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = _with_aliases(c)
        r1 = app.chat({**HELLO, "model": PLANNER})
        assert r1.status_code == 200
        v1 = r1.json()["policy"]["app_policy_version"]
        assert _put_policy(c, "alias-app", {"model_aliases": {PLANNER: "slug-b"}}
                           ).status_code == 200
        r2 = app.chat({**HELLO, "model": PLANNER})
        assert r2.status_code == 200
        assert CALLS[-1]["model"].endswith("slug-b")
        assert r2.json()["policy"]["app_policy_version"] != v1
        rows = _outcomes(c)
        assert len({r.app_policy_version for r in rows}) == 2


def test_unknown_name_with_aliases_is_400_and_never_reaches_the_backend(monkeypatch):
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = _with_aliases(c)
        r = app.chat({**HELLO, "model": "role.typo"})
        assert r.status_code == 400, r.text
        assert v1_error(r)["code"] == "unknown_model_alias"
        assert CALLS == []


def test_known_slug_is_still_a_plain_pin_for_an_app_with_aliases(monkeypatch):
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = _with_aliases(c)
        assert app.chat({**HELLO, "model": "slug-b"}).status_code == 200
        assert CALLS[-1]["model"].endswith("slug-b")


def test_app_without_aliases_unknown_name_behaves_as_before(monkeypatch):
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = onboard(c, "plain-app")
        r = app.chat({**HELLO, "model": "role.typo"})
        assert r.status_code == 422, r.text
        assert v1_error(r)["code"] == "model_not_found"
        assert CALLS == []


def test_alias_to_an_ineligible_slug_fails_like_pinning_it_directly(monkeypatch):
    _with_azure_enabled(monkeypatch)
    install_dual_fake(monkeypatch)
    with _client() as c:
        add_model(c, "slug-a")
        app = onboard(c, "alias-onprem", residency="onprem_only")
        assert _put_policy(c, "alias-onprem",
                           {"model_aliases": {PLANNER: "azure-gpt-mid"}}
                           ).status_code == 200
        direct = app.chat({**HELLO, "model": "azure-gpt-mid"})
        via = app.chat({**HELLO, "model": PLANNER})
        assert direct.status_code == via.status_code
        assert direct.status_code >= 400
        assert v1_error(direct)["code"] == v1_error(via)["code"]
        assert CALLS == []


def test_aliases_do_not_change_the_version_of_apps_without_them():
    assert AppPolicyView(residency="any").version() == \
        AppPolicyView(residency="any", model_aliases=None).version()
    with_a = AppPolicyView(residency="any", model_aliases=((PLANNER, "slug-a"),))
    assert with_a.version() != AppPolicyView(residency="any").version()
    assert with_a.alias_target(PLANNER) == "slug-a"
    assert with_a.alias_target("other") is None


@pytest.mark.parametrize("aliases,needle", [
    ({"Bad Name": "slug-a"}, "must match"),
    ({"auto": "slug-a"}, "reserved"),
    ({"slug-b": "slug-a"}, "shadow"),
    ({PLANNER: "no-such-slug"}, "not in the YAML pool"),
    ({}, "empty map"),
])
def test_admin_rejects_bad_alias_maps(aliases, needle):
    with _client() as c:
        add_model(c, "slug-a")
        add_model(c, "slug-b")
        onboard(c, "alias-admin")
        r = _put_policy(c, "alias-admin", {"model_aliases": aliases})
        assert r.status_code == 422, r.text
        assert needle in r.text


def test_admin_rejects_non_string_target():
    with _client() as c:
        onboard(c, "alias-admin")
        assert _put_policy(c, "alias-admin",
                           {"model_aliases": {PLANNER: 7}}).status_code == 422


def test_put_replaces_the_whole_map_and_reset_clears_it():
    with _client() as c:
        _with_aliases(c, "alias-admin", {PLANNER: "slug-a", WORKER: "slug-b"})
        r = _put_policy(c, "alias-admin", {"model_aliases": {WORKER: "slug-a"}})
        assert r.json()["policy"]["model_aliases"] == {WORKER: "slug-a"}
        r = _put_policy(c, "alias-admin", {"reset": ["model_aliases"]})
        assert r.status_code == 200, r.text
        assert r.json()["policy"]["model_aliases"] is None
