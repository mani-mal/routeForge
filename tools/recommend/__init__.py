"""tools.recommend — offline, human-gated trace-mining recommend-loop.

Mines RouteForge's own local request metering (`UsageRecord`) and emits a
`RecommendationReport` artifact for an operator to review and hand-apply.
NEVER mutates config, NEVER calls a model, NEVER triggers anything — this is
the "CAN: mine -> recommend, offline + human-gated" half of CLAUDE.md rule 4.
Triggering external training/distillation jobs is a *different* module
(Task 2), not this one.
"""
