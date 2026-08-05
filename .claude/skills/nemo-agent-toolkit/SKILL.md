---
name: nemo-agent-toolkit
description: Use when wiring the NeMo Agent Toolkit (nvidia-nat) into the Serving Plane - agent runtime for multi-step workflows, tool sandboxing, and Langfuse telemetry export via OpenTelemetry. Trigger on mentions of NAT, NeMo Agent Toolkit, agent runtime, workflow orchestrator, Langfuse, OpenTelemetry, or tracing.
---

# NeMo Agent Toolkit (NAT) Integration

NAT v1.8 is an open-source Python library for building, running, evaluating and
observing agent workflows. It wraps agents from other frameworks rather than
replacing them, and needs **no GPU** unless models run locally — which is why a local
bring-up runs on a laptop (routeForge deploys no inference and boots with an empty
on-prem pool).

Here it does two jobs: **agent runtime** (step 4 multi-step path) and **telemetry
export** to Langfuse. Ignore its evaluation/optimization/fine-tuning features —
those are Improvement Plane, out of scope.

## Langfuse export

NAT observability is OpenTelemetry-based and lists Langfuse as a supported backend
alongside Phoenix, Weave, LangSmith and Arize. Configure Langfuse directly; do not
build a custom exporter.

```yaml
general:
  telemetry:
    tracing:
      langfuse:
        _type: langfuse
        endpoint: ${LANGFUSE_HOST}
        public_key: ${LANGFUSE_PUBLIC_KEY}
        secret_key: ${LANGFUSE_SECRET_KEY}
```

Verify the exact key names against the installed version's docs before assuming this
shape — NAT config schemas move between minor versions. If it drifts, fall back to a
generic OTLP exporter pointed at Langfuse's OTel endpoint.

## Redaction before export — mandatory

NAT captures full trajectories: model calls, tool calls, tokens, latency, and
intermediate reasoning. All of it can carry PII that the input gate never saw.

NAT provides **telemetry redaction** in its safety/auth layer — use it, and add a
span processor as defence in depth:

```python
class RedactingSpanProcessor(SpanProcessor):
    def on_end(self, span):
        for key in ("input.value", "output.value", "tool.output"):
            if key in span.attributes:
                span.set_attribute(key, redact(span.attributes[key]))
```

Assert in tests that a known SSN never appears in exported spans. This is a
correctness test, not a nice-to-have.

## Agent runtime

Use NAT's built-in **tool-calling** or **ReAct** agent pattern; don't write a custom
loop. Configure via YAML.

The critical wiring: **every model call the agent makes must go through our router**,
not direct to a provider. Register the gateway as NAT's model endpoint so each hop
re-enters step 5. If NAT calls a provider directly, the per-call routing story dies
and the cost numbers become fiction.

Constrain the sandbox: `max_steps` (default 6), `max_spend_usd`, per-tool timeouts,
and scoped credentials. Demo/sample tools: a calculator, a fake document search, a fake
CRM lookup — enough to make hops legible, no real integrations.

## Config-driven

NAT assembles agents, models, tools and middleware from YAML. Keep the workflow in
`config/nat_workflow.yaml` so the demo can be re-shaped without code changes.

## Installation

```bash
uv add nvidia-nat
```

Python 3.11–3.13. Pin the version — reproducible customer deployments, not a moving target.

## Tests

- Multi-step task → N hops, each producing a router DecisionRecord
- Trace appears in Langfuse with full trajectory
- Known SSN absent from every exported span
- `max_steps` and `max_spend_usd` both enforced
