"""CLI entry: `python -m tools.orchestrate.trigger`.

Builds a `TrainingJobSpec` and submits it to the EXTERNAL training
orchestrator, then records the returned job in the local job-log
(`tools.orchestrate.jobs`). RouteForge TRIGGERS the job here — it does not
run it. See `tools.orchestrate.client` for the "hosts no compute" boundary.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from tools.orchestrate.client import (
    TrainingJobSpec, TrainingOrchestratorClient, resolve_credentials,
)
from tools.orchestrate.jobs import TrainingJobRecord, upsert_job

DEFAULT_ENDPOINT_ENV = "TRAINING_ORCHESTRATOR_URL"
DEFAULT_TOKEN_ENV = "TRAINING_ORCHESTRATOR_TOKEN"
DEFAULT_JOBS_FILE = "training-jobs.json"


def _parse_hyperparam(item: str) -> tuple[str, str]:
    if "=" not in item:
        raise argparse.ArgumentTypeError(f"--hyperparam must be k=v, got {item!r}")
    k, v = item.split("=", 1)
    return k, v


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m tools.orchestrate.trigger",
        description=(
            "Trigger an external training/distillation job. RouteForge "
            "does not run this job itself: it POSTs a spec to an external "
            "training orchestrator and records the returned job id for "
            "later status polling."))
    p.add_argument("--base-model", required=True, dest="base_model_ref",
                   help="a ref the EXTERNAL trainer understands (not a ModelRegistry slug)")
    p.add_argument("--method", required=True, help="e.g. distill | finetune")
    p.add_argument(
        "--dataset-ref", required=True,
        help="a REFERENCE to a residency-local exported dataset the external "
             "service already has access to -- never raw dataset content")
    p.add_argument("--hyperparam", action="append", default=[], metavar="K=V",
                   help="repeatable; e.g. --hyperparam epochs=3")
    p.add_argument("--notes", default="")
    p.add_argument("--endpoint-env", default=DEFAULT_ENDPOINT_ENV,
                   help=f"env var NAME holding the orchestrator URL (default: {DEFAULT_ENDPOINT_ENV})")
    p.add_argument("--token-env", default=DEFAULT_TOKEN_ENV,
                   help=f"env var NAME holding the orchestrator auth token (default: {DEFAULT_TOKEN_ENV})")
    p.add_argument("--jobs-file", default=DEFAULT_JOBS_FILE)
    p.add_argument("--config-dir", default="config")
    return p.parse_args(argv)


async def _trigger(args: argparse.Namespace, *, client=None) -> str:
    base_url, token = resolve_credentials(args.endpoint_env, args.token_env)
    hyperparams = dict(_parse_hyperparam(item) for item in args.hyperparam)
    spec = TrainingJobSpec(
        base_model_ref=args.base_model_ref, method=args.method,
        dataset_ref=args.dataset_ref, hyperparams=hyperparams, notes=args.notes)

    orchestrator = TrainingOrchestratorClient(base_url, token, client=client)
    external_ref = await orchestrator.submit_job(spec)

    record = TrainingJobRecord(
        job_id=external_ref, external_ref=external_ref, state="submitted",
        base_model_ref=spec.base_model_ref, method=spec.method,
        dataset_ref=spec.dataset_ref,
        created_note=f"submitted via trigger CLI ({spec.method})")
    upsert_job(args.jobs_file, record, config_dir=args.config_dir)
    return external_ref


def main(argv: list[str] | None = None, *, client=None) -> int:
    """`client` is an injectable httpx.AsyncClient-shaped object, used only
    by tests -- the real CLI always builds its own."""
    args = _parse_args(argv)
    try:
        external_ref = asyncio.run(_trigger(args, client=client))
    except Exception as e:  # friendly error at the CLI boundary
        print(f"orchestrate trigger failed: {e}", file=sys.stderr)
        return 1

    print(f"submitted training job: {external_ref}")
    print(f"poll status with: python -m tools.orchestrate.status --job {external_ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
