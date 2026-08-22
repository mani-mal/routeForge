"""tools.orchestrate — external-training orchestration hook, OUTSIDE the
serving graph.

RouteForge's side of a handshake with an EXTERNAL training service that runs
on the operator's own GPUs, in a separate repo/process. This package may
TRIGGER a distill/fine-tune job, TRACK its status, and SURFACE the trained
model for onboarding — it hosts NO training/inference compute, runs NO
training loop, and NEVER auto-onboards (CLAUDE.md rule 4: "orchestrated, not
hosted"; onboarding stays the operator-initiated, probe-gated
`POST /admin/infra/models` in `app/api/admin_routes.py`, which this package
never calls).

Nothing under `app/` may import `tools.orchestrate` (see
`tests/test_orchestrate_hook.py`'s subprocess isolation test). This package
importing `app.*` would be fine and expected (one-way, matching
`tools.recommend`), but it doesn't need to — the client here talks only to
the external orchestrator over HTTP.
"""
