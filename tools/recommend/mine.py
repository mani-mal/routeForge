"""CLI entry: `python -m tools.recommend.mine`.

Mines RouteForge's own local `UsageRecord` metering, runs the transparent
recommendation rules, and writes a human-gated `RecommendationReport`
artifact. NEVER mutates config, NEVER calls a model, NEVER triggers anything
— purely local metering aggregation + if/then heuristics (CLAUDE.md rule 4:
"mine -> recommend, offline + human-gated"). `tools.recommend` lives OUTSIDE
the serving graph — nothing under `app/` imports it; this module importing
`app.*` for settings/DB/store is fine (one-way).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from sqlalchemy import select

from app.apps.db import Database
from app.apps.models import UsageRecord
from app.config.lint import enforce
from app.config.loader import _policy_version, _read, load_settings
from tools.recommend.aggregate import aggregate
from tools.recommend.report import (
    RecommendationReport, Window, compute_proposed_policy_version, write_report,
)
from tools.recommend.rules import run_rules

DEFAULT_MIN_REQUESTS = 20
DEFAULT_OUT = "recommendation-report.yaml"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m tools.recommend.mine",
        description=(
            "Mine local usage metering and emit a human-gated routing "
            "recommendation report. Never mutates config, never calls a "
            "model, never triggers anything."))
    p.add_argument("--db", default=None,
                   help="DATABASE_URL override (default: env DATABASE_URL / sqlite)")
    p.add_argument("--config-dir", default="config")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--since", default=None,
                   help="ISO8601 timestamp; only mine rows at/after this time")
    p.add_argument("--application-id", default=None)
    p.add_argument("--min-requests", type=int, default=DEFAULT_MIN_REQUESTS,
                   help="below this many rows in-window, decline rather than emit noise")
    return p.parse_args(argv)


async def _fetch_rows(db: Database, *, application_id: str | None,
                      since: dt.datetime | None) -> list[UsageRecord]:
    stmt = select(UsageRecord).order_by(UsageRecord.ts.asc())
    if application_id is not None:
        stmt = stmt.where(UsageRecord.application_id == application_id)
    if since is not None:
        stmt = stmt.where(UsageRecord.ts >= since)
    async with db.session() as session:
        return list((await session.execute(stmt)).scalars().all())


async def _mine(args: argparse.Namespace) -> RecommendationReport | None:
    settings = load_settings(args.config_dir)
    enforce(settings)  # fail closed on a broken config before mining it
    routing_raw = _read(args.config_dir, "routing.yaml")
    pricing_raw = _read(args.config_dir, "pricing.yaml")

    since = dt.datetime.fromisoformat(args.since) if args.since else None

    db = Database(url=args.db)
    try:
        rows = await _fetch_rows(
            db, application_id=args.application_id, since=since)
    finally:
        await db.dispose()

    if len(rows) < args.min_requests:
        print(
            f"not enough data to recommend: {len(rows)} request(s) in-window, "
            f"need >= {args.min_requests} (--min-requests)", file=sys.stderr)
        return None

    agg = aggregate(rows)
    recommendations = run_rules(agg, settings.routing)
    proposed_version = compute_proposed_policy_version(
        recommendations, routing_raw, pricing_raw)

    return RecommendationReport(
        generated_window=Window(
            n_requests=agg.n_requests, since=agg.since, until=agg.until),
        current_routing_policy_version=settings.routing_policy_version,
        recommendations=recommendations,
        proposed_routing_policy_version=proposed_version,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = asyncio.run(_mine(args))
    except SystemExit:
        raise  # let the config lint's SystemExit propagate untouched
    except Exception as e:  # friendly error at the CLI boundary
        print(f"recommend-loop failed: {e}", file=sys.stderr)
        return 1
    if report is None:
        return 0

    try:
        out_path = write_report(args.out, report, config_dir=args.config_dir)
    except Exception as e:
        print(f"recommend-loop failed: {e}", file=sys.stderr)
        return 1

    print(f"wrote recommendation report: {out_path}")
    print(f"  {len(report.recommendations)} recommendation(s) over "
          f"{report.generated_window.n_requests} request(s)")
    print("Review each recommendation, then hand-apply the ones you accept to "
          f"{args.config_dir}/routing.yaml and reboot — the boot config-lint "
          "gates the result. Nothing was applied automatically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
