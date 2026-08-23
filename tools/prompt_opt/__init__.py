"""Feature 3 (DSPy prompt-optimization), Task 3: the OFFLINE DSPy optimizer
for RouteForge's own managed prompts (currently just `task_mode_classifier`).

Entry point: `python -m tools.prompt_opt.optimize` (see `optimize.py::main`).
Never runs on the request path, never imports into `app/*`, and never
auto-activates a candidate it produces (human-gated promotion via
`app/config/lint.py::enforce`).
"""
