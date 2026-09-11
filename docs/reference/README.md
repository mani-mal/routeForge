# Reference

Field, endpoint, and metric lookup — not narrative how-to. **Verified against
HEAD 2026-09-11.** This is where the [operator](../guides/README.md) and
[application-developer](../guides/README.md) reading paths in `guides/` send
you second, once you need the exact shape of a field or endpoint rather than
the walkthrough.

| Doc | Contents |
|---|---|
| [`api-reference.md`](api-reference.md) | The OpenAI-compatible `/v1/chat/completions` wire contract — request/response fields, auth, examples. |
| [`per-app-config.md`](per-app-config.md) | Every per-application policy knob (`policy_mode`, `pii_detection`, `pii_entity_denylist`, `pii_score_threshold`, `pii_exempt_patterns`, `stream_mode`) and its effective-value rules. |
| [`routing-observability.md`](routing-observability.md) | `/metrics` Prometheus exporter, `routing.stage_signals`, and the `sp.candidates` / `sp.routing_policy_version` trace attributes. |

See [`../guides/README.md`](../guides/README.md) for the task-oriented docs
these support.
