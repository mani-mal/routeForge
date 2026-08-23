"""The recommendation report artifact + its writer.

`RecommendationReport` is the ONLY output of this job. Writing it is the
job's entire effect on the filesystem — there is no other write path, and
`applied` is structurally pinned to `False` (`Literal[False]`) so it can
never be constructed any other way. Nothing here ever touches
`config/routing.yaml`; `proposed_routing_policy_version` is computed by
hashing a DEEP COPY, never the real file (CLAUDE.md rule 4 — no auto-apply,
no runtime self-edit).
"""
from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from app.config.loader import _policy_version
from tools.recommend.rules import Recommendation

HOW_TO_APPLY = (
    "This report is advisory only — nothing was applied. To adopt a "
    "recommendation: hand-edit config/routing.yaml yourself with the field "
    "path and recommended value shown, then reboot the service. The boot "
    "config-lint (app/config/lint.py) validates the result before it takes "
    "effect. There is no runtime apply path."
)


class Window(BaseModel):
    n_requests: int
    since: dt.datetime | None = None
    until: dt.datetime | None = None


class RecommendationReport(BaseModel):
    generated_window: Window
    current_routing_policy_version: str
    recommendations: list[Recommendation]
    proposed_routing_policy_version: str | None = None
    # Structurally pinned false: this job NEVER auto-applies anything.
    applied: Literal[False] = False
    how_to_apply: str = HOW_TO_APPLY


def _apply_field_path(routing_raw: dict, field_path: str, value: object) -> bool:
    """Set a dotted path (e.g. "complexity_thresholds.mid_max") inside
    `routing_raw` IN PLACE, but only if every segment already exists as a
    dict key — this never creates new structure, so an advisory/non-config
    field_path (e.g. "infra.onprem_capacity", or a rule-3-style path with a
    parenthetical) simply fails to resolve and is skipped. Returns whether it
    applied."""
    parts = field_path.split(".")
    node: object = routing_raw
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    last = parts[-1]
    if not isinstance(node, dict) or last not in node:
        return False
    node[last] = value
    return True


def compute_proposed_policy_version(
    recommendations: list[Recommendation],
    routing_raw: dict,
    pricing_raw: dict,
) -> str | None:
    """Apply every recommendation's concrete field_path -> recommended_value
    delta to a DEEP COPY of `routing_raw`, then hash the copy the same way
    boot does (`_policy_version`). Gives the operator the exact hash their
    traces would carry after hand-applying the accepted recommendations,
    computed WITHOUT touching the real config. Skips:
    - "review" recs where current_value == recommended_value (no delta),
    - advisory recs whose field_path isn't a real routing.yaml path.
    Returns None if no concrete delta applied. Never mutates the `routing_raw`
    / `pricing_raw` dicts passed in — only the internal copy is touched."""
    proposed = copy.deepcopy(routing_raw)
    applied_any = False
    for rec in recommendations:
        if rec.current_value == rec.recommended_value:
            continue
        if _apply_field_path(proposed, rec.field_path, rec.recommended_value):
            applied_any = True
    if not applied_any:
        return None
    return _policy_version(proposed, pricing_raw)


class ReportPathError(ValueError):
    """Raised when an --out path is unsafe, or would overwrite a file, or
    would write under the config directory."""


def safe_out_path(out_path: str | Path, *, config_dir: str | Path = "config") -> Path:
    """Guard the report's output path:
    - reject path traversal (a literal `..` path segment),
    - reject writing anywhere under `config_dir` (the job never touches
      config, on any path),
    - refuse to overwrite an existing file.
    Returns the resolved Path. Does no filesystem I/O beyond `Path.resolve()`
    and an existence check — never creates or opens anything."""
    raw = str(out_path)
    if ".." in Path(raw).parts:
        raise ReportPathError(f"--out must not contain '..': {raw!r}")
    resolved = Path(raw).resolve()
    config_resolved = Path(config_dir).resolve()
    if resolved == config_resolved or config_resolved in resolved.parents:
        raise ReportPathError(
            f"--out must not write under the config directory {str(config_dir)!r}: {raw!r}")
    if resolved.exists():
        raise ReportPathError(f"--out already exists, refusing to overwrite: {resolved}")
    return resolved


def write_report(out_path: str | Path, report: RecommendationReport, *,
                 config_dir: str | Path = "config") -> Path:
    """Write `report` as YAML to `out_path`, guarded by `safe_out_path`. The
    ONLY filesystem write this job performs."""
    resolved = safe_out_path(out_path, config_dir=config_dir)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = report.model_dump(mode="json")
    resolved.write_text(yaml.safe_dump(payload, sort_keys=False))
    return resolved
