"""Onboarding conformance matrix: real client shape x app policy recipe x model
setup. Every cell must be served (200), or rejected with a 4xx whose detail
names the thing to change. Never 500/502/503, never an unexplained reject.

Known bugs are xfail(strict=True) via known_bug(); the task that fixes one
deletes its line, so a fix that silently regresses flips the cell red."""
from __future__ import annotations

import pytest

from tests.conftest import v1_error
from tests.onboarding.clients import CLIENTS, STREAM_CLIENTS
from tests.onboarding.harness import (
    add_model, conformance_client, disable_seeded_models, install_dual_fake, onboard,
)

MODEL = "conf-model"

# Per-app policy recipes an operator actually configures (console presets and
# the three live apps). Written straight to the store; save-time clamps are
# tested separately in tests/test_policy_save_validation.py.
RECIPES = {
    "default": {},
    "kit_gtm_router": dict(residency="any", policy_mode="passthrough",
                           capture_io="full", stream_mode="stream_unverified"),
    "onprem_enforced": dict(residency="onprem_only", stream_mode="verified_then_stream"),
    "millwise_like": dict(residency="onprem_only", policy_mode="passthrough",
                          capture_io="off", stream_mode="verified_then_stream"),
    "scoped_model": dict(allowed_models=[MODEL], stream_mode="incremental_verify"),
}

# Model pool setups: (price_input, price_output) per 1M for the only on-prem model.
SETUPS = {
    "free_onprem": (0.0, 0.0),
    "priced_10": (10.0, 10.0),    # the $10/1M incident (2026-09-15)
}

# A rejection is actionable when its detail names at least one of these.
HINT_WORDS = (
    "stream_mode", "max_cost_usd_per_request", "allowed_models", "allowed_tiers",
    "allowed_providers", "allowed_app_ids", "residency", "onboard", "disabled",
    "max_rpm", "max_spend_usd_per_day",
)

# Fixed framing the product puts around every rejection. Stripped before the
# HINT_WORDS check: "no candidates after residency filter" contains "residency",
# so matching the raw detail would pass every residency 422 without a fix hint.
FIXED_PREFIXES = (
    "no model is available for this request's policy: ",
    "no candidates after residency filter",
    "no candidates after budget filter",
    "model request rejected: ",
    "requested model",
    "no backend could serve this request",
    "streaming is not supported for this application",
)


def _hint_text(detail: str) -> str:
    for prefix in FIXED_PREFIXES:
        detail = detail.replace(prefix, "")
    return detail


def expected_status(client: str, recipe: str, setup: str) -> int | None:
    """The pinned outcome for a cell, or None when only 'served or told why'
    applies. A free on-prem model must serve every client, except a streaming
    client on an app that never opted into streaming."""
    if setup != "free_onprem":
        return None
    if recipe == "default" and client in STREAM_CLIENTS:
        return 400
    return 200


def known_bug(client: str, recipe: str, setup: str) -> str | None:
    return None


@pytest.fixture
def dual_fake(_fake_acompletion, monkeypatch):
    # Depends on the autouse fake so this one is installed after it and wins.
    install_dual_fake(monkeypatch)


@pytest.mark.parametrize("setup", sorted(SETUPS))
@pytest.mark.parametrize("recipe", sorted(RECIPES))
@pytest.mark.parametrize("client_name", sorted(CLIENTS))
def test_real_client_is_served_or_told_why(client_name, recipe, setup, dual_fake, request):
    reason = known_bug(client_name, recipe, setup)
    if reason:
        request.applymarker(pytest.mark.xfail(strict=True, reason=reason))
    price_in, price_out = SETUPS[setup]
    with conformance_client() as c:
        disable_seeded_models(c)
        add_model(c, MODEL, tier="cheap", price_input=price_in, price_output=price_out)
        app = onboard(c, f"conf-{recipe}-{client_name}-{setup}".replace("_", "-"),
                      **RECIPES[recipe])
        r = app.chat(CLIENTS[client_name]())

    assert r.status_code not in (500, 502, 503), r.text
    expected = expected_status(client_name, recipe, setup)
    if expected is not None:
        assert r.status_code == expected, r.text
        if expected == 400:
            assert "stream_mode" in v1_error(r).get("message", ""), r.text
    if r.status_code == 200:
        return
    assert r.status_code in (400, 422, 429), r.text
    detail = v1_error(r).get("message", "")
    assert any(w in _hint_text(detail) for w in HINT_WORDS), \
        f"unactionable rejection: {detail!r}"


@pytest.mark.parametrize("recipe", sorted(RECIPES))
def test_every_listed_model_is_servable(recipe, dual_fake):
    """GET /v1/models must never list an id that a plain request then rejects."""
    with conformance_client() as c:
        add_model(c, MODEL, tier="cheap")
        app = onboard(c, f"models-{recipe}".replace("_", "-"), **RECIPES[recipe])
        listed = c.get("/v1/models", headers=app.headers).json()["data"]
        assert listed, "recipe lists no models"
        for m in listed:
            r = app.chat(CLIENTS["langchain_parts_chat"](model=m["id"]))
            assert r.status_code == 200, (m["id"], r.text)
