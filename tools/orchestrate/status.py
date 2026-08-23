"""CLI entry: `python -m tools.orchestrate.status`.

Polls the EXTERNAL training orchestrator for each tracked, non-terminal
job's status and updates the local job-log. When a job reaches `succeeded`
with a `result_endpoint` + `result_slug`, this PRINTS onboarding
instructions -- it never calls the onboarding path itself. Onboarding a
trained model stays operator-initiated via the existing, probe-gated
`POST /admin/infra/models` (`app/api/admin_routes.py`); this module has no
import of, or reference to, `onboard_model` / `store.upsert_onboarded` / any
onboarding function, and no in-process promotion path (CLAUDE.md rule 4).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from tools.orchestrate.client import TrainingOrchestratorClient, resolve_credentials
from tools.orchestrate.jobs import TrainingJobRecord, load_jobs, upsert_job

DEFAULT_ENDPOINT_ENV = "TRAINING_ORCHESTRATOR_URL"
DEFAULT_TOKEN_ENV = "TRAINING_ORCHESTRATOR_TOKEN"
DEFAULT_JOBS_FILE = "training-jobs.json"

TERMINAL_STATES = frozenset({"succeeded", "failed"})


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m tools.orchestrate.status",
        description=(
            "Poll status for tracked external training jobs and update the "
            "local job-log. Prints onboarding instructions for a succeeded "
            "job -- never onboards automatically."))
    p.add_argument("--job", default=None, dest="job_id",
                   help="poll only this job id (default: all non-terminal tracked jobs)")
    p.add_argument("--endpoint-env", default=DEFAULT_ENDPOINT_ENV,
                   help=f"env var NAME holding the orchestrator URL (default: {DEFAULT_ENDPOINT_ENV})")
    p.add_argument("--token-env", default=DEFAULT_TOKEN_ENV,
                   help=f"env var NAME holding the orchestrator auth token (default: {DEFAULT_TOKEN_ENV})")
    p.add_argument("--jobs-file", default=DEFAULT_JOBS_FILE)
    p.add_argument("--config-dir", default="config")
    return p.parse_args(argv)


def onboarding_instructions(record: TrainingJobRecord) -> str:
    """The instructions printed for a succeeded job -- text only. This
    function never calls an onboarding function; it only formats the
    `POST /admin/infra/models` request an operator would send by hand."""
    return (
        f"Job {record.job_id} succeeded. Onboard the trained model "
        "(RouteForge does NOT do this automatically):\n"
        "  POST /admin/infra/models  "
        f"{{slug: {record.result_slug!r}, endpoint: {record.result_endpoint!r}, "
        "tier: <cheap|mid|frontier>, ...}\n"
        "It will be probe-gated and enabled only if the probe passes.")


async def _poll(args: argparse.Namespace, *, client=None) -> list[TrainingJobRecord]:
    base_url, token = resolve_credentials(args.endpoint_env, args.token_env)
    records = load_jobs(args.jobs_file, config_dir=args.config_dir)
    if args.job_id is not None:
        records = [r for r in records if r.job_id == args.job_id]

    orchestrator = TrainingOrchestratorClient(base_url, token, client=client)
    updated: list[TrainingJobRecord] = []
    for record in records:
        if record.state in TERMINAL_STATES:
            continue
        status = await orchestrator.get_status(record.external_ref)
        new_record = record.model_copy(update={
            "state": status.state,
            "result_endpoint": status.result_endpoint,
            "result_slug": status.result_slug,
            "updated_note": status.error or "",
        })
        upsert_job(args.jobs_file, new_record, config_dir=args.config_dir)
        updated.append(new_record)
    return updated


def main(argv: list[str] | None = None, *, client=None) -> int:
    """`client` is an injectable httpx.AsyncClient-shaped object, used only
    by tests -- the real CLI always builds its own."""
    args = _parse_args(argv)
    try:
        updated = asyncio.run(_poll(args, client=client))
    except Exception as e:  # friendly error at the CLI boundary
        print(f"orchestrate status failed: {e}", file=sys.stderr)
        return 1

    if not updated:
        print("no non-terminal tracked job(s) to poll")
        return 0

    for record in updated:
        print(f"{record.job_id}: {record.state}")
        if record.state == "succeeded" and record.result_endpoint and record.result_slug:
            print(onboarding_instructions(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
