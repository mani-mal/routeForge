# IP review — hand-off brief

**Purpose:** everything a reviewer new to RouteForge needs in order to recommend what
intellectual property is worth implementing, in the order they should read it.
**Created:** 2026-09-05. **Audience:** a fresh reviewer (human or model) with no prior
context on this repo.

> **Read §1 before anything else.** The most expensive mistake available here is
> re-deriving analysis that already exists, and the second most expensive is
> recommending work that is out of scope by construction. §1 and §2 prevent both.

---

## 1. Your task is not "find IP" — it is "validate, challenge, extend"

**A ranked IP analysis already exists:**
[`ip-and-research-opportunities.md`](./ip-and-research-opportunities.md) (2026-08-31, 567
lines). It contains:

- **A thesis** (§0) — that RouteForge's one structural asset is treating the set of
  eligible models as a *per-request quantity derived from policy before any optimization
  runs*, where the literature and the market both assume a fixed pool.
- **A prior-art table** (§1) — eight patents/applications, with what each one forecloses.
- **IP-1 … IP-12** (§2) — ranked in three tiers, each with mechanism, what already exists
  in the repo, what to build, why it is novel against that prior art, and the risk.
- **R1 … R9** (§3) — paper ideas ordered by expected acceptance × strategic value.
- **A sequencing section** (§4) — including three correctness bugs that block the rest.
- **Stated limitations** (§5) — read these; they are your actual assignment (see §5 below).

So the useful contributions are, in rough order of value:

1. **Close the gaps §5 admits to** — principally the incomplete patent search (§5 below).
2. **Falsify a claim.** Every "nobody does X" in that document is bounded by one pass of
   targeted search. Finding prior art that kills IP-1 or IP-4 is worth more than adding
   IP-13.
3. **Assess what the document could not have seen** — it predates `policy_mode`; see §6.
4. **Re-rank against effort.** The ranking is by novelty and strategic value; a reviewer
   who re-ranks by implementation cost against the current codebase adds something real.
5. **Only then**, propose additions.

State plainly which of these you are doing. A reviewer who returns a fresh ranked list
that silently overlaps 80% with the existing one has produced nothing.

---

## 2. Hard scope constraints — recommendations violating these are unusable

From [`../../CLAUDE.md`](../../CLAUDE.md), which carries nine non-negotiable rules
(1, 2, 2a, 3–8). The two that most often invalidate an IP recommendation:

- **RouteForge hosts no compute (rule 4).** It is a *pure router*: it points at
  OpenAI-compatible endpoints that operators onboard, and deploys, scales or
  load-balances **no** inference. The Improvement Plane (training, evaluation gates,
  promotion) is **orchestrated, not hosted** — RouteForge may capture training substrate,
  trigger an external job, and onboard the result, but it runs no training loop and has
  no in-process promotion path. **Any proposal requiring RouteForge to host inference or
  training compute is out of scope, and the boundary is deliberate — it is the product.**
- **`onprem_only` is absolute (rule 3), and residency beats the risk floor (rule 8).**
  Cloud must be unreachable *by construction* — filtered from the candidate pool before
  the router scores anything, never a post-hoc check. Several IP claims depend on this
  being structural rather than procedural; a proposal that weakens it to a check
  invalidates the asset it is trying to exploit.

Also load-bearing: nothing external sees the raw request before the gate (rule 1);
telemetry is redacted on egress (rule 2); placement (`residency`) and protection
(`policy_mode`) are separate axes and neither may be inferred from the other (rule 2a);
the gate fails closed (rule 5).

---

## 3. Reading order

### Tier 0 — determines whether your output is usable at all

| # | Document | Why |
|---|---|---|
| 1 | [`../../CLAUDE.md`](../../CLAUDE.md) | The constitution: the nine rules and the two scope boundaries. §2 above is a summary, not a substitute. |
| 2 | [`ip-and-research-opportunities.md`](./ip-and-research-opportunities.md) | The existing ranked menu and the foreclosed prior art. Your baseline. |
| 3 | [`../guides/getting-started.md`](../guides/getting-started.md) | What the product is and every policy option, concept-level. Fastest route to competence. |
| 4 | [`logical-and-request-flow.md`](../architecture/logical-and-request-flow.md) then [`sample-request-flows.md`](../architecture/sample-request-flows.md) | The ten Serving-Plane steps, then three worked request traces through them. |

### Tier 1 — the mechanisms the IP claims rest on

Every IP-x entry asserts "already in the repo." You cannot assess novelty or
implementation cost without the mechanism:

| Document | Claims it underpins |
|---|---|
| [`../reference/per-app-config.md`](../reference/per-app-config.md) | IP-2 (redaction-cost accounting), IP-6 (unified policy context). The exemption levers and every write-time clamp. |
| [`routing-tier-selection.md`](../architecture/routing-tier-selection.md) | IP-2, R2. Tier is `min(1.0, tokens_after_compression / 2000)` — *literally a function of token count*, which is why redaction demonstrably moves price. |
| [`../reference/routing-observability.md`](../reference/routing-observability.md) | IP-4 (decision ledger), IP-7 (TOCTOU revocation), IP-11 (raise-only signals). `sp.candidates`, `routing_policy_version`, `app_policy_version`. |
| [`semantic-cache-residency-spec.md`](../architecture/specs/semantic-cache-residency-spec.md) | IP-10, R6. The partition key and dual write/read enforcement. |
| [`persistent-memory-residency-spec.md`](../architecture/specs/persistent-memory-residency-spec.md) | CCR-class residency rules (rule 6). |
| [`../guides/onboarding-models.md`](../guides/onboarding-models.md) | IP-8 (cold-start routing), IP-9 (derived rejection). The pool boots **empty**; onboarding is probe-gated. |
| [`inference-layer-boundary.md`](../architecture/decisions/inference-layer-boundary.md) | Keeps proposals inside rule 4. Answers "does RouteForge do llm-d / DRA / Gateway API / prefill-decode?" |

### Tier 2 — scans already run; read so you don't repeat them

| Document | What it already settles |
|---|---|
| [`external-router-feature-analysis.md`](../comparisons/external-router-feature-analysis.md) | Plano, Devin Fusion, multi-model report. Includes the decision that a small LLM is an *advisory signal, never an authority*. |
| [`caching-cost-feature-fit.md`](../comparisons/caching-cost-feature-fit.md) | The four caches (KV/prefix/prompt/semantic); KV is delegated to Dynamo, confirmed. Contains the F1 cached-token cost gap that IP-5 and R5 depend on. |
| [`ai-governance-feature-fit.md`](../comparisons/ai-governance-feature-fit.md) | The governance backlog (G2′ scoped per-tool identity, G3′ immutable audit, G4′ per-run identity) that IP-4 and IP-6 reference directly. |
| [`nemo-switchyard-comparison.md`](../comparisons/nemo-switchyard-comparison.md) + [`switchyard-features-to-incorporate.md`](../comparisons/switchyard-features-to-incorporate.md) | NVIDIA overlap: what to borrow, what to refuse on scope grounds, and the deferred replace-decision. |
| [`self-improving-agent-feature-fit.md`](../architecture/decisions/self-improving-agent-feature-fit.md) | The 2026-08-17 control-plane-vs-compute scope decision that set rule 4's current wording. |
| [`compression-in-scope.md`](../architecture/decisions/compression-in-scope.md) | Why step-5 compression stays in RouteForge while RLHF moved out — the reasoning any scope-boundary proposal must engage with. |

### Tier 3 — reality check

- [`../backlog/bugs.md`](../backlog/bugs.md) — current defects (**B24** resolved 2026-09-15).
- [`rlhf-readiness.md`](./rlhf-readiness.md), [`../archive/eval-logging-backlog.md`](../archive/eval-logging-backlog.md) — the capture substrate feeding Target A/B datasets.

**Do not skip this tier.** IP §4 lists three correctness items that must land *regardless
of any IP decision*, because every number a filing or paper would cite depends on them:
cached-token accounting (savings figures are currently wrong in the conservative
direction); real measured latency in the scoring function (a fabricated per-tier constant
is used while actual latency is measured and discarded); and a cross-request circuit
breaker.

### Code — for verifying "already in the repo"

`app/routing/router.py` and `app/routing/strategy.py` (the filter/rank split that makes
eligibility structural), `app/policy/context.py` (`_stricter()` — the meet operation IP-1
generalizes), `app/policy/effective.py` (`FilteredDetector`), `app/cache/store.py`
(residency partition key), `app/output/streaming_verify.py` (hold-back redactor, IP-3),
`app/obs/export.py` (trace attributes), `app/routing/signals.py` (raise-only pattern),
`app/apps/registry.py` (`ModelRegistry`, `has_healthy_onprem()`), `app/pipeline.py`, and
the four `config/*.yaml`.

### Do not send / do not read

- `docs/superpowers/plans/` and `docs/superpowers/specs/` — ~60 dated build records. Pure
  noise for this task.
- [`../archive/2026-07-25-architecture-review.md`](../archive/2026-07-25-architecture-review.md) — carries its own
  historical-snapshot banner; predates two pivots.
- `docs/guides/`, `docs/reference/`, `deploy/`, `ui/`, `docs/marketing/` — operator,
  developer and packaging surfaces. None bears on IP.

---

## 4. What is already foreclosed — do not propose these

From IP §1, pulled from Google Patents / USPTO PPUBS (more reliable than the arXiv leads
in that document). **Do not build a filing around:**

| Foreclosed | By |
|---|---|
| A proxy that detects and redacts **output** PII by type per requestor class | HiddenLayer, US 12,105,844 B1 |
| Selecting the **cheapest model meeting a quality bar** | Cast AI, US 12,236,193 |
| Generic **semantic caching** for LLM Q&A | Inventus, US 12,259,913 B1 |
| **Router + cache + compression** as a combination | Infobip, US 2025/0165752 A1 (published app) |
| Field-level LLM data gating | SAP, US 12,541,616 B2 |
| Agent proposed-action verification | Citibank, US 12,450,494 B1 |

Adjacent but **not** a direct hit: Netskope US 12,068,944 B2 (multi-tenant cloud residency
routing) — it is not LLM-inference-specific and is telemetry-and-remediation *post-hoc*,
not filter-before-scoring. Worth attacking if you want to falsify the core thesis.

IP §2's "Explicitly not worth pursuing" also rules out a better semantic/complexity tier
classifier (crowded, and the literature's own recent results argue coarse task-type
routing captures most of the achievable gain).

---

## 5. The known gaps — this is the assignment

IP §5 states its own limitations. Treat them as the work order:

1. **The patent search is incomplete, and the document says so.** No assignee-scoped
   search was run against **Microsoft, Google, NVIDIA, Salesforce, Adobe, or ServiceNow**,
   all of whom almost certainly hold filings in this space (Google Patents' advanced
   assignee filter was not reachable from the tooling used). §1 is explicitly *"a floor on
   prior art, never a ceiling."* **Closing this is the single highest-value contribution
   available**, because it can invalidate Tier-1 items rather than merely add to them.
2. **A meaningful share of the arXiv IDs are unverified**, particularly 2026-dated ones
   (`26xx.xxxxx`). They are *leads, not citations*, and some may not exist. Verify against
   arxiv.org before any of them reaches a filing, a paper, or a slide.
3. **"Nobody does X" is bounded by search reach.** It means "not found in one pass." The
   three strongest claims — no eligibility axis in any routing benchmark; no streaming PII
   hold-back; no residency constraint anywhere in the KV-cache literature — were each
   corroborated by more than one independent scan, which raises confidence without
   settling it.
4. **Novelty ≠ patentability.** Obviousness, claim scope and freedom-to-operate are not
   assessed anywhere in that document and cannot be. They are questions for counsel.

---

## 6. What the existing analysis could not have seen

`ip-and-research-opportunities.md` is dated **2026-08-31**. **`policy_mode` shipped
2026-09-05** (PR #34, commit `36d7e37`) and the document contains **zero** mentions of it.
It is therefore entirely unassessed, and it is not a minor omission:

The change split **placement** (`residency` — *where may compute run*) from **protection**
(`policy_mode` — *how much does the gate do to the data*) into two orthogonal,
independently-declared axes, with the rule that **neither may ever be inferred from the
other** (CLAUDE.md rule 2a). It exists because the previous design used `onprem_only` as a
proxy for "maximum protection", which forced customers who wanted RouteForge as a pure
router/meter/tracer over their own data to **misdeclare residency** to obtain a protection
posture — strictly less safe, since it trades a hard residency floor that cloud cannot
survive by construction for a mutable provider allowlist.

Why it plausibly belongs in Tier 1: the document's entire thesis is that eligibility is a
*derived, per-request, pre-optimization* quantity. `policy_mode` is the assertion that the
**protection** dimension is likewise explicit, separately declared, versioned into
`app_policy_version`, stamped on every response, and emitted on the trace — while provably
never touching placement. Nothing in the §1 prior-art table addresses the separation of
these axes. Assess it; do not rediscover it by accident.

Start from [`../reference/per-app-config.md`](../reference/per-app-config.md) (`policy_mode` section) and
[`../guides/choosing-app-settings.md`](../guides/choosing-app-settings.md) for the operator
decision model and the real customer case that forced it.

---

## 7. Sequencing — the one irreversible thing

From IP §4, and it constrains *when* your recommendations can be acted on, not just what
they are:

**Public disclosure — an arXiv preprint, a conference talk, a public repo, a blog post —
starts clocks and, in most jurisdictions outside the US, destroys novelty for a later
filing.** The US grace period is limited and does not extend abroad. Several items are
*both* patent candidates and paper candidates (IP-1/R3, IP-3/R4, IP-2/R2), so **the filing
decision must precede the publication decision for each one**, and that decision belongs
with counsel.

Note also that every Tier-3 mechanism (IP-9 … IP-12) is **already shipped in a public
repo** — itself a fact for counsel, early.

Nothing in this brief or in the IP document is legal advice.

---

## 8. What to hand back

A reviewer's output is most useful in this shape:

1. **Verdict on the thesis** (IP §0). Does eligibility-as-a-derived-per-request-quantity
   survive an assignee-scoped patent search? This is the load-bearing claim; everything
   else is a corollary.
2. **Per-item disposition** for IP-1 … IP-12 — `confirmed` / `weakened` / `foreclosed` /
   `re-ranked` — each with the evidence that moved it. Say which items you did *not*
   reach.
3. **New prior art found**, with numbers and assignees, mapped to the items it affects.
4. **An assessment of `policy_mode`** (§6), placed in the existing tier structure.
5. **Additions, if any** — last, and explicitly marked as additions, having established
   they do not overlap IP-1 … IP-12.
6. **Corrections to §5's unverified arXiv IDs** you happened to check.

Keep the existing document's convention: for each item, state the *mechanism*, what
already exists in the repo, what would need building, why it is novel **against named
prior art**, and the risk. An item without named prior art is an opinion, not an
assessment.
