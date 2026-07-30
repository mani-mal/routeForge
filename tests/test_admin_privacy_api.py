"""Admin API: GET/PUT /admin/applications/{id}/privacy (Task 10).

Uses the same logged-in-client pattern as tests/test_admin_api.py."""

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}


def _login(client):
    r = client.post("/admin/login", json=_ADMIN)
    assert r.status_code == 200
    return r


def _make_app(client, app_id: str) -> None:
    r = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code == 200


_VALID_PROFILE = {
    "recognizers": [
        {"name": "MRN", "regex": r"\bMRN-\d{6}\b", "score": 0.9, "enabled": True},
    ],
    "rules": [
        {"entities": ["MRN"], "sensitivity": "restricted", "risk_class": "high",
         "residency": "onprem_only", "require_verification": True},
    ],
}


def test_put_valid_profile_then_get_returns_it(client):
    _login(client)
    _make_app(client, "privacyapp")

    put = client.put("/admin/applications/privacyapp/privacy", json=_VALID_PROFILE)
    assert put.status_code == 200

    got = client.get("/admin/applications/privacyapp/privacy")
    assert got.status_code == 200
    body = got.json()
    assert len(body["recognizers"]) == 1
    rec = body["recognizers"][0]
    assert rec["name"] == "MRN"
    assert rec["regex"] == r"\bMRN-\d{6}\b"
    assert len(body["rules"]) == 1
    rule = body["rules"][0]
    assert rule["entities"] == ["MRN"]
    assert rule["sensitivity"] == "restricted"
    assert rule["risk_class"] == "high"
    assert rule["residency"] == "onprem_only"
    assert rule["require_verification"] is True


def test_get_unknown_app_404(client):
    _login(client)
    assert client.get("/admin/applications/nope/privacy").status_code == 404


def test_put_unknown_app_404(client):
    _login(client)
    r = client.put("/admin/applications/nope/privacy", json=_VALID_PROFILE)
    assert r.status_code == 404


def test_put_lowercase_recognizer_name_422(client):
    _login(client)
    _make_app(client, "badname")
    body = {"recognizers": [{"name": "mrn", "regex": r"\d+"}], "rules": []}
    r = client.put("/admin/applications/badname/privacy", json=body)
    assert r.status_code == 422


def test_put_recognizer_colliding_with_builtin_422(client):
    _login(client)
    _make_app(client, "builtincollide")
    body = {"recognizers": [{"name": "US_SSN", "regex": r"\d+"}], "rules": []}
    r = client.put("/admin/applications/builtincollide/privacy", json=body)
    assert r.status_code == 422


def test_put_duplicate_recognizer_name_422(client):
    _login(client)
    _make_app(client, "duprecog")
    body = {"recognizers": [
        {"name": "MRN", "regex": r"\d+"},
        {"name": "MRN", "regex": r"\w+"},
    ], "rules": []}
    r = client.put("/admin/applications/duprecog/privacy", json=body)
    assert r.status_code == 422


def test_put_bad_sensitivity_422(client):
    _login(client)
    _make_app(client, "badsens")
    body = {"recognizers": [], "rules": [
        {"entities": [], "sensitivity": "secret", "risk_class": "high",
         "residency": "any"},
    ]}
    r = client.put("/admin/applications/badsens/privacy", json=body)
    assert r.status_code == 422


def test_put_bad_risk_class_422(client):
    _login(client)
    _make_app(client, "badrisk")
    body = {"recognizers": [], "rules": [
        {"entities": [], "sensitivity": "public", "risk_class": "extreme",
         "residency": "any"},
    ]}
    r = client.put("/admin/applications/badrisk/privacy", json=body)
    assert r.status_code == 422


def test_put_bad_residency_422(client):
    _login(client)
    _make_app(client, "badresidency")
    body = {"recognizers": [], "rules": [
        {"entities": [], "sensitivity": "public", "risk_class": "low",
         "residency": "mars_only"},
    ]}
    r = client.put("/admin/applications/badresidency/privacy", json=body)
    assert r.status_code == 422


def test_put_unknown_entity_reference_422(client):
    _login(client)
    _make_app(client, "unknownentity")
    body = {"recognizers": [], "rules": [
        {"entities": ["NOPE"], "sensitivity": "public", "risk_class": "low",
         "residency": "any"},
    ]}
    r = client.put("/admin/applications/unknownentity/privacy", json=body)
    assert r.status_code == 422


def test_put_catastrophic_regex_422(client):
    _login(client)
    _make_app(client, "redosapp")
    body = {"recognizers": [{"name": "EVIL", "regex": "(a+)+$"}], "rules": []}
    r = client.put("/admin/applications/redosapp/privacy", json=body)
    assert r.status_code == 422


def test_put_never_persists_on_validation_failure(client):
    """A bad PUT must not leave a partial profile behind."""
    _login(client)
    _make_app(client, "atomicapp")

    good = client.put("/admin/applications/atomicapp/privacy", json=_VALID_PROFILE)
    assert good.status_code == 200

    bad = {"recognizers": [{"name": "bad", "regex": r"\d+"}], "rules": []}
    r = client.put("/admin/applications/atomicapp/privacy", json=bad)
    assert r.status_code == 422

    got = client.get("/admin/applications/atomicapp/privacy").json()
    assert len(got["recognizers"]) == 1
    assert got["recognizers"][0]["name"] == "MRN"


def test_put_rejects_too_many_recognizers(client):
    """Spec §5: caps on number of recognizers/rules per app. >32 recognizers -> 422."""
    _login(client)
    _make_app(client, "toomanyrecog")
    body = {
        "recognizers": [
            {"name": f"REC{i}", "regex": r"\d+"} for i in range(33)
        ],
        "rules": [],
    }
    r = client.put("/admin/applications/toomanyrecog/privacy", json=body)
    assert r.status_code == 422


def test_put_rejects_too_many_rules(client):
    """Spec §5: caps on number of recognizers/rules per app. >64 rules -> 422."""
    _login(client)
    _make_app(client, "toomanyrules")
    body = {
        "recognizers": [],
        "rules": [
            {"entities": [], "sensitivity": "public", "risk_class": "low",
             "residency": "any"} for _ in range(65)
        ],
    }
    r = client.put("/admin/applications/toomanyrules/privacy", json=body)
    assert r.status_code == 422


def test_put_rejects_out_of_range_score(client):
    _login(client)
    _make_app(client, "badscorehigh")
    _make_app(client, "badscorelow")
    body_high = {"recognizers": [{"name": "MRN", "regex": r"\d+", "score": 1.5}], "rules": []}
    r = client.put("/admin/applications/badscorehigh/privacy", json=body_high)
    assert r.status_code == 422

    body_low = {"recognizers": [{"name": "MRN", "regex": r"\d+", "score": -0.1}], "rules": []}
    r = client.put("/admin/applications/badscorelow/privacy", json=body_low)
    assert r.status_code == 422


def test_put_accepts_boundary_caps_and_scores(client):
    """32 recognizers / 64 rules / score 1.0 and 0.0 are all still valid (inclusive bounds)."""
    _login(client)
    _make_app(client, "boundaryapp")
    recognizers = [
        {"name": f"REC{i}", "regex": r"\d+",
         "score": 1.0 if i % 2 == 0 else 0.0}
        for i in range(32)
    ]
    rules = [
        {"entities": [], "sensitivity": "public", "risk_class": "low", "residency": "any"}
        for _ in range(64)
    ]
    body = {"recognizers": recognizers, "rules": rules}
    r = client.put("/admin/applications/boundaryapp/privacy", json=body)
    assert r.status_code == 200


def test_put_clears_profile_cache(client):
    _login(client)
    _make_app(client, "cacheapp")

    # Prime the cache the way the serving path would.
    from app.apps.privacy import AppPrivacyProfile
    cache = client.app.state.profile_cache
    cache.put("cacheapp", AppPrivacyProfile(recognizers=(), rules=()))
    assert cache.get("cacheapp") is not None

    r = client.put("/admin/applications/cacheapp/privacy", json=_VALID_PROFILE)
    assert r.status_code == 200

    assert cache.get("cacheapp") is None
