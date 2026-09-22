import asyncio

from app.config.loader import load_settings
from app.apps.db import Database
from app.apps import store
from app.apps.registry import ModelRegistry


def _run(c):
    return asyncio.run(c)


S = load_settings()


async def _reg_with(rows, settings=None):
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    async with db.session() as s:
        for slug, f in rows.items():
            await store.upsert_onboarded(s, slug, **f)
        await s.commit()
    reg = ModelRegistry(settings if settings is not None else S)
    await reg.refresh(db)
    return reg


def test_effective_pool_merges_enabled_onboarded():
    reg = _run(_reg_with({
        "nemotron-mid": dict(display_name="Nemotron", provider="onprem",
            tier="mid", price_input=0.2, price_output=0.2,
            endpoint="http://nem/v1", api_key_env="NEM", enabled=True),
        "off-model": dict(display_name="Off", provider="onprem", tier="cheap",
            price_input=0.1, price_output=0.1, endpoint="http://o/v1",
            enabled=False)}))
    names = {c.name for c in reg.effective_pool()}
    assert "nemotron-mid" in names          # enabled onboarded merged in
    assert "off-model" not in names         # disabled excluded
    assert reg.price_for("nemotron-mid").input == 0.2
    assert reg.endpoint_for("nemotron-mid") == ("http://nem/v1", "NEM")
    assert reg.has_healthy_onprem() is True


def test_app_scope():
    reg = _run(_reg_with({
        "scoped": dict(display_name="S", provider="onprem", tier="mid",
            price_input=0.2, price_output=0.2, endpoint="http://s/v1",
            enabled=True, allowed_app_ids=["acme"])}))
    assert reg.is_allowed_for_app("scoped", "acme") is True
    assert reg.is_allowed_for_app("scoped", "hospital") is False


def test_empty_onboarded_has_no_healthy_onprem():
    reg = _run(_reg_with({}))
    assert reg.has_healthy_onprem() is False


def test_effective_pool_omits_disabled_provider_candidates():
    from app.config.loader import load_settings
    from app.apps.registry import ModelRegistry
    s = load_settings()  # default: azure + bedrock disabled
    reg = ModelRegistry(s)
    # cold cache => YAML-only pool; disabled cloud providers must not appear
    names = {c.name for c in reg.effective_pool()}
    assert "azure-gpt-mid" not in names      # azure disabled in serving.yaml
    assert "bedrock-frontier" not in names   # bedrock disabled in serving.yaml


def test_effective_pool_includes_enabled_provider_candidates():
    from app.config.loader import load_settings
    from app.apps.registry import ModelRegistry
    s = load_settings()
    prov = dict(s.serving.providers)
    prov["bedrock"] = prov["bedrock"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    reg = ModelRegistry(s)
    assert "bedrock-frontier" in {c.name for c in reg.effective_pool()}


# --- P1b: endpoint data-handling declarations (region / prompt_retention /
# accepts_raw_sensitive), resolved once in effective_pool(). See design doc
# §2 and task-1-brief.md. ---

def _with_onprem_provider_defaults(**updates):
    prov = dict(S.serving.providers)
    prov["onprem"] = prov["onprem"].model_copy(update=updates)
    return S.model_copy(update={"serving": S.serving.model_copy(update={"providers": prov})})


def test_per_model_value_wins_over_provider_default():
    """An onboarded row that declares eu beats a provider default of us."""
    s = _with_onprem_provider_defaults(region="us")
    reg = _run(_reg_with({
        "m1": dict(display_name="M1", provider="onprem", tier="mid",
            price_input=0.1, price_output=0.1, endpoint="http://x/v1",
            enabled=True, region="eu")}, settings=s))
    cand = {c.name: c for c in reg.effective_pool()}["m1"]
    assert cand.region == "eu"
    assert cand.data_handling_source["region"] == "model"


def test_provider_default_applies_when_the_model_is_silent():
    s = _with_onprem_provider_defaults(
        region="us", prompt_retention="none", accepts_raw_sensitive="true")
    reg = _run(_reg_with({
        "m2": dict(display_name="M2", provider="onprem", tier="mid",
            price_input=0.1, price_output=0.1, endpoint="http://x/v1",
            enabled=True)}, settings=s))
    cand = {c.name: c for c in reg.effective_pool()}["m2"]
    assert cand.region == "us"
    assert cand.prompt_retention == "none"
    assert cand.accepts_raw_sensitive == "true"
    assert cand.data_handling_source == {
        "region": "provider_default", "prompt_retention": "provider_default",
        "accepts_raw_sensitive": "provider_default"}


def test_everything_unset_resolves_to_unknown_with_unknown_provenance():
    """This is every endpoint that exists today -- it must not become an error."""
    reg = _run(_reg_with({
        "m3": dict(display_name="M3", provider="onprem", tier="mid",
            price_input=0.1, price_output=0.1, endpoint="http://x/v1",
            enabled=True)}))
    cand = {c.name: c for c in reg.effective_pool()}["m3"]
    assert cand.region == "unknown"
    assert cand.prompt_retention == "unknown"
    assert cand.accepts_raw_sensitive == "unknown"
    assert cand.data_handling_source == {
        "region": "unknown", "prompt_retention": "unknown",
        "accepts_raw_sensitive": "unknown"}


def test_region_is_normalised_to_lowercase():
    """`US` and `us` are the same region; matching is exact after normalisation."""
    reg = _run(_reg_with({
        "m4": dict(display_name="M4", provider="onprem", tier="mid",
            price_input=0.1, price_output=0.1, endpoint="http://x/v1",
            enabled=True, region="US")}))
    cand = {c.name: c for c in reg.effective_pool()}["m4"]
    assert cand.region == "us"


def test_provenance_names_where_each_value_came_from():
    s = _with_onprem_provider_defaults(prompt_retention="retained")
    reg = _run(_reg_with({
        "m5": dict(display_name="M5", provider="onprem", tier="mid",
            price_input=0.1, price_output=0.1, endpoint="http://x/v1",
            enabled=True, region="eu", accepts_raw_sensitive=False)}, settings=s))
    cand = {c.name: c for c in reg.effective_pool()}["m5"]
    assert cand.data_handling_source == {
        "region": "model", "prompt_retention": "provider_default",
        "accepts_raw_sensitive": "model"}
    assert cand.accepts_raw_sensitive == "false"


# --- P1b Task 6: registry_version() (design §6) ------------------------------
# Same shape as AppPolicyView.version() / app/config/loader.py::_policy_version
# -- sha256[:12] over the decision-relevant fields, sorted-key JSON so it is
# stable across boots for equal input. What makes a candidate's identity here
# is name/provider/tier plus the three P1b declarations -- never
# quality/display_name/price, which don't change eligibility.

def test_registry_version_is_12_hex_chars():
    reg = _run(_reg_with({}))
    v = reg.registry_version()
    assert isinstance(v, str)
    assert len(v) == 12
    int(v, 16)  # hex


def test_registry_version_stable_across_boots_for_equal_input():
    rows = {
        "m1": dict(display_name="M1", provider="onprem", tier="mid",
            price_input=0.1, price_output=0.1, endpoint="http://x/v1",
            enabled=True, region="eu", prompt_retention="none",
            accepts_raw_sensitive=True)}
    reg_a = _run(_reg_with(rows))
    reg_b = _run(_reg_with(rows))
    assert reg_a.registry_version() == reg_b.registry_version()


def test_registry_version_changes_when_a_declaration_changes():
    base_rows = {
        "m1": dict(display_name="M1", provider="onprem", tier="mid",
            price_input=0.1, price_output=0.1, endpoint="http://x/v1",
            enabled=True, region="eu")}
    changed_rows = {
        "m1": dict(display_name="M1", provider="onprem", tier="mid",
            price_input=0.1, price_output=0.1, endpoint="http://x/v1",
            enabled=True, region="us")}   # only the declared region moved
    v_before = _run(_reg_with(base_rows)).registry_version()
    v_after = _run(_reg_with(changed_rows)).registry_version()
    assert v_before != v_after


def test_registry_version_unaffected_by_non_decision_fields():
    """Quality/display_name/price never gate eligibility -- the hash must not
    churn on a field this request-scoped decision never reads."""
    rows_a = {
        "m1": dict(display_name="M1", provider="onprem", tier="mid",
            price_input=0.1, price_output=0.1, endpoint="http://x/v1",
            enabled=True, region="eu", quality=0.5)}
    rows_b = {
        "m1": dict(display_name="Something Else", provider="onprem", tier="mid",
            price_input=0.99, price_output=0.99, endpoint="http://x/v1",
            enabled=True, region="eu", quality=0.9)}
    v_a = _run(_reg_with(rows_a)).registry_version()
    v_b = _run(_reg_with(rows_b)).registry_version()
    assert v_a == v_b


def test_registry_version_changes_when_a_model_is_added():
    v_empty = _run(_reg_with({})).registry_version()
    v_one = _run(_reg_with({
        "m1": dict(display_name="M1", provider="onprem", tier="mid",
            price_input=0.1, price_output=0.1, endpoint="http://x/v1",
            enabled=True)})).registry_version()
    assert v_empty != v_one


def test_effective_pool_carries_context_window():
    """B50: a known context window reaches the PoolCandidate untouched; an
    unknown one stays None, never a guess."""
    reg = _run(_reg_with({
        "known-ctx": dict(display_name="K", provider="onprem", tier="mid",
            price_input=0.1, price_output=0.1, endpoint="http://k/v1",
            enabled=True, context_window=8192),
        "unknown-ctx": dict(display_name="U", provider="onprem", tier="mid",
            price_input=0.1, price_output=0.1, endpoint="http://u/v1",
            enabled=True, context_window=None)}))
    by_name = {c.name: c for c in reg.effective_pool()}
    assert by_name["known-ctx"].context_window == 8192
    assert by_name["unknown-ctx"].context_window is None
