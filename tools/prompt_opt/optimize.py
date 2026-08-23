"""Feature 3 (DSPy prompt-optimization), Task 3: OFFLINE, standalone DSPy
optimizer for RouteForge's own `task_mode_classifier` managed prompt.

Entry: `python -m tools.prompt_opt.optimize --dataset PATH --model SLUG
[--base-label v1] [--out-label ...] [--config-dir config]`.

**Scope (CLAUDE.md rule 4 -- hosts no compute; onboarding is the only
model-entry path):**
- This job runs OFFLINE only. Nothing under `app/` may import this package
  (see `tests/test_prompt_optimizer.py`'s subprocess isolation test).
- The DSPy LM it optimizes against MUST be an already-onboarded model,
  resolved via `ModelRegistry.effective_pool()` / `.endpoint_for(slug)`.
  There is no flag to pass a raw base_url -- accepting one would let this
  job reach a model routeForge never onboarded, which is exactly what
  onboarding-as-the-only-entry-path forbids.
- It never trains/serves anything itself; the "optimization" is DSPy calling
  that one onboarded model repeatedly as a judge/predictor over a local
  dataset. That's the only compute this job performs.

**Human-gated promotion (the whole point):** this job only ever WRITES a new
candidate `PromptArtifact` with `verified: false, source: "dspy"`. It never
sets `verified: true`, never edits `config/routing.yaml`, and never touches
`routing.prompt_opt.active`. `app/config/lint.py::enforce`'s human-gate guard
is the thing standing between a candidate and activation -- an operator must
review the file, hand-edit `verified: true`, and pin it themselves.

`dspy` is an optional extra (`uv sync --extra dspy`) and is imported ONLY
inside `run_optimization` (mirrors the `nat` lazy-import idiom in
`app/agent/runtime.py`) -- importing this module, or anything in `app/`,
never requires it to be installed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel

from tools.prompt_opt.dataset import Example, load_dataset

_PROMPT_ID = "task_mode_classifier"


class OptimizerError(Exception):
    """Friendly, user-facing error for CLI/orchestration failures (missing
    dspy, unknown model slug, missing base artifact, ...) -- never an opaque
    ImportError/KeyError bubbling out of this job."""


def _require_dspy() -> Any:
    """Lazy import: `dspy` is an optional extra. Raises a friendly
    `OptimizerError` (not a bare ImportError) when it isn't installed."""
    try:
        import dspy
    except ImportError as e:
        raise OptimizerError(
            "dspy is not installed. Install the dspy extra: "
            "`uv sync --extra dspy`.") from e
    return dspy


# ---------------------------------------------------------------------------
# Registry-only LM resolution (CLAUDE.md rule 4 -- onboarding is the only
# model-entry path). Deliberately does NOT import dspy: validating the slug
# against the registry must succeed/fail identically whether or not the dspy
# extra is installed.
# ---------------------------------------------------------------------------

class LMSpec(BaseModel):
    """Plain connection materials for a DSPy LM. Built ONLY from an
    already-onboarded registry row -- never from a caller-supplied raw
    base_url (there is no such CLI argument). `dspy.LM(...)` is constructed
    from this inside `run_optimization`, not here, so resolving it never
    requires the `dspy` extra to be installed."""
    model: str
    api_base: str
    api_key: str


def resolve_lm(registry: Any, slug: str) -> LMSpec:
    """Resolve `slug` to an already-onboarded model's connection materials
    via the registry -- mirrors `LiteLLMBackend._map`'s onprem branch
    exactly. Raises `OptimizerError` if `slug` is not present (+ enabled) in
    `registry.effective_pool()`, or has no registered endpoint (e.g. it's a
    static cloud pool entry, not an onboarded on-prem model)."""
    pool_slugs = {c.name for c in registry.effective_pool()}
    if slug not in pool_slugs:
        raise OptimizerError(
            f"model {slug!r} is not in the effective model pool (onboard it "
            "first via POST /admin/infra/models, or pass an already-enabled "
            "slug). A raw endpoint/base_url is never accepted here -- "
            "onboarding is the only model-entry path (CLAUDE.md rule 4).")
    endpoint = registry.endpoint_for(slug)
    if endpoint is None:
        raise OptimizerError(
            f"model {slug!r} has no registered endpoint (it is a static "
            "cloud pool entry, not an onboarded on-prem model) -- the DSPy "
            "LM must resolve to an onboarded model.")
    api_base, api_key_env = endpoint
    api_key = os.environ.get(api_key_env, "sk-noauth") if api_key_env else "sk-noauth"
    return LMSpec(model=f"openai/{slug}", api_base=api_base, api_key=api_key)


# ---------------------------------------------------------------------------
# The injectable heavy-optimization boundary. Tests monkeypatch
# `run_optimization` wholesale, so real DSPy / a real LM never runs in the
# test suite -- everything above and below this function is plain Python
# exercised directly.
# ---------------------------------------------------------------------------

def _exact_match_metric(example: Any, prediction: Any, trace: Any = None) -> bool:
    return getattr(example, "mode", None) == getattr(prediction, "mode", None)


def run_optimization(
    base_text: str,
    trainset: list[Example],
    lm: LMSpec,
    metric: Callable[..., bool] | None = None,
) -> str:
    """Runs the real DSPy compile step and returns the optimized prompt text.

    `dspy` and its teleprompter are imported HERE, and only here -- this is
    the one place in the whole codebase that touches the `dspy` package. A
    real `dspy.LM` is built from `lm` (already-onboarded connection
    materials; never a raw endpoint) and configured as the active LM before
    compiling a small `dspy.Predict` classifier program with `BootstrapFewShot`.

    Returns the compiled program's instruction text, with a rendered
    few-shot demo block appended when the teleprompter produced demos.
    """
    dspy = _require_dspy()
    from dspy.teleprompt import BootstrapFewShot

    real_lm = dspy.LM(model=lm.model, api_base=lm.api_base, api_key=lm.api_key)
    dspy.settings.configure(lm=real_lm)

    class ClassifyTaskMode(dspy.Signature):
        __doc__ = base_text
        text: str = dspy.InputField(desc="sanitized user request text")
        mode: str = dspy.OutputField(desc="one of 'direct' or 'multi_step'")

    program = dspy.Predict(ClassifyTaskMode)
    examples = [
        dspy.Example(text=ex.text, mode=ex.label).with_inputs("text")
        for ex in trainset
    ]

    teleprompter = BootstrapFewShot(metric=metric or _exact_match_metric)
    compiled = teleprompter.compile(program, trainset=examples)

    instructions = compiled.signature.instructions
    demos = list(getattr(compiled, "demos", None) or [])
    if not demos:
        return instructions
    demo_lines = "\n".join(
        f"- text: {d.text!r} -> mode: {d.mode!r}" for d in demos)
    return f"{instructions}\n\nFew-shot examples:\n{demo_lines}"


# ---------------------------------------------------------------------------
# Candidate artifact emission (human-gated -- the whole point).
# ---------------------------------------------------------------------------

def _content_label(optimized_text: str) -> str:
    """Deterministic, content-derived candidate label -- no timestamp/random,
    reproducible for the same optimized text."""
    return f"dspy-{hashlib.sha256(optimized_text.encode()).hexdigest()[:8]}"


_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_out_label(out_label: str, base_label: str) -> None:
    """Guards against `--out-label` clobbering the shipped/base artifact or
    escaping `artifacts_dir` (path traversal). Raises `OptimizerError` --
    never lets an unsafe label reach a filesystem write."""
    if out_label == base_label:
        raise OptimizerError(
            f"--out-label {out_label!r} is the same as --base-label "
            f"{base_label!r} -- refusing to overwrite the base artifact. "
            "Pick a different --out-label.")
    if "/" in out_label or "\\" in out_label:
        raise OptimizerError(
            f"--out-label {out_label!r} must not contain a path separator.")
    if ".." in out_label:
        raise OptimizerError(f"--out-label {out_label!r} must not contain '..'.")
    if out_label.startswith("."):
        raise OptimizerError(
            f"--out-label {out_label!r} must not start with '.'.")
    if not _SAFE_LABEL_RE.match(out_label):
        raise OptimizerError(
            f"--out-label {out_label!r} contains unsafe characters -- only "
            "letters, digits, '.', '_', '-' are allowed.")


def emit_candidate(
    artifacts_dir: str,
    optimized_text: str,
    *,
    base_label: str,
    dataset_path: str,
    model_slug: str,
    example_count: int,
    prompt_id: str = _PROMPT_ID,
    out_label: str | None = None,
) -> Path:
    """Writes `<artifacts_dir>/<prompt_id>/<label>.yaml` as a NEW candidate
    `PromptArtifact`: `verified: false` (always, never `true`), `source:
    "dspy"`. Never touches `config/routing.yaml` or `prompt_opt.active` --
    those live entirely outside `artifacts_dir` and this function writes
    nothing else.

    Guards (checked BEFORE any filesystem write, so a bad `out_label` or an
    existing candidate can never be clobbered):
    - `out_label` may not equal `base_label` (would overwrite the base
      artifact -- e.g. the shipped, hand-authored, `verified: true` v1.yaml).
    - `out_label` must be a safe single filename stem: no path separator, no
      `..`, no leading dot, and only `[A-Za-z0-9._-]` characters (blocks path
      traversal outside `artifacts_dir`).
    - the resolved `out_path` must not already exist (refuses to overwrite
      any prior candidate, including a content-hash re-run landing on the
      same default label)."""
    from app.prompts.store import PromptArtifact  # local: keep this module dspy-import-free at top level

    if out_label is not None:
        _validate_out_label(out_label, base_label)
    label = out_label or _content_label(optimized_text)

    out_path = Path(artifacts_dir, prompt_id, f"{label}.yaml")
    if out_path.exists():
        raise OptimizerError(
            f"candidate artifact already exists at {out_path} -- refusing to "
            "overwrite. Pick a different --out-label, or remove the old "
            "file if you intend to replace it.")

    notes = (
        f"DSPy candidate optimized from base label {base_label!r} using "
        f"dataset {dataset_path!r} ({example_count} examples) against "
        f"registry-resolved model {model_slug!r}. NOT auto-activated: "
        "review this file, set verified: true by hand, and pin it in "
        f"routing.prompt_opt.active.{prompt_id} to promote (the boot config "
        "lint rejects an unverified pin -- CLAUDE.md rule 5).")
    artifact = PromptArtifact(
        id=prompt_id, label=label, text=optimized_text,
        verified=False, source="dspy", notes=notes)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(artifact.model_dump(), sort_keys=False))
    return out_path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def optimize_and_emit(
    *,
    dataset_path: str,
    model_slug: str,
    registry: Any,
    artifacts_dir: str,
    base_label: str = "v1",
    out_label: str | None = None,
    prompt_id: str = _PROMPT_ID,
    metric: Callable[..., bool] | None = None,
) -> Path:
    """The end-to-end orchestration core: load the base artifact + dataset,
    resolve the LM from the registry (never a raw endpoint), run optimization
    via the injectable `run_optimization` boundary, and emit a new unverified
    candidate artifact. Raises `OptimizerError` / `ValueError` on any
    failure -- callers (CLI `main`) turn those into a clean exit code."""
    from app.prompts.store import load_artifact  # local: keeps this module import-light at top level

    base = load_artifact(artifacts_dir, prompt_id, base_label)
    if base is None:
        raise OptimizerError(
            f"base artifact {prompt_id}:{base_label} not found under {artifacts_dir}")

    trainset = load_dataset(dataset_path)
    lm = resolve_lm(registry, model_slug)
    optimized_text = run_optimization(base.text, trainset, lm, metric)

    return emit_candidate(
        artifacts_dir, optimized_text,
        base_label=base_label, dataset_path=dataset_path, model_slug=model_slug,
        example_count=len(trainset), prompt_id=prompt_id, out_label=out_label)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m tools.prompt_opt.optimize",
        description=(
            "OFFLINE DSPy optimizer for RouteForge's step-4 task-mode "
            "classifier prompt. Writes a candidate (verified: false) "
            "PromptArtifact for a human to review and promote by hand -- "
            "never activates anything itself."))
    p.add_argument("--dataset", required=True,
                    help="local JSONL eval dataset path ({\"text\":..., \"label\":...} per line)")
    p.add_argument("--model", required=True,
                    help="onboarded model slug, resolved via the ModelRegistry "
                         "(NOT a raw endpoint/base_url)")
    p.add_argument("--base-label", default="v1",
                    help="base artifact label to optimize from (default: v1)")
    p.add_argument("--out-label", default=None,
                    help="override the content-derived candidate label")
    p.add_argument("--config-dir", default="config",
                    help="config directory to load Settings from (default: config)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    from app.apps.db import Database
    from app.apps.registry import ModelRegistry
    from app.config.loader import load_settings

    try:
        settings = load_settings(args.config_dir)
        db = Database()
        registry = ModelRegistry(settings)
        asyncio.run(registry.refresh(db))

        out_path = optimize_and_emit(
            dataset_path=args.dataset,
            model_slug=args.model,
            registry=registry,
            artifacts_dir=settings.routing.prompt_opt.artifacts_dir,
            base_label=args.base_label,
            out_label=args.out_label,
        )
    except Exception as e:
        # Broad on purpose: any startup/runtime failure (a bad --config-dir
        # raising FileNotFoundError, a malformed YAML, an OptimizerError /
        # ValueError from the pipeline, ...) becomes this one friendly line
        # instead of a raw traceback. `SystemExit` (raised by the config lint
        # in `load_settings`/`enforce` on a real boot-config violation, and
        # already printing its own named CONFIG_LINT line) is a BaseException,
        # not an Exception, so it is never caught here -- it propagates and
        # keeps its own exit code.
        print(f"prompt-opt: error: {e}", file=sys.stderr)
        return 1

    print(f"prompt-opt: wrote candidate artifact: {out_path}")
    print(
        "prompt-opt: this candidate is UNVERIFIED and NOT active. To promote it:\n"
        f"  1. Review {out_path}\n"
        "  2. Set `verified: true` in that file by hand\n"
        f"  3. Pin it: config/routing.yaml -> routing.prompt_opt.active.{_PROMPT_ID}: "
        f"{out_path.stem!r}\n"
        "The boot config lint rejects an unverified pin (CLAUDE.md rule 5 -- "
        "fail closed; rule 4 -- human-gated promotion).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
