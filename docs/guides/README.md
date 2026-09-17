# Guides

Task-oriented how-to documents for operators and application developers.
**Verified against HEAD 2026-09-11** — corrections made during the sweep are
called out below.

## Operator reading path

Read these in order to take RouteForge from nothing to a working deployment
with an onboarded model and a registered application:

1. [`getting-started.md`](getting-started.md) — install, configure, and boot
   RouteForge. *Corrected during the 2026-09-11 sweep: one starter recipe
   returned HTTP 422; fixed.*
2. [`onboarding-models.md`](onboarding-models.md) — point RouteForge at an
   OpenAI-compatible on-prem endpoint at runtime (the pool boots empty).
3. [`creating-a-sample-application.md`](creating-a-sample-application.md) —
   register an application and issue its token. *Corrected during the sweep.*
4. [`choosing-app-settings.md`](choosing-app-settings.md) — pick residency,
   `policy_mode`, streaming, and capture settings for an application.
5. [`admin-setup.md`](admin-setup.md) — the admin console walkthrough for
   onboarding an application end to end.
6. [`langfuse-per-app-projects.md`](langfuse-per-app-projects.md) — give an
   application its own Langfuse project (shared, manual, or auto-provisioned).

## Application-developer reading path

Read these in order to integrate a client application against the
OpenAI-compatible API:

1. [`integrating-an-app.md`](integrating-an-app.md) — call
   `/v1/chat/completions` from your application.
2. [`../reference/api-reference.md`](../reference/api-reference.md) — full
   endpoint and wire-field reference.
3. [`agentic-app-guide.md`](agentic-app-guide.md) — build an agentic
   application on top of RouteForge (your loop, RouteForge routes each call).
4. [`exporting-eval-datasets.md`](exporting-eval-datasets.md) — pull eval /
   RLHF-ready datasets out of Langfuse traces.

## Console pages

- [`ui-guide.md`](ui-guide.md) — the RouteForge console, region by region.
  *Corrected during the sweep: a stale cost-baseline formula and stale router
  strings.*
- [`reading-the-dashboard-and-trace.md`](reading-the-dashboard-and-trace.md) —
  reading a dashboard and a technical trace together, support-ticket style.
  *Corrected during the sweep: the redaction banner note was extended.*

## Other

- [`faq.md`](faq.md) — operator- and customer-facing frequently asked questions.
