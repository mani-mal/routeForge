# Comparisons

RouteForge weighed against external tools and reports. **Point-in-time
analysis:** claims about *RouteForge* were re-verified against HEAD during the
2026-09-10/11 sweep; claims about the *external* tools were not re-checked and
may have drifted since each doc's date.

| Doc | External tool / report | Decision | Still open? |
|---|---|---|---|
| [`nemo-switchyard-comparison.md`](nemo-switchyard-comparison.md) | NVIDIA NeMo Switchyard | Same problem (per-call model selection), different scope — Switchyard's Prefill Router is refused, several ideas (escalation, stage signals, strategy interface) are worth borrowing | **Yes — the "replace our router with Switchyard" question is deferred, not rejected.** That distinction is the reason this doc is kept. |
| [`switchyard-features-to-incorporate.md`](switchyard-features-to-incorporate.md) | NVIDIA NeMo Switchyard (companion report) | Concrete per-feature incorporation plan drawn from the internal Nemotron/Switchyard report | Action items only; no scope question left open |
| [`external-router-feature-analysis.md`](external-router-feature-analysis.md) | Plano, Devin Fusion, Multi-Model report | A small LLM is an advisory signal, not a routing authority — RouteForge's eligibility-first design stands | Closed |
| [`caching-cost-feature-fit.md`](caching-cost-feature-fit.md) | "Four caches in LLM serving" (KV/prefix/prompt/semantic) | KV caching stays Dynamo's job, not RouteForge's; the real gap is a cost-accounting blind spot for provider-side prompt caching | Open as a backlog item (see `roadmap/`), not as a scope question |
| [`ai-governance-feature-fit.md`](ai-governance-feature-fit.md) | "Agentic AI Governance" / "AI Control Tower" checklist | RouteForge already covers a majority of the checklist as the natural chokepoint; the rest is a ranked feature backlog | Positioning settled; backlog items open |

See [`../architecture/README.md`](../architecture/README.md) for how these
tie back into the current architecture.
