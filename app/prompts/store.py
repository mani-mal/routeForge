"""Managed-prompt substrate (Feature 3 / DSPy prompt-optimization, Task 1).

A "managed prompt" is one of RouteForge's OWN internal prompts (the step-4
task-mode classifier is the first; verify/judge is future). Artifacts are
versioned YAML files on disk under `<artifacts_dir>/<prompt_id>/<label>.yaml`,
human-gated (an operator must set `verified: true` AND pin a `label` active in
`routing.yaml`'s `prompt_opt.active`), and attributed in telemetry via a
`prompt_version` content hash that mirrors `app/config/loader.py::
_policy_version` exactly.

This module only READS artifacts (CLAUDE.md rule 4 -- no model calls, no
optimization, no auto-promotion here). It deliberately does NOT import
`app.config.schema` -- `Settings`/`PromptOptCfg` import this module instead,
and a reverse import would create a cycle.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class PromptArtifact(BaseModel):
    id: str
    label: str
    text: str
    verified: bool = False
    # Provenance: "hand-authored" (default) or "dspy" (Task 3's optimizer sets
    # this on candidate labels it produces).
    source: str = "hand-authored"
    notes: str = ""


def load_artifact(artifacts_dir: str, prompt_id: str, label: str) -> PromptArtifact | None:
    """Read `<artifacts_dir>/<prompt_id>/<label>.yaml`.

    Returns `None` when the file does not exist -- a clean "not found" signal
    the lint layer turns into a named `PROMPT_ACTIVE_MISSING` error. Returns a
    `PromptArtifact` with `id`/`label` forced to the requested values
    otherwise. A file that exists but is unparseable or missing a required
    field (`id`/`text`) raises `ValueError` -- deliberately NOT swallowed here;
    the lint layer converts it to a named `PROMPT_ARTIFACT_INVALID` error.
    """
    path = Path(artifacts_dir, prompt_id, f"{label}.yaml")
    if not path.exists():
        return None
    try:
        raw: Any = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: unparseable YAML ({e})") from e
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: does not parse to a YAML mapping")
    if "id" not in raw or "text" not in raw:
        raise ValueError(f"{path}: missing required field(s) 'id'/'text'")
    doc = dict(raw)
    # Force id/label to the requested values -- the artifact's on-disk id must
    # not be able to smuggle in a different prompt_id than the directory it
    # was resolved from, and label comes from the filename, not the body.
    doc["id"] = prompt_id
    doc["label"] = label
    try:
        return PromptArtifact(**doc)
    except Exception as e:  # pydantic ValidationError et al.
        raise ValueError(f"{path}: invalid artifact ({e})") from e


def resolve_active(cfg: Any) -> dict[str, PromptArtifact]:
    """Load every `(prompt_id, label)` pinned in `cfg.active` (a
    `PromptOptCfg`-shaped object: `.active: dict[str, str]`, `.artifacts_dir:
    str`). Returns `{prompt_id: PromptArtifact}`.

    Missing files raise `FileNotFoundError`; malformed artifacts propagate
    `load_artifact`'s `ValueError`. Both are the caller's (the config
    loader's) problem to route to the lint layer -- a well-formed but
    UNVERIFIED artifact is still returned here. The `verified` gate is
    enforced in `app/config/lint.py::enforce`, not here: this function
    resolves, lint/enforce judges (CLAUDE.md rule 5 -- fail closed, never a
    silent skip).
    """
    active: dict[str, PromptArtifact] = {}
    for prompt_id, label in cfg.active.items():
        artifact = load_artifact(cfg.artifacts_dir, prompt_id, label)
        if artifact is None:
            raise FileNotFoundError(
                f"{prompt_id}:{label} not found under {cfg.artifacts_dir}")
        active[prompt_id] = artifact
    return active


def prompt_version(active: dict[str, PromptArtifact]) -> str:
    """First 12 hex of sha256 over the canonicalized active prompt texts.
    Mirrors `app/config/loader.py::_policy_version`'s canonical-JSON style
    precisely. Empty when nothing is active -- matches
    `Settings.routing_policy_version`'s empty-string default so "no managed
    prompt active" and "no routing policy computed" read identically."""
    if not active:
        return ""
    blob = json.dumps({pid: a.text for pid, a in active.items()}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]
