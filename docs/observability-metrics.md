# RouteForge observability metrics catalog

Reference catalog of the metrics RouteForge should surface, the open-source tools that
produce them, and corrected definitions. Captured from a collected research note.
Related: ties to `docs/eval-logging-backlog.md` and `docs/rlhf-readiness.md`
(feedback/score metrics).

**Tool layers at a glance:**
- **OpenTelemetry / OpenInference** — common trace format.
- **Langfuse** — LLM traces, tokens, costs, quality scores and analysis.
- **NeMo Agent Toolkit Eval & Profiler** — workflow evaluation, agent trajectory, offline regression.
- **Prometheus & Grafana** — real-time serving, routing, GPU, infrastructure monitoring.
- **vLLM, Dynamo & DCGM Exporter** — inference and GPU-native metrics.

## Corrected definitions (watch these)

| Metric | Recommended definition | Reason |
|---|---|---|
| Prompt tokens | System prompt + conversation history + retrieved context + tool schemas/results + user input | The user's question alone is not the complete prompt |
| Total tokens | Track both LLM-call total and workflow total | An agent may make many LLM calls |
| Context utilization | (Input tokens + actual output tokens) ÷ model context window × 100, per call | Don't use total workflow tokens — they weren't all in one context window |
| Reserved context utilization | (Input tokens + configured max output tokens) ÷ context window × 100 | Predicts context-overflow risk before generation |
| TPS | Output tokens ÷ (last token ts − first token ts) | Excludes TTFT; measures decode throughput |
| TPOT | (last token − first token) ÷ (output tokens − 1) | Better representation of streaming smoothness |
| Reasoning ratio | Reported reasoning tokens ÷ final answer tokens | Only when the model/provider reports reasoning-token counts |

> Reasoning tokens should not be added to total tokens again if the provider already
> includes them within output-token usage.

## Recommended open-source measurement stack

| Layer | Tool | RouteForge responsibility |
|---|---|---|
| Telemetry standard | OpenTelemetry GenAI conventions | Standard spans, token attributes, model info, trace propagation |
| Agent/tool tracing | OpenInference | Conventions for agents, retrieval, tool calls |
| LLM observability | Langfuse | Per-request traces, tokens, cost, scores, feedback, custom dashboards |
| Workflow profiling | NeMo Agent Toolkit | Agent steps, LLM/tool timing, token efficiency, bottlenecks |
| Offline evaluation | NAT Eval + Ragas | Accuracy, groundedness, relevance, trajectory; standardized latency/token files |
| Time-series monitoring | Prometheus | Counters, histograms, alerts, SLOs |
| Operations dashboard | Grafana | Routing, inference, GPU, infrastructure dashboards |
| vLLM inference | vLLM `/metrics` | TTFT, queue time, running/waiting requests, KV cache, throughput, outcomes |
| Distributed inference | NVIDIA Dynamo `/metrics` | Frontend, router, worker, KV transfer, backend metrics (incl. backend vLLM/TRT-LLM) |
| Semantic routing | vLLM Semantic Router | Model selection, intent, PII/jailbreak decisions, routing latency (Prom/Grafana/Jaeger) |
| GPU telemetry | NVIDIA DCGM Exporter | GPU utilization, HBM, power, temperature, hardware errors |
| Context compression | Headroom | Tokens before/after, savings, CCR retrieval |
| RAG quality | Ragas | Groundedness, answer accuracy, context quality |
| Broader quality testing | DeepEval | Task completion, hallucination, relevancy, custom tests |
| Security testing | Garak | Prompt injection, leakage, jailbreak, toxicity, adversarial |
| Privacy | Presidio | PII detection, redaction, anonymization |
| Energy | Kepler | K8s pod/container/node energy consumption |

## 1. Context and token metrics

| Priority | Metric | Definition | Diagnostic value | Primary tool |
|---|---|---|---|---|
| Core | Input tokens per call | Complete prompt tokens per LLM invocation | Prompt cost & prefill workload | Langfuse, OTel, NAT |
| Core | Output tokens per call | Tokens returned per invocation | Decode cost & verbosity | Langfuse, OTel, NAT |
| Core | Total workflow tokens | Sum of input+output across every call | True agent-level consumption | NAT Profiler, Langfuse |
| Core | Context utilization | (Input + actual output) ÷ context window | Detects context pressure | Custom RouteForge metric |
| Production | Reserved context utilization | (Input + max output) ÷ context window | Predicts overflow before execution | RouteForge |
| Production | Reasoning tokens | Provider-reported internal reasoning-token count | Measures reasoning expense | OTel/Langfuse when exposed |
| Production | Cached input tokens | Prompt tokens served from provider/prefix cache | Prompt-cache savings | Provider, vLLM, Langfuse |
| Production | Prompt composition | Token share from system, history, RAG, tools, user | What's consuming context | Custom OTel span attributes |
| Core | Truncation rate | Requests with truncated input or `finish_reason=length` | Context-window failures | vLLM, Langfuse |
| Core | Headroom compression rate | (Tokens before − after) ÷ before | Context savings | Headroom |
| Production | Compression quality delta | Quality with Headroom − without | Detects information loss | NAT Eval |
| Production | CCR retrieval rate | Requests where compressed source was retrieved | Compression too aggressive? | Headroom |
| Production | Semantic-cache hit rate | Cache `hits` ÷ lookups (`app/cache/store.py`; `blocked_cloud_entries` is the residency honesty counter) | Repeat-query savings, no model call | RouteForge semantic cache |
| Production | Cache cost savings | `sp.cost_cache_saved_usd` — model cost avoided by a cache hit (`app/obs/export.py`) | Third savings lever, tracked separately | RouteForge |
| Production | Memory recall rate | Memory `retrievals` (and `retrievals_to_cloud`, CCR-class honesty counter) ÷ requests (`app/memory/store.py`) | Persistent-memory reuse; residency honesty | RouteForge persistent memory |
| Core | Token Intelligence Density | Σ quality score ÷ Σ workflow tokens × 1,000 | Quality per 1,000 tokens | Custom NAT evaluator |
| Production | Reasoning-to-answer ratio | Reasoning tokens ÷ user-visible answer tokens | Detects overthinking | Custom evaluator |

## 2. Latency, speed and traffic metrics

| Priority | Metric | Definition | Diagnostic value | Primary tool |
|---|---|---|---|---|
| Core | End-to-end latency | Request received → final response/action | User experience | OTel, Langfuse |
| Core | TTFT | Request received → first output token | Queue & prefill performance | vLLM, Dynamo |
| Core | TPOT | Average time between generated tokens | Streaming smoothness | vLLM |
| Core | Output TPS | Output tokens ÷ decode duration | Raw decode speed | vLLM, Dynamo |
| Production | Queue latency | Time waiting before inference starts | Capacity shortage/overload | vLLM, Dynamo |
| Production | Prefill latency | Time processing input context | Long-prompt performance | vLLM/Dynamo traces |
| Core | Requests per second | Completed requests/sec | System throughput | Prometheus |
| Core | Tokens per second (aggregate) | Total generated tokens across workers/sec | Cluster throughput | Prometheus, vLLM |
| Core | Concurrent/in-flight requests | Active requests by model & worker | Capacity & saturation | Dynamo |
| Production | Task-mode router latency | Time deciding direct vs agent workflow | Router overhead | NAT + OTel |
| Production | Model-router latency | Semantic classification + backend-selection time | Routing overhead | Semantic Router, Dynamo |
| Production | Privacy/guardrail latency | Presidio + Guardrails processing time | Security overhead | Custom Prometheus histogram |
| Production | Headroom latency | Compression + CCR processing time | Whether token savings justify delay | Headroom + OTel |
| Production | Tool/retrieval latency | Latency by tool/API/DB/vector store | Non-LLM bottlenecks | NAT Profiler |
| Production | Timeout/cancellation rate | Timed-out or cancelled ÷ total | Slow or abandoned work | Dynamo, Prometheus |

> Always display latency as p50, p95, p99 — not only averages.

## 3. Quality, RAG and agent metrics

| Priority | Metric | Definition | Diagnostic value | Primary tool |
|---|---|---|---|---|
| Core | Task success rate | Successful ÷ attempted tasks | Primary outcome | NAT Eval / custom |
| Core | Quality score | Domain-specific score 0–1 | Supports routing & density comparison | NAT Eval |
| Production | Answer accuracy | Comparison with expected answer | Factual/task correctness | NAT + Ragas |
| Production | Response groundedness | Claims supported by retrieved evidence | Hallucination diagnosis | Ragas |
| Production | Answer relevance | Whether response addresses the request | Detects irrelevant verbosity | Ragas/DeepEval |
| Production | Context precision | Relevant retrieved chunks ÷ retrieved | Retrieval noise | Ragas |
| Production | Context recall | Required evidence successfully retrieved | Retrieval omissions | Ragas |
| Production | Agent trajectory accuracy | Correctness of tool & planning sequence | Right answer reached incorrectly | NAT trajectory evaluator |
| Core | Tool execution success | Successful ÷ attempted tool calls | Tool integration health | NAT Profiler |
| Production | Tool argument accuracy | Correct tool & valid arguments selected | Agent planning errors | NAT custom evaluator |
| Core | Structured-output validity | Responses satisfying required JSON/schema | Downstream reliability | JSON Schema / custom |
| Core | User acceptance | Thumbs-up, accepted output, or no regeneration | Production usefulness signal | Langfuse |
| Production | Human intervention rate | Tasks needing approval/correction/takeover | Autonomy & operational burden | Langfuse/custom |
| Production | Regression delta | Candidate score − production baseline | Deployment-gate decision | NAT Eval |
| Advanced | Judge agreement | Agreement between human, deterministic & LLM judges | Detects unreliable evaluation | NAT/custom |

## 4. Routing and financial metrics

| Priority | Metric | Definition | Diagnostic value | Primary tool |
|---|---|---|---|---|
| Core | Task-mode distribution | Direct vs multi-step workflow % | Workload characterization | NAT Router Agent |
| Production | Task-mode accuracy | Correct branch vs labeled examples | Unnecessary agents / missed tool use | NAT Eval |
| Core | Model selection distribution | Requests & tokens per model/provider | Cost & load visibility | Semantic Router, Langfuse |
| Core | Route success rate | Successful tasks by selected route/model | Router effectiveness | Langfuse |
| Advanced | Routing regret | Best allowed route score − selected route score | Missed routing opportunities | Shadow evaluation |
| Core | Escalation rate | Cheap-model requests promoted to stronger | Router confidence & weak-model limits | RouteForge |
| Production | Fallback success rate | Successful ÷ fallback attempts | Fallback resilience | RouteForge, Langfuse |
| Production | Retry rate | Extra LLM/tool attempts per workflow | Hidden cost & reliability | NAT Profiler |
| Core | Cost per workflow | All model, tool & infra costs | Actual RouteForge cost | Langfuse + custom |
| Core | Cost per successful task | Total cost ÷ successful tasks | Best financial efficiency measure | Custom Langfuse metric |
| Core | Cost Intelligence Density | Σ quality ÷ Σ cost | Quality per dollar | Custom NAT evaluator |
| Core | Savings vs baseline | (Baseline − RouteForge cost) ÷ baseline at equivalent quality | Demonstrates routing value | NAT experiments |
| Production | Cloud vs local cost | Provider cost vs allocated self-hosted cost | Economical execution location | Langfuse + Prometheus |
| Production | Constraint compliance | Routes meeting quality/latency/residency/safety | Prevents cheap-but-invalid routing | RouteForge policy engine |

## 5. Inference, GPU and capacity metrics

| Priority | Metric | Definition | Diagnostic value | Primary tool |
|---|---|---|---|---|
| Core | Running requests | Requests actively processed per worker | Worker load | vLLM, Dynamo |
| Core | Waiting requests | Requests queued per worker | Immediate capacity warning | vLLM, Dynamo |
| Production | Continuous batch size | Active sequences/tokens per batch | Batching efficiency | vLLM |
| Core | KV-cache utilization | Used ÷ available KV blocks | Context capacity & preemption risk | vLLM, Dynamo |
| Core | Prefix-cache hit rate | Reused ÷ eligible prefix tokens | Prompt-cache effectiveness | vLLM |
| Production | Request preemption rate | Requests evicted/recomputed due to cache pressure | Insufficient KV capacity | vLLM |
| Core | GPU utilization | % GPU compute active | Underutilization or saturation | DCGM Exporter |
| Core | HBM utilization | GPU memory used/available | OOM & model-capacity planning | DCGM Exporter |
| Production | GPU power & temperature | Watts & GPU temp | Throttling & infra health | DCGM Exporter |
| Production | Model load time | Time to load/initialize a model | Cold-start impact | Dynamo |
| Production | OOM/worker crash rate | GPU OOMs & worker restarts | Capacity/configuration errors | Kubernetes, Prometheus |
| Core | Tokens per GPU-second | Generated tokens ÷ allocated GPU time | Hardware efficiency | vLLM + DCGM |
| Production | Successful tasks per GPU-hour | Successful outcomes ÷ GPU hours | Workload-level infra value | Custom Prometheus metric |
| Advanced | Energy per successful task | Joules ÷ successful tasks | Energy efficiency | Kepler + DCGM |
| Advanced | KV-transfer latency/failure | Prefill→decode cache-transfer results | Disaggregated inference diagnostics | Dynamo/NIXL |

## 6. Reliability, privacy and security metrics

| Priority | Metric | Definition | Diagnostic value | Primary tool |
|---|---|---|---|---|
| Core | Request error rate | Failed ÷ total, by error type | General reliability | Prometheus, Dynamo |
| Core | Availability/SLO | Successful service time vs target | Production health | Prometheus |
| Production | Circuit-breaker activations | Provider/model circuits opened | Provider instability | RouteForge |
| Core | Guardrail block rate | Inputs/outputs blocked ÷ requests | Safety activity | NeMo Guardrails |
| Production | Guardrail false-positive rate | Safe requests incorrectly blocked | User-impact measurement | Labeled NAT evaluation |
| Production | Guardrail false-negative rate | Unsafe requests incorrectly allowed | Safety effectiveness | Garak + labeled tests |
| Core | PII detection rate | Requests containing PII by type | Sensitive-data volume | Presidio |
| Core | PII redaction rate | Sensitive entities removed before external calls/logging | Privacy control operation | Presidio |
| Core | PII leakage rate | Sensitive values reaching forbidden output/provider/trace | **Critical; target zero** | Presidio + tests |
| Production | Prompt-injection attack success | Successful ÷ adversarial attempts | Injection-control effectiveness | Garak |
| Production | Jailbreak success rate | Policy-bypassing responses ÷ attempts | Guardrail effectiveness | Garak |
| Production | Unauthorized tool attempts | Tool calls rejected by permissions/policy | Agent abuse or bad planning | NAT + policy engine |
| Core | Data-residency violations | Requests routed outside permitted boundary | **Critical compliance; target zero** | RouteForge policy engine |
| Core | Trace-redaction coverage | Sensitive traces redacted ÷ sensitive traces | Prevents telemetry leakage | Presidio + OTel Collector |
| Production | Security-control latency | Added latency from privacy/safety checks | Security/performance tradeoff | Prometheus |

## Recommended initial dashboard

Don't put all metrics on one screen. First dashboard = these core indicators:

| Area | Primary tiles |
|---|---|
| Outcome | Task success, quality score, user acceptance |
| Efficiency | Workflow tokens, Token Intelligence Density, cost per successful task |
| Speed | End-to-end p95, TTFT p95, TPS, queue latency |
| Routing | Route distribution, route success, escalation & fallback rates |
| Context | Context utilization, truncation, Headroom compression |
| Capacity | Waiting requests, KV-cache utilization, GPU utilization, HBM |
| Reliability | Error rate, timeout rate, availability |
| Security | PII leakage, residency violations, guardrail blocks |

**Recommendation:** use **Langfuse** as the RouteForge diagnostic/quality interface and
**Grafana** as the real-time operations interface. NAT Eval supplies offline quality &
trajectory results to Langfuse; Prometheus collects vLLM, Dynamo, Semantic Router, DCGM,
Headroom and RouteForge custom metrics for Grafana.
