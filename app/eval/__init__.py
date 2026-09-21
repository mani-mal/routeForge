"""FeasibleRouteBench: an offline evaluation harness.

This package is operator-run tooling, never imported by the serving path.
Nothing under `app/` outside `app/eval/` may import `app.eval` (see
`.superpowers/sdd/2026-09-21-feasible-route-bench/global-constraints.md`).
"""
