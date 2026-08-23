"""Command-line entry point for the RouteForge LangChain sample."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from pydantic import SecretStr

from routeforge_sample.agent import SupportInvestigator
from routeforge_sample.routeforge import RouteForgeChatModel, RouteForgeError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Investigate a synthetic support ticket")
    parser.add_argument("ticket_id", nargs="?", default="TCK-1001", choices=("TCK-1001", "TCK-1002"))
    parser.add_argument("--check-health", action="store_true", help="check RouteForge before running")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    return parser


def _call_summary(index: int, call: dict[str, Any]) -> str:
    policy = call.get("policy", {})
    cost = call.get("cost", {})
    entities = ",".join(policy.get("detected_entities", [])) or "none"
    return (
        f"hop={index} request={call.get('request_id')} model={call.get('model')} "
        f"onprem_only={policy.get('onprem_only')} entities={entities} "
        f"cost=${float(cost.get('total_usd', 0.0)):.6f}"
    )


def main() -> None:
    args = _parser().parse_args()
    base_url = os.environ.get("ROUTEFORGE_BASE_URL", "http://localhost:8080/v1")
    api_key = os.environ.get("ROUTEFORGE_API_KEY")
    if not api_key:
        print("ROUTEFORGE_API_KEY is required (it should start with rf_live_)", file=sys.stderr)
        raise SystemExit(2)

    model = RouteForgeChatModel(base_url=base_url, api_key=SecretStr(api_key))
    try:
        if args.check_health:
            print("RouteForge health:", json.dumps(model.health(), sort_keys=True), file=sys.stderr)
        result = SupportInvestigator(model).investigate(args.ticket_id)
    except RouteForgeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        model.close()

    if args.json:
        print(
            json.dumps(
                {
                    "ticket_id": result.ticket_id,
                    "answer": result.answer,
                    "total_cost_usd": result.total_cost_usd,
                    "routeforge_calls": result.calls,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    print(f"\nInvestigation: {result.ticket_id}\n")
    print(result.answer)
    print("\nRouteForge calls")
    for index, call in enumerate(result.calls, start=1):
        print("  " + _call_summary(index, call))
    print(f"  total_cost=${result.total_cost_usd:.6f}")


if __name__ == "__main__":
    main()

