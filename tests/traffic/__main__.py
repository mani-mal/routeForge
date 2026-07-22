"""Traffic generator CLI (traffic-and-tests skill, Phase 7 Task 2).

Fires a seeded, weighted mix of synthetic personas at a *running* Serving
Plane API and prints the demo's headline numbers — tier distribution, total
vs. baseline cost, % saved, on-prem count, p50/p95 latency (the numbers
you'll read aloud, per the traffic-and-tests skill).

Usage:
    python -m tests.traffic --requests 500 --rps 10 --seed 42
    python -m tests.traffic --requests 500 --seed 42 --compare-compression
"""

from __future__ import annotations

import argparse
import asyncio

from tests.traffic.generator import Summary, run_traffic


def _print_summary(title: str, s: Summary) -> None:
    print(f"\n=== {title} ===")
    print(f"requests:          {s.count}  (errors: {len(s.errors)})")
    print("tier distribution:")
    for tier, count in sorted(s.tier_distribution.items()):
        pct = (count / s.count * 100.0) if s.count else 0.0
        print(f"  {tier:10s} {count:5d}  ({pct:5.1f}%)")
    print(f"total cost:        ${s.total_usd:.4f}")
    print(f"baseline cost:     ${s.baseline_usd:.4f}  (all-frontier)")
    print(f"% saved:           {s.pct_saved:.1f}%")
    print(f"  routing saved:      ${s.routing_saved_usd:.4f}")
    print(f"  compression saved:  ${s.compression_saved_usd:.4f}")
    print(f"on-prem count:     {s.onprem_count}")
    print(f"latency p50/p95:   {s.p50_ms:.1f}ms / {s.p95_ms:.1f}ms")
    if s.errors:
        print(f"errors (first 10 of {len(s.errors)}):")
        for e in s.errors[:10]:
            print(f"  {e}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m tests.traffic",
        description="Fire a seeded, weighted-persona traffic burst at the "
                     "Serving Plane API and print the demo's headline "
                     "numbers.",
    )
    p.add_argument("--requests", type=int, default=500,
                    help="number of requests to fire (default: 500)")
    p.add_argument("--rps", type=float, default=10,
                    help="target requests/sec pacing (default: 10)")
    p.add_argument("--seed", type=int, default=42,
                    help="RNG seed for reproducible traffic (default: 42)")
    p.add_argument("--base-url", default="http://127.0.0.1:8080",
                    help="Serving Plane base URL (default: "
                         "http://127.0.0.1:8080)")
    p.add_argument("--profile", default="demo",
                    help="named traffic profile (default: demo; currently "
                         "the only profile defined — accepted for forward "
                         "compatibility)")
    p.add_argument("--compare-compression", action="store_true",
                    help="run the same seed twice (Headroom off, then on) "
                         "and print both summaries side by side")
    return p.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> None:
    if args.compare_compression:
        off = await run_traffic(
            args.base_url, n=args.requests, rps=args.rps, seed=args.seed,
            headroom=False)
        _print_summary("Headroom OFF", off)

        on = await run_traffic(
            args.base_url, n=args.requests, rps=args.rps, seed=args.seed,
            headroom=True)
        _print_summary("Headroom ON", on)

        print("\n=== A/B: compression lever (same seed, off vs on) ===")
        print(f"compression saved   off -> on: "
              f"${off.compression_saved_usd:.4f} -> "
              f"${on.compression_saved_usd:.4f}")
        print(f"total cost          off -> on: "
              f"${off.total_usd:.4f} -> ${on.total_usd:.4f}")
        print(f"% saved             off -> on: "
              f"{off.pct_saved:.1f}% -> {on.pct_saved:.1f}%")
    else:
        summary = await run_traffic(
            args.base_url, n=args.requests, rps=args.rps, seed=args.seed)
        _print_summary(f"Traffic burst (profile={args.profile})", summary)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
