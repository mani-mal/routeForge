"""Offline operator tooling, OUTSIDE the serving graph.

OFFLINE-only utilities that must never be imported by the serving graph
(`app/main.py`, `app/pipeline.py`, `app/api/routes.py`,
`app/routing/task_mode*.py`, ...). Tenants: `tools/prompt_opt/` (DSPy
prompt-optimization) and `tools/recommend/` + `tools/orchestrate/`
(trace-mining recommend-loop + external-training orchestration). Per
CLAUDE.md rule 4 (RouteForge hosts no compute), these standalone jobs
orchestrate external compute and are human-gated; the request path can
never accidentally pull one in. `tools.*` importing `app.*` is fine and
expected (one-way): the jobs read the same config/DB the server uses,
they just never run inside it.
"""
