"""Admin API: probe-gated on-prem model onboarding (POST/PUT/DELETE/GET
/admin/infra/models). See Task 5 of the model-registry-onboarding plan.

Probe outcome is what gates `enabled` — a model that fails its reachability
probe is persisted but never routable (registry.has_healthy_onprem() keys on
`enabled`). Static validation (tier/endpoint/price/duplicate/unknown app id)
runs before the probe so a bad request never touches the network.
"""

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}


def _login(client):
    r = client.post("/admin/login", json=_ADMIN)
    assert r.status_code == 200
    return r


async def _ok(*a, **k):
    return True, "ok"


async def _fail(*a, **k):
    return False, "boom"


_BODY = {
    "slug": "nemotron-mid",
    "display_name": "Nemotron",
    "tier": "mid",
    "endpoint": "http://nem/v1",
    "price_input": 0.2,
    "price_output": 0.2,
}


def test_onboard_probe_pass_enables(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json=_BODY)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["probe_status"] == "healthy"
    assert body["probe_error"] is None

    lst = client.get("/admin/infra/models").json()
    assert any(m["slug"] == "nemotron-mid" and m["enabled"] for m in lst)


def test_onboard_probe_fail_disables(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _fail)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "unreachable-model"})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["probe_status"] == "unreachable"
    assert body["probe_error"] == "boom"

    lst = client.get("/admin/infra/models").json()
    row = next(m for m in lst if m["slug"] == "unreachable-model")
    assert row["enabled"] is False
    assert row["probe_status"] == "unreachable"


def test_onboard_bad_tier_422(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "bad-tier-model",
                                                  "tier": "strong"})
    assert r.status_code == 422


def test_onboard_bad_endpoint_422(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "bad-endpoint-model",
                                                  "endpoint": "ftp://nem/v1"})
    assert r.status_code == 422


def test_onboard_negative_price_422(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "bad-price-model",
                                                  "price_input": -1})
    assert r.status_code == 422


def test_onboard_unknown_app_id_422(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "scoped-model",
                                                  "allowed_app_ids": ["no-such-app"]})
    assert r.status_code == 422


def test_onboard_duplicate_409(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json=_BODY).status_code == 200
    assert client.post("/admin/infra/models", json=_BODY).status_code == 409


def test_onboard_duplicate_of_yaml_pool_409(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    # local-8b is seeded as an onboarded row by SP_SEED_TEST_MODELS (see
    # conftest.py), which is enough to collide via store.get_onboarded — no
    # need to reach into the static YAML pool for this duplicate check.
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "local-8b"})
    assert r.status_code == 409


def test_put_reprobes_and_updates(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json=_BODY).status_code == 200

    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _fail)
    r = client.put("/admin/infra/models/nemotron-mid",
                   json={**_BODY, "display_name": "Nemotron v2"})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["probe_status"] == "unreachable"

    lst = client.get("/admin/infra/models").json()
    row = next(m for m in lst if m["slug"] == "nemotron-mid")
    assert row["display_name"] == "Nemotron v2"
    assert row["enabled"] is False


def test_put_can_disable_healthy_model(client, monkeypatch):
    # Regression: enabled must be admin intent ANDed with probe result, not the
    # probe result alone — otherwise a healthy model can never be turned off.
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json=_BODY).status_code == 200

    r = client.put("/admin/infra/models/nemotron-mid", json={**_BODY, "enabled": False})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["probe_status"] == "healthy"

    lst = client.get("/admin/infra/models").json()
    row = next(m for m in lst if m["slug"] == "nemotron-mid")
    assert row["enabled"] is False
    assert row["probe_status"] == "healthy"

def test_put_cannot_enable_unreachable_model(client, monkeypatch):
    # Probe-gating preserved: intent=True can never overcome a failing probe.
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _fail)
    _login(client)
    assert client.post("/admin/infra/models", json={**_BODY, "slug": "flaky-model"}).status_code == 200

    r = client.put("/admin/infra/models/flaky-model", json={**_BODY, "slug": "flaky-model", "enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    lst = client.get("/admin/infra/models").json()
    row = next(m for m in lst if m["slug"] == "flaky-model")
    assert row["enabled"] is False

def test_onboard_disabled_intent(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "intent-off-model", "enabled": False})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["probe_status"] == "healthy"

def test_put_missing_slug_404(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.put("/admin/infra/models/does-not-exist", json=_BODY)
    assert r.status_code == 404


def test_delete_onboarded(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json=_BODY).status_code == 200

    r = client.delete("/admin/infra/models/nemotron-mid")
    assert r.status_code == 200

    lst = client.get("/admin/infra/models").json()
    assert not any(m["slug"] == "nemotron-mid" for m in lst)


def test_delete_missing_slug_404(client):
    _login(client)
    r = client.delete("/admin/infra/models/does-not-exist")
    assert r.status_code == 404


def test_onboard_requires_admin_auth(client):
    r = client.post("/admin/infra/models", json=_BODY)
    assert r.status_code == 401
    assert client.get("/admin/infra/models").status_code == 401


# --- B19: slashed slugs (org/model) are edit/delete-able end-to-end ----------

_SLASH = "Qwen/Qwen2.5-1.5B-Instruct"  # a real OpenAI-compatible id with a '/'


def test_slashed_slug_put_and_delete_roundtrip(client, monkeypatch):
    # {slug:path} routing: an org/model slug must be editable and deletable, not
    # 404 on the slash. Regression for B19 (bare {slug} matched [^/]+).
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models",
                       json={**_BODY, "slug": _SLASH}).status_code == 200

    # PUT reaches the slashed row (was 404).
    r = client.put(f"/admin/infra/models/{_SLASH}",
                   json={**_BODY, "slug": _SLASH, "display_name": "Qwen edited"})
    assert r.status_code == 200
    row = next(m for m in client.get("/admin/infra/models").json()
               if m["slug"] == _SLASH)
    assert row["display_name"] == "Qwen edited"

    # DELETE reaches the slashed row (was 404) and removes it.
    assert client.delete(f"/admin/infra/models/{_SLASH}").status_code == 200
    assert not any(m["slug"] == _SLASH
                   for m in client.get("/admin/infra/models").json())


def test_repost_of_disabled_slug_self_heals(client, monkeypatch):
    # B19 stuck-row trap: a first onboard with a bad endpoint probes-unreachable
    # and is saved DISABLED. A clean re-POST (fixed endpoint) must re-probe and
    # upsert rather than hard-409, so the row self-heals without DB surgery.
    _login(client)

    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _fail)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": _SLASH})
    assert r.status_code == 200 and r.json()["enabled"] is False

    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": _SLASH})
    assert r.status_code == 200, "re-POST of a disabled row should upsert, not 409"
    assert r.json()["enabled"] is True

    # An already-ENABLED row is not re-onboardable via POST — edit it with PUT.
    assert client.post("/admin/infra/models",
                       json={**_BODY, "slug": _SLASH}).status_code == 409


def test_onboard_yaml_pool_name_always_409(client, monkeypatch):
    # A static YAML model_pool name (config/routing.yaml) is never re-onboardable,
    # even before any onboarded row exists — the self-heal path is scoped to
    # disabled ONBOARDED rows only and must not swallow this branch (B19).
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "azure-gpt-mid"})
    assert r.status_code == 409


# --- P1b: endpoint data-handling declarations (design §2/§8) ----------------
# The admin-HTTP wiring this section covers: OnboardIn accepting the three
# fields and passing them to store.upsert_onboarded (already covered at the
# store layer by tests/test_onboarded_store.py), and _onboarded_dict
# returning them raw (not resolved through a provider default -- that
# resolution is ModelRegistry.effective_pool()'s job, exercised by
# tests/test_model_registry.py).

def test_onboard_declares_data_handling_and_reads_back(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={
        **_BODY, "slug": "declared-model",
        "region": "US", "prompt_retention": "none", "accepts_raw_sensitive": True})
    assert r.status_code == 200

    row = next(m for m in client.get("/admin/infra/models").json()
               if m["slug"] == "declared-model")
    # Region is normalized (stripped + lowercased) the same way
    # ModelRegistry._resolve_data_handling resolves it for routing, so the
    # console shows the token the router will actually match.
    assert row["region"] == "us"
    assert row["prompt_retention"] == "none"
    assert row["accepts_raw_sensitive"] is True


def test_onboard_edit_one_declaration_the_others_survive(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json={
        **_BODY, "slug": "declared-model",
        "region": "eu", "prompt_retention": "retained",
        "accepts_raw_sensitive": False}).status_code == 200

    # PUT is a full-body replace (same semantics as every other optional
    # field on this route) -- restate the two unchanged declarations
    # alongside the one that's changing, and all three land as intended.
    r = client.put("/admin/infra/models/declared-model", json={
        **_BODY, "slug": "declared-model",
        "region": "eu", "prompt_retention": "none",  # <- the one change
        "accepts_raw_sensitive": False})
    assert r.status_code == 200

    row = next(m for m in client.get("/admin/infra/models").json()
               if m["slug"] == "declared-model")
    assert row["region"] == "eu"
    assert row["prompt_retention"] == "none"
    assert row["accepts_raw_sensitive"] is False


def test_onboard_bad_prompt_retention_422(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={
        **_BODY, "slug": "bad-retention-model", "prompt_retention": "sometimes"})
    assert r.status_code == 422


def test_onboard_with_no_declarations_reads_back_undeclared(client, monkeypatch):
    # An onboard that names none of the three P1b fields must behave exactly
    # as it did before P1b (design §12): all three come back None/undeclared,
    # never a fabricated "unknown" string that would make an omitted
    # declaration indistinguishable from an explicit one.
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json=_BODY).status_code == 200

    row = next(m for m in client.get("/admin/infra/models").json()
               if m["slug"] == "nemotron-mid")
    assert row["region"] is None
    assert row["prompt_retention"] is None
    assert row["accepts_raw_sensitive"] is None


# --- accepts_raw_sensitive: omission must not clear it (task-8 review) -----
# Every other optional field on this route -- including region and
# prompt_retention above -- is full-replace: an omitted field clears the
# stored value, because the console's edit form always resubmits the whole
# row. accepts_raw_sensitive is the deliberate exception: clearing a
# declared `false` to unknown SILENTLY REOPENS the endpoint to raw sensitive
# traffic (app/policy/feasible.py's raw-sensitive rule fires only on a
# declared `false`, never on "unknown"), so an omitted field must preserve
# what's stored, while an EXPLICIT null still clears it (real operator
# intent, not an accident of an older body shape). See
# app/api/admin_routes.py::_resolved_accepts_raw_sensitive.

def test_edit_omitting_accepts_raw_sensitive_keeps_stored_value(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json={
        **_BODY, "slug": "closed-model", "accepts_raw_sensitive": False}).status_code == 200

    # This PUT body has no "accepts_raw_sensitive" key at all (just _BODY's
    # fields plus a changed price) -- an omission, not an explicit null.
    r = client.put("/admin/infra/models/closed-model",
                   json={**_BODY, "slug": "closed-model", "price_input": 0.5})
    assert r.status_code == 200

    row = next(m for m in client.get("/admin/infra/models").json()
               if m["slug"] == "closed-model")
    assert row["accepts_raw_sensitive"] is False  # preserved, not cleared
    assert row["price_input"] == 0.5              # the actual edit still applied


def test_edit_explicit_null_accepts_raw_sensitive_clears_it(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json={
        **_BODY, "slug": "closed-model", "accepts_raw_sensitive": False}).status_code == 200

    r = client.put("/admin/infra/models/closed-model",
                   json={**_BODY, "slug": "closed-model", "accepts_raw_sensitive": None})
    assert r.status_code == 200

    row = next(m for m in client.get("/admin/infra/models").json()
               if m["slug"] == "closed-model")
    assert row["accepts_raw_sensitive"] is None    # explicit clear honoured


def test_edit_omitting_region_and_retention_still_clears_them(client, monkeypatch):
    # The contrast case: region/prompt_retention are NOT given the same
    # exception. Omitting them on a PUT still clears them to undeclared,
    # because that direction fails CLOSED (an app requiring either excludes
    # an "unknown" endpoint) -- pinned here so the asymmetry above stays a
    # deliberate, single-field exception and is never "fixed" into
    # consistency with accepts_raw_sensitive.
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json={
        **_BODY, "slug": "declared-model", "region": "eu",
        "prompt_retention": "retained"}).status_code == 200

    r = client.put("/admin/infra/models/declared-model",
                   json={**_BODY, "slug": "declared-model"})
    assert r.status_code == 200

    row = next(m for m in client.get("/admin/infra/models").json()
               if m["slug"] == "declared-model")
    assert row["region"] is None
    assert row["prompt_retention"] is None
