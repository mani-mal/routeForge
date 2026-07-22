"""NAT agent runtime (design §10.3, Phase 5).

`run`/`AgentResult` are re-exported from `app.agent.runtime` for callers
(the pipeline). `app.agent.runtime` imports `nat` lazily, inside `run`, so
importing `app.agent` itself never requires the (optional, extras-gated)
`nat` package to be installed.
"""
from app.agent.runtime import AgentResult, run

__all__ = ["AgentResult", "run"]
