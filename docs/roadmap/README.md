# Roadmap

**Nothing in this directory is shipped.** These are analyses, proposals, and
research menus for possible future work — read `architecture/` for what
RouteForge does today.

| Doc | Status |
|---|---|
| [`priorities-1-3-status.md`](priorities-1-3-status.md) | **The exception to "nothing here is shipped":** the build status of the Priorities 1–3 program. Lists every shipped slice (P1a → P3c-1, with PRs) and what remains (P3c-2 scenarios, P3c-3 uncertainty, P3c-4 capacity, the review workflow, paused Priority 2 learning, open owner decisions). |
| [`rlhf-readiness.md`](rlhf-readiness.md) | **Largely shipped, despite the title.** The feedback/dataset substrate it designs (F1–F5) landed across `0.11.0` and F1 Phase D — `sp.candidates` / `sp.routing_policy_version` export, `POST /v1/feedback` + score names, and the offline `tools/recommend/` `tools/prompt_opt/` `tools/orchestrate/` tooling all exist today. Only *training itself* remains external and out of scope. |
| [`observability-metrics-target-state.md`](observability-metrics-target-state.md) | Genuinely target-state, with one correction: `cache_saved_usd` is **already** aggregated in `app/api/history.py` and returned by `GET /api/stats` and `GET /api/fleet` today — only a console dashboard tile is missing, not the underlying data. |
| [`ip-and-research-opportunities.md`](ip-and-research-opportunities.md) | Not a commitment — a ranked menu (IP-1…12, R1…R9) of IP-worthy features and research directions, with prior-art caveats. |
| [`ip-review-handoff.md`](ip-review-handoff.md) | Not a proposal itself — the reading-order brief for a reviewer evaluating `ip-and-research-opportunities.md`. |

See [`../backlog/bugs.md`](../backlog/bugs.md) for known defects (not future
work) and [`../comparisons/README.md`](../comparisons/README.md) for the
external-tool analyses some of these items grew out of.
