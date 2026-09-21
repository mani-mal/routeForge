"""FeasibleRouteBench: an offline evaluation harness.

This package is operator-run tooling, never imported by the serving path.
Nothing under `app/` outside `app/eval/` may import `app.eval` (see
`docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md`).
"""
