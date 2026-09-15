"""Onboarding conformance matrix: real client shape x app policy recipe x model
setup. Every cell must be served (200), or rejected with a 4xx whose detail
names the thing to change. Never 500/502/503, never an unexplained reject.

Known bugs are xfail(strict=True) via known_bug(); the task that fixes one
deletes its line, so a fix that silently regresses flips the cell red."""
from __future__ import annotations

import pytest

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


def known_bug(client: str, recipe: str, setup: str) -> str | None:
    if recipe == "default" and client in STREAM_CLIENTS:
        return "stream 400 does not name stream_mode (Task 7)"
    if setup == "priced_10" and client == "qwen_code_turn":
        return "budget 422 does not name max_cost_usd_per_request (Task 7)"
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
    if r.status_code == 200:
        return
    assert r.status_code in (400, 422, 429), r.text
    detail = r.json().get("detail", "")
    assert any(w in detail for w in HINT_WORDS), f"unactionable rejection: {detail!r}"


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
