"""Feature 3 (DSPy prompt-optimization), Task 3: the OFFLINE DSPy optimizer
(`tools/prompt_opt/`).

Covers the 6 invariants from the task brief:
1. structural isolation -- the serving graph never imports dspy (subprocess)
2. dspy is optional -- app.main imports fine without it; the optimizer's own
   guard raises a friendly error when it's absent
3. candidate emission contract (optimization mocked, no real dspy/LM)
4. human-gate holds -- a pinned candidate fails boot (reuses Task 1's enforce)
5. registry-only model resolution (rule 4) -- rejects an un-onboarded slug
6. dataset validation is fail-closed

NO network, NO real DSPy run, NO real LM anywhere in this file.
"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.config.lint import ConfigLintError, enforce
from app.config.loader import load_settings
from app.prompts.store import load_artifact
from tools.prompt_opt.dataset import load_dataset
from tools.prompt_opt.optimize import (
    OptimizerError,
    emit_candidate,
    optimize_and_emit,
    resolve_lm,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Test fixtures / stubs
# ---------------------------------------------------------------------------

class _FakeCandidate:
    def __init__(self, name: str) -> None:
        self.name = name


class _StubRegistry:
    """Minimal stand-in for `ModelRegistry` -- no DB, no async, no real
    onboarded row. Enough surface for `resolve_lm`/`optimize_and_emit`:
    `effective_pool()` and `endpoint_for(slug)`."""

    def __init__(self, pool: dict[str, tuple[str, str | None] | None]) -> None:
        # pool: {slug: (endpoint, api_key_env) | None}. A slug present with
        # value None simulates "in the pool but no endpoint" (e.g. a static
        # cloud pool member, not onboarded).
        self._pool = pool

    def effective_pool(self):
        return [_FakeCandidate(slug) for slug in self._pool]

    def endpoint_for(self, slug: str):
        return self._pool.get(slug)


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


def _write_base_artifact(artifacts_dir: Path, *, label: str = "v1",
                          text: str = "classify direct vs multi_step") -> None:
    d = artifacts_dir / "task_mode_classifier"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{label}.yaml").write_text(yaml.safe_dump({
        "id": "task_mode_classifier", "label": label, "verified": True,
        "source": "hand-authored", "notes": "", "text": text,
    }))


# ---------------------------------------------------------------------------
# Invariant 1 -- structural isolation (the load-bearing rule-4 test)
# ---------------------------------------------------------------------------

def test_serving_graph_never_imports_dspy_subprocess():
    """A fresh subprocess importing app.main must never pull dspy into
    sys.modules -- written as a subprocess so a leaked import elsewhere in
    the process (e.g. a prior test importing tools.prompt_opt.optimize)
    can't mask a real violation."""
    env = dict(os.environ)
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    env.setdefault("SP_PII_DETECTION", "regex")
    result = subprocess.run(
        [sys.executable, "-c",
         "import app.main, sys; assert 'dspy' not in sys.modules"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}")


# ---------------------------------------------------------------------------
# Invariant 2 -- dspy is optional
# ---------------------------------------------------------------------------

def test_app_main_importable_without_dspy():
    """Importing the serving graph in-process must not require dspy either
    (belt-and-suspenders alongside the subprocess test above)."""
    import app.main  # noqa: F401
    assert "dspy" not in sys.modules


def test_optimizer_raises_friendly_error_when_dspy_absent(monkeypatch):
    """Force `import dspy` to fail regardless of whether the extra happens to
    be installed in this environment (setting sys.modules[name] = None makes
    CPython raise ImportError on the next `import dspy`)."""
    monkeypatch.setitem(sys.modules, "dspy", None)
    from tools.prompt_opt.optimize import _require_dspy
    with pytest.raises(OptimizerError, match="dspy extra"):
        _require_dspy()


# ---------------------------------------------------------------------------
# Invariant 3 -- candidate emission contract (optimization mocked)
# ---------------------------------------------------------------------------

def test_optimize_and_emit_writes_unverified_dspy_candidate(tmp_path, monkeypatch):
    artifacts_dir = tmp_path / "prompts"
    _write_base_artifact(artifacts_dir)

    dataset_path = tmp_path / "eval.jsonl"
    _write_jsonl(dataset_path, [
        '{"text": "what is the capital of France?", "label": "direct"}',
        '{"text": "look up the weather then draft an email about it", "label": "multi_step"}',
    ])

    registry = _StubRegistry({"onprem-test-model": ("http://onprem-host:8000/v1", None)})

    canned_text = "OPTIMIZED: answer with exactly direct or multi_step."
    import tools.prompt_opt.optimize as optimize_mod
    monkeypatch.setattr(optimize_mod, "run_optimization",
                         lambda base_text, trainset, lm, metric=None: canned_text)

    out_path = optimize_and_emit(
        dataset_path=str(dataset_path),
        model_slug="onprem-test-model",
        registry=registry,
        artifacts_dir=str(artifacts_dir),
    )

    expected_label = optimize_mod._content_label(canned_text)
    assert out_path == artifacts_dir / "task_mode_classifier" / f"{expected_label}.yaml"
    assert out_path.exists()

    artifact = load_artifact(str(artifacts_dir), "task_mode_classifier", expected_label)
    assert artifact is not None
    assert artifact.verified is False
    assert artifact.source == "dspy"
    assert artifact.text == canned_text
    assert artifact.label == expected_label

    # Never touches routing.yaml / prompt_opt.active -- nothing outside
    # artifacts_dir was written at all.
    assert not (tmp_path / "routing.yaml").exists()
    assert set(tmp_path.iterdir()) == {artifacts_dir, dataset_path}


def test_optimize_and_emit_out_label_override(tmp_path, monkeypatch):
    artifacts_dir = tmp_path / "prompts"
    _write_base_artifact(artifacts_dir)
    dataset_path = tmp_path / "eval.jsonl"
    _write_jsonl(dataset_path, ['{"text": "hi", "label": "direct"}'])
    registry = _StubRegistry({"m": ("http://h:8000/v1", None)})

    import tools.prompt_opt.optimize as optimize_mod
    monkeypatch.setattr(optimize_mod, "run_optimization",
                         lambda *a, **k: "some optimized text")

    out_path = optimize_and_emit(
        dataset_path=str(dataset_path), model_slug="m", registry=registry,
        artifacts_dir=str(artifacts_dir), out_label="my-run-7")

    assert out_path.name == "my-run-7.yaml"
    artifact = load_artifact(str(artifacts_dir), "task_mode_classifier", "my-run-7")
    assert artifact.verified is False
    assert artifact.source == "dspy"


def test_emit_candidate_never_sets_verified_true(tmp_path):
    out_path = emit_candidate(
        str(tmp_path), "some text", base_label="v1", dataset_path="d.jsonl",
        model_slug="m", example_count=3)
    doc = yaml.safe_load(out_path.read_text())
    assert doc["verified"] is False
    assert doc["source"] == "dspy"


# ---------------------------------------------------------------------------
# Fix round 1 -- `--out-label` guards (clobber / traversal / overwrite)
# ---------------------------------------------------------------------------

def test_emit_candidate_rejects_out_label_equal_to_base_label(tmp_path):
    """`--out-label v1` (== --base-label) must never be allowed to overwrite
    the shipped, hand-authored, verified base artifact."""
    with pytest.raises(OptimizerError, match="same as --base-label"):
        emit_candidate(
            str(tmp_path), "some text", base_label="v1", dataset_path="d.jsonl",
            model_slug="m", example_count=1, out_label="v1")
    # Nothing written at all.
    assert not (tmp_path / "task_mode_classifier").exists()


@pytest.mark.parametrize("bad_label", [
    "../v1", "../../etc/passwd", "sub/dir", "sub\\dir", "..", "...", ".hidden",
])
def test_emit_candidate_rejects_unsafe_out_label(tmp_path, bad_label):
    """Path separators, `..`, and leading dots must be rejected -- a
    traversal label like `../../x` must never write outside artifacts_dir."""
    with pytest.raises(OptimizerError):
        emit_candidate(
            str(tmp_path), "some text", base_label="v1", dataset_path="d.jsonl",
            model_slug="m", example_count=1, out_label=bad_label)
    # No file was written anywhere under or outside tmp_path.
    written = list(tmp_path.rglob("*.yaml"))
    assert written == []


def test_emit_candidate_refuses_to_overwrite_existing_artifact(tmp_path):
    """A second emit at the same label (explicit or content-derived) must
    error instead of silently clobbering the first candidate."""
    out_path = emit_candidate(
        str(tmp_path), "some text", base_label="v1", dataset_path="d.jsonl",
        model_slug="m", example_count=1, out_label="dup-run")
    original_contents = out_path.read_text()

    with pytest.raises(OptimizerError, match="already exists"):
        emit_candidate(
            str(tmp_path), "different text", base_label="v1", dataset_path="d.jsonl",
            model_slug="m", example_count=1, out_label="dup-run")

    # The original candidate is untouched.
    assert out_path.read_text() == original_contents


def test_emit_candidate_never_modifies_shipped_v1_artifact(tmp_path):
    """Prove the refuse-overwrite guard protects the real shipped base
    artifact: pre-seed a tmp artifacts_dir with a copy of the shipped
    config/prompts/task_mode_classifier/v1.yaml and assert emit_candidate
    cannot touch it via either the base-label-clobber guard or the
    refuse-overwrite guard, and the real repo file is untouched throughout."""
    real_v1 = REPO_ROOT / "config" / "prompts" / "task_mode_classifier" / "v1.yaml"
    real_v1_contents_before = real_v1.read_text()

    artifacts_dir = tmp_path / "prompts"
    _write_base_artifact(artifacts_dir, label="v1", text=real_v1_contents_before)

    # (1) --out-label v1 is rejected outright (clobber guard).
    with pytest.raises(OptimizerError, match="same as --base-label"):
        emit_candidate(
            str(artifacts_dir), "candidate text", base_label="v1",
            dataset_path="d.jsonl", model_slug="m", example_count=1,
            out_label="v1")

    # (2) Even a distinct label that happens to already exist on disk is
    # refused (refuse-overwrite guard) -- simulate a prior candidate file
    # sitting at the target path.
    existing = artifacts_dir / "task_mode_classifier" / "already-there.yaml"
    existing.write_text("sentinel: true\n")
    with pytest.raises(OptimizerError, match="already exists"):
        emit_candidate(
            str(artifacts_dir), "candidate text", base_label="v1",
            dataset_path="d.jsonl", model_slug="m", example_count=1,
            out_label="already-there")
    assert existing.read_text() == "sentinel: true\n"

    # The v1.yaml artifact in the tmp dir (standing in for the shipped file)
    # was never modified by either attempt.
    v1_path = artifacts_dir / "task_mode_classifier" / "v1.yaml"
    assert yaml.safe_load(v1_path.read_text())["verified"] is True

    # And the actual repo-shipped file was never touched by this test.
    assert real_v1.read_text() == real_v1_contents_before


# ---------------------------------------------------------------------------
# Invariant 4 -- human-gate holds (reuses Task 1's enforce guard)
# ---------------------------------------------------------------------------

def test_emitted_candidate_pinned_fails_boot(tmp_path, monkeypatch):
    artifacts_dir = tmp_path / "prompts"
    _write_base_artifact(artifacts_dir)
    dataset_path = tmp_path / "eval.jsonl"
    _write_jsonl(dataset_path, ['{"text": "hi", "label": "direct"}'])
    registry = _StubRegistry({"m": ("http://h:8000/v1", None)})

    import tools.prompt_opt.optimize as optimize_mod
    monkeypatch.setattr(optimize_mod, "run_optimization",
                         lambda *a, **k: "candidate optimized text")

    out_path = optimize_and_emit(
        dataset_path=str(dataset_path), model_slug="m", registry=registry,
        artifacts_dir=str(artifacts_dir))
    label = out_path.stem

    s = copy.deepcopy(load_settings())
    prompt_opt = s.routing.prompt_opt.model_copy(
        update={"artifacts_dir": str(artifacts_dir),
                "active": {"task_mode_classifier": label}})
    s.routing = s.routing.model_copy(update={"prompt_opt": prompt_opt})

    with pytest.raises(ConfigLintError, match="UNVERIFIED"):
        enforce(s)


# ---------------------------------------------------------------------------
# Invariant 5 -- registry-only model resolution (rule 4)
# ---------------------------------------------------------------------------

def test_resolve_lm_rejects_slug_not_in_effective_pool():
    registry = _StubRegistry({})
    with pytest.raises(OptimizerError, match="not in the effective model pool"):
        resolve_lm(registry, "ghost-model")


def test_resolve_lm_rejects_slug_with_no_endpoint():
    # Present in the pool but no registered endpoint (e.g. a static cloud
    # pool member, not an onboarded on-prem model).
    registry = _StubRegistry({"cloud-frontier": None})
    with pytest.raises(OptimizerError, match="no registered endpoint"):
        resolve_lm(registry, "cloud-frontier")


def test_resolve_lm_happy_path_builds_lm_spec_from_registry():
    registry = _StubRegistry({"onprem-a": ("http://onprem-a:8000/v1", "SOME_KEY_ENV")})
    lm = resolve_lm(registry, "onprem-a")
    assert lm.model == "openai/onprem-a"
    assert lm.api_base == "http://onprem-a:8000/v1"
    # no env var set -> falls back to the placeholder, never a raw secret
    assert lm.api_key == "sk-noauth"


def test_optimize_and_emit_rejects_unknown_model_slug(tmp_path):
    artifacts_dir = tmp_path / "prompts"
    _write_base_artifact(artifacts_dir)
    dataset_path = tmp_path / "eval.jsonl"
    _write_jsonl(dataset_path, ['{"text": "hi", "label": "direct"}'])
    registry = _StubRegistry({})  # empty pool

    with pytest.raises(OptimizerError, match="not in the effective model pool"):
        optimize_and_emit(
            dataset_path=str(dataset_path), model_slug="not-onboarded",
            registry=registry, artifacts_dir=str(artifacts_dir))

    # No candidate written on rejection.
    assert not (artifacts_dir / "task_mode_classifier").exists() or not any(
        (artifacts_dir / "task_mode_classifier").glob("dspy-*.yaml"))


# ---------------------------------------------------------------------------
# Invariant 6 -- dataset validation is fail-closed
# ---------------------------------------------------------------------------

def test_load_dataset_happy_path(tmp_path):
    p = tmp_path / "eval.jsonl"
    _write_jsonl(p, [
        '{"text": "what is 2+2?", "label": "direct"}',
        '{"text": "book a flight then email the itinerary", "label": "multi_step"}',
    ])
    examples = load_dataset(str(p))
    assert len(examples) == 2
    assert examples[0].label == "direct"
    assert examples[1].label == "multi_step"


def test_load_dataset_raises_on_unknown_label(tmp_path):
    p = tmp_path / "eval.jsonl"
    _write_jsonl(p, ['{"text": "hi", "label": "sometimes"}'])
    with pytest.raises(ValueError, match="not in"):
        load_dataset(str(p))


def test_load_dataset_raises_on_missing_label(tmp_path):
    p = tmp_path / "eval.jsonl"
    _write_jsonl(p, ['{"text": "hi"}'])
    with pytest.raises(ValueError, match="missing required field"):
        load_dataset(str(p))


def test_load_dataset_raises_on_malformed_json(tmp_path):
    p = tmp_path / "eval.jsonl"
    _write_jsonl(p, ['{"text": "hi", "label": "direct"', '{"text": "ok", "label": "direct"}'])
    with pytest.raises(ValueError, match="unparseable JSON"):
        load_dataset(str(p))


def test_load_dataset_raises_on_empty_file(tmp_path):
    p = tmp_path / "eval.jsonl"
    p.write_text("")
    with pytest.raises(ValueError, match="no examples found"):
        load_dataset(str(p))


def test_load_dataset_skips_blank_lines(tmp_path):
    p = tmp_path / "eval.jsonl"
    _write_jsonl(p, ['{"text": "hi", "label": "direct"}', '', '   '])
    examples = load_dataset(str(p))
    assert len(examples) == 1


def test_load_dataset_missing_file_raises(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_dataset(str(tmp_path / "nope.jsonl"))


# ---------------------------------------------------------------------------
# Fix round 1 (Minor) -- main() surfaces startup errors as the friendly line,
# not a raw traceback, for exception types beyond OptimizerError/ValueError.
# ---------------------------------------------------------------------------

def test_main_reports_friendly_error_for_bad_config_dir(tmp_path, capsys):
    from tools.prompt_opt.optimize import main

    rc = main([
        "--dataset", str(tmp_path / "eval.jsonl"),
        "--model", "m",
        "--config-dir", str(tmp_path / "does-not-exist"),
    ])

    assert rc == 1
    captured = capsys.readouterr()
    assert "prompt-opt: error:" in captured.err
    assert "Traceback" not in captured.err
