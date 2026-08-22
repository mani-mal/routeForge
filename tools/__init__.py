"""Top-level `tools/` package: OFFLINE-only utilities that must never be
imported by the serving graph (`app/main.py`, `app/pipeline.py`,
`app/api/routes.py`, `app/routing/task_mode*.py`, ...). See
`tools/prompt_opt/` (Feature 3 / DSPy prompt-optimization, Task 3) for the
first tenant -- CLAUDE.md rule 4: RouteForge hosts no compute; this package
is where standalone jobs that orchestrate external compute live, structurally
separated so the request path can never accidentally pull one in.
"""
