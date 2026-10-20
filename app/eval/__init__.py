"""FeasibleRouteBench: an offline evaluation harness.

This package is operator-run tooling, never imported by the serving path.
Nothing under `app/` outside `app/eval/` may import `app.eval` (see
the FeasibleRouteBench design spec).
"""
