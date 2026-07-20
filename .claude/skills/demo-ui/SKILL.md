---
name: demo-ui
description: Use when building the demo UI - live request flow visualizer, decision inspector, cost dashboard, and control panel. Trigger on mentions of UI, frontend, dashboard, visualizer, React, or demo screen.
---

# Demo UI

**The UI is the product in a demo.** The answer text is almost incidental; what sells
is showing the *decision* — which branch, why, what it cost, what was excluded.

React + Vite + Tailwind + Recharts. Single page, three panels.

## Panel 1 — Live flow visualizer (the centrepiece)

The ten steps as nodes. A request animates through them in real time, lighting each
node as it completes and taking the branch it actually took.

Non-negotiable details:

- **Stream over WebSocket/SSE.** Polling makes it feel dead. Emit a `DecisionRecord`
  per step as it happens.
- **Show excluded providers greyed out with the reason.** For a PII request, the
  cloud tiers visibly grey out. This single visual carries the privacy story.
- **Multi-step tasks loop visibly.** Each agent hop re-enters the router node with an
  incrementing hop counter. Show it looping, or the per-call routing point is lost.

## Panel 2 — Decision inspector

Click any request → full trace: step-by-step, chosen vs. rejected candidates with
reasons, entity types detected (never values), per-hop cost, cumulative cost,
latency, and a deep link to the Langfuse trace.

The rejected-candidates list with reasons is what makes it legible on a projector.

## Compression view (step 5)

When Headroom is on, show per request: **tokens before → after** with the reduction
percentage, which compressor fired, and CCR store/retrieve counts. When it's bypassed,
show the reason ("below min tokens", "high risk") — customers ask why.

The strongest moment: run the same multi-step task with compression off, then on. Same
answer, dramatically fewer tokens. Make that A/B one click.

## Panel 3 — Cost dashboard

Headline: **actual spend vs. all-frontier baseline**, with the percentage saved.
**Split the saving into two bars — routing vs. compression.** Two independent levers;
if you merge them the customer can't tell which is working.
Underneath: tier distribution (pie), cost over time (line), p50/p95 latency, and
count of requests kept on-prem by policy.

Fed by the traffic generator, so numbers move while the room watches.

## Control panel

Live controls that visibly change behaviour:
- Cost-weight slider → watch traffic shift tiers. Best interactive moment in the demo.
- **Headroom on/off toggle** → re-run the same request, watch token counts collapse.
- Toggle providers on/off → watch the pool shrink.
- Fire single scripted requests (clean / PII / multi-step / high-risk).
- Start/stop traffic burst.

## Design

Dark, high-contrast, legible from the back of a room. Large type; assume a projector.
Colour-code by tier consistently — cheap green, mid blue, strong amber, frontier red —
and reuse those colours everywhere so the pie chart reads instantly.

Prefer clarity over animation polish. If a choice is between "looks impressive" and
"a customer understands what just happened", pick the second.

## Rules

- Never render raw PII, even if the API returns it. Entity types only.
- Never render CCR cache contents — show counts and sizes, never the stored originals.
- Handle empty state — a fresh demo starts with zero requests.
- No client-side secrets; UI talks only to our API.
