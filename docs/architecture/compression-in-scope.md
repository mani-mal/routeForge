# Why context compression (Headroom, step 5) lives in RouteForge, not the calling app

**Status:** decision doc, 2026-08-20. Answers the recurring scope question: *"We
decided agent-harness self-improvement and RLHF are out of scope because the calling
application owns its own harness. By the same logic, shouldn't Headroom compression live
in the application too?"* **Decision: no — compression stays in RouteForge.** Sibling
reads: [`self-improving-agent-feature-fit.md`](./self-improving-agent-feature-fit.md)
(the control-plane-vs-compute boundary this rests on), the `headroom-compression` skill,
CLAUDE.md rules #6/#7.

---

## The question, stated fairly

RLHF and agent self-improvement are out of scope. The reasoning is real: they change
model weights or rewrite the harness's own logic — that's *compute* and a *different
plane* — and the app that calls RouteForge owns its own agent loop, produces its own
history, and could manage its own context. So the instinct is: compression reduces the
tokens the app sends; the app could do that itself; move Headroom out.

The instinct has a genuine kernel of truth (see the last section). But the analogy does
not carry, for four reasons — one of which is load-bearing on privacy.

## First, a framing correction

Headroom does **not** compress logs or telemetry. It compresses the **request context** —
tool outputs and conversation history — fed into each LLM call, on the serving hot path
between the gate (step 2) and the router (step 6). If it compressed observability logs,
the move-it-to-the-app argument would be much stronger: a log sink can live anywhere.
It doesn't. It transforms the live routed request.

## Why the RLHF / harness analogy does not transfer

**1. Different plane, and not compute.** RLHF and self-editing harnesses are out because
they belong to the **Improvement Plane** and they **host or change compute** — model
weights, training loops, runtime code rewrites. That is the governing line from the
boundary doc: *control-plane vs compute; RouteForge orchestrates and consumes, it hosts
no heavy compute.* Compression is none of that. It is **step 5 of the canonical ten
Serving-Plane steps** — a per-call transformation that changes only the token count for
*this* request, hosts no compute, and touches no model. It sits on the same axis as the
gate and the router, not on the improvement axis. It is on the RouteForge side of the
line by construction.

**2. Boundary enforcement — the load-bearing reason it cannot move to the app.** CCR
caches *originals* locally (rule #6): a second copy of sensitive data, with an enforced
TTL, that must never leave the residency boundary. RouteForge is the component that
*enforces* that boundary — the same way it enforces the gate. If the app compressed
before calling RouteForge, either the CCR cache of originals would live **outside**
RouteForge's residency/TTL enforcement, or the app would have to re-implement the entire
residency/TTL/redaction machinery itself. And rule #7 (compression runs *after* the gate,
on sanitized content only) is only satisfiable if RouteForge owns the ordering.
**You cannot move a boundary-enforcing component outside the boundary it enforces.** This
is the same argument that keeps the gate in RouteForge — and the same argument the
boundary doc uses to keep the mem0 memory store in RouteForge (§2.C: CCR-class residency,
same family as Headroom).

**3. It is a separately-measured cost lever.** The definition of done requires the cost
dashboard to split **routing savings** from **compression savings** and track them
separately. If the app compresses upstream, RouteForge sees only pre-shrunk tokens and
that attribution becomes impossible — the operator can no longer tell which lever is
working. Compression must run where the router prices tokens (post-compression, step 6)
for the two-levers story to be measurable at all.

**4. It is one of the ten seams.** "The product's value is showing the seams." Step 5 is a
swappable seam with a narrow interface, default-OFF, toggled to measure before/after. That
demonstrability *is* a feature; it does not exist if compression happens off-platform.

## Where the instinct is right — and why it is still fine

On the **direct / pass-through path**, the calling app owns its own conversation history,
and a cost-conscious app *could* pre-summarize before calling RouteForge. RouteForge does
not *need* compression to route — routing works on whatever tokens arrive. That overlap is
real. It stays fine because Headroom is **default-OFF and conditional** (opt-in per
operator), so it is never forced on an app that manages its own context.

On the **multi-step path it is unambiguous**: RouteForge's own NAT agent runtime produces
the tool outputs and history, and every hop re-enters compression (5) and the router (6).
There is no app-owned harness there to hand the job to — the harness *is* RouteForge.

## Net

RLHF and self-improvement are out because they are the Improvement Plane changing a model
(or rewriting the harness) on external compute. Compression is a per-call, in-boundary,
Serving-Plane seam that (1) hosts no compute, (2) can only be enforced inside the
residency boundary RouteForge owns, (3) must be measured where the router prices tokens,
and (4) is one of the ten seams the product exists to show. Keep it in RouteForge.
