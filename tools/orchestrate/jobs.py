"""Local, file-based job-log for external training/distill jobs
(tools.orchestrate).

Deliberately a plain JSON file, NOT a DB table — this avoids the
no-migrations gotcha (`app/apps/models.py`'s `create_all` never `ALTER`s an
existing deployed DB) and keeps this offline hook entirely off the serving
DB. It never carries a secret: only the job spec + status fields documented
in `TrainingJobRecord` are ever written, never the orchestrator token (see
`tools.orchestrate.client.resolve_credentials`).
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel


class TrainingJobRecord(BaseModel):
    # job_id is deliberately just the external_ref: it's already a unique,
    # deterministic id assigned by the external service, so there's no need
    # for a separate local uuid/timestamp scheme.
    job_id: str
    external_ref: str
    state: str
    base_model_ref: str
    method: str
    dataset_ref: str
    result_endpoint: str | None = None
    result_slug: str | None = None
    created_note: str = ""
    updated_note: str = ""


class JobsPathError(ValueError):
    """Raised when a --jobs-file path is unsafe: path traversal, or a path
    under the config directory."""


def safe_jobs_path(path: str | Path, *, config_dir: str | Path = "config") -> Path:
    """Guard the job-log path: reject a literal `..` path segment, and
    reject any path under `config_dir` (this job never touches config, on
    any path — mirrors `tools.recommend.report.safe_out_path`). Unlike that
    helper, this does NOT refuse an existing file: the job-log is upserted
    across many CLI invocations, not written once."""
    raw = str(path)
    if ".." in Path(raw).parts:
        raise JobsPathError(f"--jobs-file must not contain '..': {raw!r}")
    resolved = Path(raw).resolve()
    config_resolved = Path(config_dir).resolve()
    if resolved == config_resolved or config_resolved in resolved.parents:
        raise JobsPathError(
            f"--jobs-file must not write under the config directory "
            f"{str(config_dir)!r}: {raw!r}")
    return resolved


def load_jobs(path: str | Path, *, config_dir: str | Path = "config"
             ) -> list[TrainingJobRecord]:
    """Read the job-log at `path`. A missing file is not an error — it
    simply means no jobs are tracked yet."""
    resolved = safe_jobs_path(path, config_dir=config_dir)
    if not resolved.exists():
        return []
    raw = json.loads(resolved.read_text())
    return [TrainingJobRecord(**r) for r in raw]


def upsert_job(path: str | Path, record: TrainingJobRecord, *,
               config_dir: str | Path = "config") -> Path:
    """Insert or update `record` (keyed on `job_id`) in the job-log at
    `path`, creating the file/parent directories if needed. Returns the
    resolved path."""
    resolved = safe_jobs_path(path, config_dir=config_dir)
    by_id = {r.job_id: r for r in load_jobs(resolved, config_dir=config_dir)}
    by_id[record.job_id] = record
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps([r.model_dump(mode="json") for r in by_id.values()], indent=2))
    return resolved
