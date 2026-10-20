# Archive

**Historical — do not rely on these.** Each file below describes a past state
of RouteForge or a backlog that has since closed. They are kept for record,
not as current documentation.

| Doc | Archived | Why it's here | Superseded by |
|---|---|---|---|
| [`2026-07-25-architecture-review.md`](2026-07-25-architecture-review.md) | 2026-09-10 | Pre-pivot historical snapshot — predates the multi-tenant product pivot and the 2026-08-03 pure-router pivot; its "prototype" framing and `inference_backend: mock\|vllm\|dynamo\|none` axis no longer exist. | [`../architecture/`](../architecture/README.md) + [`../../CLAUDE.md`](../../CLAUDE.md) |
| [`ui-open-items.md`](ui-open-items.md) | 2026-09-10 | All 7 items closed (shipped in PR #35, `ui:0.17.0`). | [`../backlog/bugs.md`](../backlog/bugs.md) |
| [`eval-logging-backlog.md`](eval-logging-backlog.md) | 2026-09-10 | Marked BUILT 2026-07-29 — `capture_io: off\|redacted\|full` shipped; both backlog items done. | [`../reference/per-app-config.md`](../reference/per-app-config.md) |
| [`design-handoffs/RouteForge-Priorities-1-3-LLM-Handoff.md`](design-handoffs/RouteForge-Priorities-1-3-LLM-Handoff.md) | 2026-10-20 | The design hand-off for Priorities 1–3 (policy compiler, endpoint declarations, feasible set, signals, evaluation, simulator). Kept as the source the specs in `../superpowers/specs/` cite. | [`../roadmap/priorities-1-3-status.md`](../roadmap/priorities-1-3-status.md) |
| [`design-handoffs/RouteForge-Agentic-Trajectory-ArXiv-Extension.md`](design-handoffs/RouteForge-Agentic-Trajectory-ArXiv-Extension.md) | 2026-10-20 | Extension of the hand-off above for agent trajectories (envelope, switching cost, trajectory oracle). The policy envelope shipped (PR #102); the routing half is not built. | [`../research/trajectory-feasible-routing.md`](../research/trajectory-feasible-routing.md) |
| [`design-handoffs/policy-protection-redesign-handoff.md`](design-handoffs/policy-protection-redesign-handoff.md) | 2026-10-20 | The hand-off that split `policy_mode` into independent protection axes. Implemented as P1a (PR #52). | [`../architecture/multidimensional-policy-model.md`](../architecture/multidimensional-policy-model.md) + [`../reference/per-app-config.md`](../reference/per-app-config.md) |

Task 9 of the 2026-09-10 docs reclassification verified item-by-item that no
live (unshipped) item remained in either backlog file archived here.
