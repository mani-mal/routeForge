"""Feature 3 (DSPy prompt-optimization) Task 1: managed-prompt substrate.

Covers `app/prompts/store.py` in isolation (load/resolve/version), the
default-no-op boot invariant, and the fail-closed config-lint guards
(`PROMPT_ACTIVE_MISSING`, `PROMPT_ARTIFACT_INVALID`, the unverified-pin
`ConfigLintError`). See tests/test_config_lint.py for the sibling lint style
this mirrors, and tests/test_obs_export.py for the sp.prompt_version
telemetry-stamp test.
"""
import copy

import pytest
import yaml

from app.config.lint import ConfigLintError, enforce, lint_settings
from app.config.loader import load_settings
from app.config.schema import PromptOptCfg
from app.prompts.store import PromptArtifact, load_artifact, prompt_version, resolve_active


def _write(tmp_path, prompt_id, label, *, verified=True, text="say direct or multi_step",
           omit=()):
    d = tmp_path / prompt_id
    d.mkdir(parents=True, exist_ok=True)
    doc = {"id": prompt_id, "label": label, "verified": verified, "text": text,
           "source": "hand-authored", "notes": ""}
    for k in omit:
        doc.pop(k, None)
    (d / f"{label}.yaml").write_text(yaml.safe_dump(doc))


# ---------------------------------------------------------------------------
# load_artifact
# ---------------------------------------------------------------------------

def test_load_artifact_returns_none_for_missing_file(tmp_path):
    assert load_artifact(str(tmp_path), "task_mode_classifier", "v1") is None


def test_load_artifact_happy_path(tmp_path):
    _write(tmp_path, "task_mode_classifier", "v1", verified=True, text="hello")
    a = load_artifact(str(tmp_path), "task_mode_classifier", "v1")
    assert isinstance(a, PromptArtifact)
    assert a.id == "task_mode_classifier"
    assert a.label == "v1"
    assert a.verified is True
    assert a.text == "hello"
    assert a.source == "hand-authored"


def test_load_artifact_forces_id_and_label_to_requested_values(tmp_path):
    # The on-disk id must not smuggle in a different prompt_id than the
    # directory it was resolved from; label comes from the filename.
    d = tmp_path / "task_mode_classifier"
    d.mkdir()
    (d / "v1.yaml").write_text(yaml.safe_dump(
        {"id": "someone_else", "label": "other", "verified": True, "text": "x"}))
    a = load_artifact(str(tmp_path), "task_mode_classifier", "v1")
    assert a.id == "task_mode_classifier"
    assert a.label == "v1"


@pytest.mark.parametrize("omit", [("id",), ("text",), ("id", "text")])
def test_load_artifact_raises_on_missing_required_field(tmp_path, omit):
    _write(tmp_path, "p", "v1", omit=omit)
    with pytest.raises(ValueError):
        load_artifact(str(tmp_path), "p", "v1")


def test_load_artifact_raises_on_unparseable_yaml(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "v1.yaml").write_text("id: [unclosed\n  bracket: true")
    with pytest.raises(ValueError):
        load_artifact(str(tmp_path), "p", "v1")


def test_load_artifact_raises_when_not_a_mapping(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "v1.yaml").write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError):
        load_artifact(str(tmp_path), "p", "v1")


# ---------------------------------------------------------------------------
# resolve_active
# ---------------------------------------------------------------------------

def test_resolve_active_empty_cfg_returns_empty_dict(tmp_path):
    cfg = PromptOptCfg(artifacts_dir=str(tmp_path), active={})
    assert resolve_active(cfg) == {}


def test_resolve_active_loads_every_pin(tmp_path):
    _write(tmp_path, "task_mode_classifier", "v1", text="a")
    _write(tmp_path, "verify_judge", "dspy-run7", text="b")
    cfg = PromptOptCfg(artifacts_dir=str(tmp_path),
                        active={"task_mode_classifier": "v1", "verify_judge": "dspy-run7"})
    active = resolve_active(cfg)
    assert set(active) == {"task_mode_classifier", "verify_judge"}
    assert active["task_mode_classifier"].text == "a"
    assert active["verify_judge"].label == "dspy-run7"


def test_resolve_active_raises_filenotfound_on_missing_pin(tmp_path):
    cfg = PromptOptCfg(artifacts_dir=str(tmp_path), active={"task_mode_classifier": "v1"})
    with pytest.raises(FileNotFoundError):
        resolve_active(cfg)


def test_resolve_active_a_well_formed_but_unverified_artifact_is_still_returned(tmp_path):
    # The `verified` gate is enforced in app/config/lint.py::enforce, NOT
    # here -- resolve_active resolves, lint/enforce judges.
    _write(tmp_path, "task_mode_classifier", "v1", verified=False)
    cfg = PromptOptCfg(artifacts_dir=str(tmp_path), active={"task_mode_classifier": "v1"})
    active = resolve_active(cfg)
    assert active["task_mode_classifier"].verified is False


# ---------------------------------------------------------------------------
# prompt_version
# ---------------------------------------------------------------------------

def test_prompt_version_empty_for_empty_active():
    assert prompt_version({}) == ""


def test_prompt_version_shape_and_stability():
    active = {"task_mode_classifier": PromptArtifact(id="task_mode_classifier", label="v1",
                                                       text="hello", verified=True)}
    v1 = prompt_version(active)
    v2 = prompt_version(active)
    assert v1 == v2
    assert isinstance(v1, str)
    assert len(v1) == 12
    int(v1, 16)  # sha256[:12] hex shape


def test_prompt_version_changes_when_text_changes():
    a1 = {"x": PromptArtifact(id="x", label="v1", text="hello", verified=True)}
    a2 = {"x": PromptArtifact(id="x", label="v1", text="hello world", verified=True)}
    assert prompt_version(a1) != prompt_version(a2)


# ---------------------------------------------------------------------------
# Default no-op boot (invariant 1)
# ---------------------------------------------------------------------------

def test_default_boot_has_no_active_prompt():
    s = load_settings()
    assert s.prompt_version == ""
    assert s.active_prompts == {}
    assert s.routing.prompt_opt.active == {}
    assert s.routing.prompt_opt.enabled is False
    assert lint_settings(s) == []
    enforce(s)  # must not raise


# ---------------------------------------------------------------------------
# Fail-closed lint guards (invariants 4 & 5)
# ---------------------------------------------------------------------------

def _settings_with_pin(tmp_path, prompt_id="task_mode_classifier", label="v1"):
    s = copy.deepcopy(load_settings())
    prompt_opt = s.routing.prompt_opt.model_copy(
        update={"artifacts_dir": str(tmp_path), "active": {prompt_id: label}})
    s.routing = s.routing.model_copy(update={"prompt_opt": prompt_opt})
    return s


def test_missing_pin_yields_named_lint_error_and_exits(tmp_path):
    s = _settings_with_pin(tmp_path)  # nothing written to tmp_path
    errs = lint_settings(s)
    assert any("PROMPT_ACTIVE_MISSING task_mode_classifier:v1" in e for e in errs)
    with pytest.raises(SystemExit):
        enforce(s)


def test_malformed_artifact_yields_named_lint_error_and_exits(tmp_path):
    _write(tmp_path, "task_mode_classifier", "v1", omit=("text",))
    s = _settings_with_pin(tmp_path)
    errs = lint_settings(s)
    assert any("PROMPT_ARTIFACT_INVALID task_mode_classifier:v1" in e for e in errs)
    with pytest.raises(SystemExit):
        enforce(s)


def test_unverified_pin_raises_config_lint_error_naming_the_pin(tmp_path):
    _write(tmp_path, "task_mode_classifier", "v1", verified=False)
    s = _settings_with_pin(tmp_path)
    with pytest.raises(ConfigLintError, match="UNVERIFIED"):
        enforce(s)
    with pytest.raises(ConfigLintError, match=r"task_mode_classifier:v1"):
        enforce(s)
    # Boot must NOT succeed silently -- lint_settings alone does not catch
    # this (the artifact is well-formed); only enforce's human-gate guard does.
    assert lint_settings(s) == []


# ---------------------------------------------------------------------------
# Activation happy path (invariant 3)
# ---------------------------------------------------------------------------

def test_activation_happy_path_computes_expected_version_and_passes_lint(tmp_path):
    _write(tmp_path, "task_mode_classifier", "v1", verified=True, text="classify this")
    s = _settings_with_pin(tmp_path)
    active = resolve_active(s.routing.prompt_opt)
    pv = prompt_version(active)
    s = s.model_copy(update={"active_prompts": active, "prompt_version": pv})

    assert lint_settings(s) == []
    enforce(s)  # must not raise -- verified: true satisfies the human gate

    assert len(pv) == 12
    assert pv == prompt_version({"task_mode_classifier": active["task_mode_classifier"]})
    assert s.prompt_version == pv
    assert s.active_prompts["task_mode_classifier"].text == "classify this"


# ---------------------------------------------------------------------------
# Telemetry threading at the production call site (invariant 6)
# ---------------------------------------------------------------------------

_AUTH = {"Authorization": "Bearer demo-key"}


def test_routes_export_trace_call_threads_prompt_version_from_settings(client, monkeypatch):
    """The one production export_trace call site (app/api/routes.py) must
    pass prompt_version=s.prompt_version -- exactly the routing_policy_version
    pattern -- so a real request's export always carries it (empty by
    default, since no config in this repo pins an active prompt)."""
    import app.api.routes as routes_mod

    captured = {}
    real_export_trace = routes_mod.export_trace

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_export_trace(*args, **kwargs)

    monkeypatch.setattr(routes_mod, "export_trace", spy)

    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "capital of France?"}]})
    assert r.status_code == 200

    assert "prompt_version" in captured
    assert captured["prompt_version"] == load_settings().prompt_version == ""


def test_shipped_sample_artifact_is_well_formed_and_verified_but_not_pinned():
    # config/prompts/task_mode_classifier/v1.yaml ships verified so an
    # operator can pin it later, but routing.yaml does NOT pin it -- default
    # boot stays a no-op (test_default_boot_has_no_active_prompt above).
    a = load_artifact("config/prompts", "task_mode_classifier", "v1")
    assert a is not None
    assert a.verified is True
    assert a.text.strip() != ""
