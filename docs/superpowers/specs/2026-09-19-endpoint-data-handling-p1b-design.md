# P1b — Endpoint data-handling metadata and contractual eligibility

**Status:** Approved design, 2026-09-19. Not yet implemented.
**Implements:** Priority 1, slice B of `docs/archive/design-handoffs/RouteForge-Priorities-1-3-LLM-Handoff.md`
(§6's eligible-set table, §10's endpoint-registry validation, invariants 4 and 6).
**Builds on:** P1a (merged, PR #52) — the compiler, the axes, and the certificate.

## 1. Problem

RouteForge can exclude an endpoint for four reasons today: residency, a disabled
provider, the app's model/provider/tier allowlists, and model scope
(`allowed_app_ids`). It cannot exclude one for the reasons a compliance officer
actually writes down:

- *"Patient data must remain in the United States"* — an endpoint's **region** is
  not recorded anywhere. `OnboardedModel` carries slug, provider, tier, quality,
  price, endpoint, context window, `enabled`, `allowed_app_ids` and probe status;
  `PoolCandidate` carries name, provider, tier, display name and quality.
- *"The provider must not retain prompts"* — **retention** is not recorded.
- *"This endpoint may see raw patient data; that one only after redaction"* —
  whether an endpoint may receive **unredacted sensitive content** is not recorded.

So the healthcare example in the priorities doc cannot be expressed, and the
certificate's exclusion reasons are routing vocabulary ("tier", "provider")
rather than the customer's.

## 2. What an endpoint declares

Three attributes, on both endpoint sources:

| Attribute | Values | Meaning |
|---|---|---|
| `region` | a token, normalized to lowercase (`us`, `eu`, `de`, `onprem`) | where the compute runs |
| `prompt_retention` | `none` \| `retained` \| `unknown` | whether the provider keeps prompts |
| `accepts_raw_sensitive` | `true` \| `false` \| `unknown` | may this endpoint receive unredacted sensitive content |

**Three sources, one resolution order** (most specific wins):

1. the per-model value — a column on `onboarded_models`, or a key on a
   `routing.yaml` `model_pool` entry;
2. the provider default in `serving.yaml`
   (`providers.azure: {region: us, prompt_retention: none}`);
3. `unknown`.

`ModelRegistry.effective_pool()` already merges the YAML pool with enabled
onboarded rows into `PoolCandidate`s; these three fields ride along on that same
merge, so every consumer sees one shape. `_OnboardedRow` gains them too.

**Regions are customer vocabulary, not an enum.** A token compared exactly after
lowercase normalization. RouteForge ships no geography list, because a customer's
"eu" may mean something specific to their contract.

**These are declarations, not proofs.** RouteForge records what an operator
declared about an endpoint and enforces policy against that declaration. It
cannot verify a provider's contractual claim, and no document may imply it does.
The certificate names the declaration's source (per-model, provider default, or
unknown) so an auditor can see what the decision rested on.

## 3. What an app requires

Two new nullable `AppPolicy` columns, resolved by the compiler like every other
axis (ceiling → explicit → preset → deployment default, spec P1a §2.2):

| Column | Values | Null means |
|---|---|---|
| `allowed_regions` | list of tokens | no regional constraint |
| `require_no_retention` | bool | false |

They are **placement-adjacent constraints, not protection axes**: they narrow
where a request may run, never what the gate does to the data. They may only
narrow the candidate set — no value of either can widen it, which is what keeps
P1a's noninterference property intact.

## 4. The three exclusion rules

Applied where the candidate pool is already filtered (`app/policy/context.py`),
so an excluded endpoint is unreachable by construction rather than rejected
post-hoc (CLAUDE.md rule 3's shape, extended to contractual facts):

| Rule | Condition | Reason code | An `unknown` endpoint is |
|---|---|---|---|
| Region | `allowed_regions` set and the endpoint's region is not in it | `region_not_allowed` | **excluded** |
| Retention | `require_no_retention` and retention is not `none` | `prompt_retention_not_allowed` | **excluded** |
| Raw sensitive | the request would send raw sensitive content and the endpoint declares `accepts_raw_sensitive: false` | `raw_sensitive_not_accepted` | **kept** |

> **Amended 2026-09-19.** The raw-sensitive row originally read "…and the
> endpoint **does not declare** `accepts_raw_sensitive: true`", which excluded
> an `unknown` endpoint as well as a declared `false`. That is superseded by the
> wording above — the rule now fires **only on a declared `false`**. The
> original wording is recorded here rather than silently rewritten, because the
> obvious "fix" is to restore it: see the justification below for why that would
> be a regression, not a correction.
>
> **Amended again 2026-09-19 (final branch review).** "Would send raw sensitive
> content" originally required a sensitivity above `public`, which made the rule
> **permanently inert for `policy_mode: passthrough`** — the one app class that
> by definition sends unredacted content. The passthrough preset resolves to
> `model_input: raw` *and* `pii_detection: off`; with no detector nothing is ever
> detected, no sensitivity rule matches, the request classifies `public`, and the
> flag was always false. An app that has declared it sends *unclassified* raw
> data cannot show its content is public, so `model_input: raw` + detection `off`
> now counts on its own — the same fail-closed posture as "`unknown` never
> satisfies a requirement", applied to the request side. The rule still fires
> only on an operator's explicit `accepts_raw_sensitive: false`, so §12's
> upgrade-safety promise is untouched.

**"Would send raw sensitive content"** means: the compiled `model_input` is `raw`
**and** either the gate classified the request above `public` (i.e. a sensitivity
rule matched at least one detected entity) **or** the compiled `pii_detection` is
`off`. Under `model_input: redact` the model
receives placeholders, so the rule does not fire — which is exactly the
priorities doc's "Azure US: eligible after redaction" row, obtained from P1a's
existing axis rather than a third app knob. Azure US's row is a **declared
`false`**: it is excluded for a raw+sensitive request and eligible again the
moment `model_input` is `redact`, so the amendment leaves the healthcare
scenario working exactly as §12 describes it.

**Unknown never satisfies a requirement, and never blocks a request that imposes
none.** Both halves of that sentence are load-bearing, and they are why the
`unknown` column above is not uniform:

- **Region and retention exclude an `unknown` endpoint**, because *the app
  declared a requirement*. An endpoint that cannot prove the property does not
  qualify — that is the entire content of the requirement. An endpoint whose
  region is `unknown` is therefore excluded only for an app that set
  `allowed_regions`; an app that set neither column is unaffected.
- **The raw-sensitive rule keeps an `unknown` endpoint**, because *no app
  declared anything*. Its requirement comes from the request's own shape (a
  `raw` model_input over a better-than-public sensitivity), so excluding
  `unknown` there would retire **every endpoint in every existing deployment**
  on the first request carrying a detected entity — a rule nobody asked for,
  breaking §12's promise that an app which sets neither requirement routes
  byte-identically. Nothing backfills `accepts_raw_sensitive`, so there is no
  upgrade path that avoids it. Only an operator's explicit `false` is enforced.

This is the deliberate reading of the priorities doc's invariant 4: an endpoint
may not *prove* a property it never declared, but an undeclared property is not
itself a defect for a policy that never asks about it. The strict alternative —
every incompletely-described endpoint leaves every candidate set — would make
this branch an outage for every existing deployment on upgrade, and is deferred
behind a future per-deployment switch that nobody has asked for yet.

## 5. Fallback stays inside the eligible set

The gateway's failover and confidence-escalation candidates are drawn from the
**compiled feasible set**, not from the pool. A candidate excluded on the first
pass cannot be reached on a retry. This makes the priorities doc's invariant 6
structural, and turns P1a's `fallback_in_eligible_set` check into a verification
of something that cannot fail by construction rather than an after-the-fact
report. Today's failover already draws from the filtered pool; the work is
threading the *compiled set* through and pinning it with a test that a
region-excluded endpoint is never tried on failover.

## 6. Versioning and evidence

- **`endpoint_registry_version`** — a content hash over the effective pool's
  identity and data-handling metadata, emitted as `sp.endpoint_registry_version`
  and carried in the certificate beside `app_policy_version` and
  `routing_policy_version`. A decision then names the registry it was made
  against, which is what the priorities doc's replay contracts assume, and a
  changed endpoint declaration becomes visible in the trace instead of silent.
- **Exclusion reasons** — the three new codes join `sp.candidates`' rejected set,
  so the certificate answers "which endpoints were eligible, and why not the
  others" in the operator's vocabulary.
- **Declaration provenance** — each of the three attributes records whether it
  came from the model, the provider default, or is unknown.

## 7. Configuration and lint

`serving.yaml` gains optional `region` / `prompt_retention` /
`accepts_raw_sensitive` keys under each `providers.<name>` block; `routing.yaml`
gains the same three as optional keys per `model_pool` entry. The cross-file lint
adds two checks:

- a provider default or pool entry naming an unknown key, or a value outside its
  enum, **fails the boot** (the existing drift-becomes-boot-failure posture);
- an app requiring a region that no endpoint in the pool declares raises a
  **warning**, since that configuration can only ever produce an empty candidate
  set. A warning, not a failure: the endpoint may be onboarded later.

## 8. Console

The onboarding form gains the three declarations, with `unknown` as the default
and a one-line note that they are declarations RouteForge enforces but cannot
verify. The request drill-down shows the new exclusion reasons beside the
existing ones. **No new client-side rules:** the console renders server-supplied
values, per the `effective` block P1a established.

## 9. Schema and deployment

Five `ALTER TABLE` statements — three on `onboarded_models`, two on
`app_policies` — appended to `scripts/migrate_app_policies.py` and documented in
`docs/reference/per-app-config.md`'s "Manual DB migration" section. **Run them
before rolling the image**, as with P1a. Every column is nullable and resolves to
`unknown` / no-constraint, so no existing endpoint or app changes behavior.

## 10. Non-negotiables this must not break

Re-assert by test: `onprem_only` still filters cloud providers before scoring
(rule 3); the new constraints may only narrow, never widen (so an app requiring
`region: us` can never thereby reach a provider its residency excludes); a
request whose candidate set empties is **rejected with reasons**, never relaxed
(rule 5, and IP-9's derived reject); nothing about these constraints reaches the
gate's ordering or the protection axes (P1a noninterference).

## 11. Out of scope

Certifications, training-on-data and subprocessor metadata; a strict mode that
excludes any endpoint with incomplete metadata; endpoint capability matching
(vision, context length) beyond what the router already does; the minimal
conflicting-core extraction from Priority 3; and everything in P2's evaluation
plane.

## 12. Definition of done

- An operator declares region and retention on an onboarded endpoint and on a
  cloud pool entry, and an app requiring `us` + no-retention routes only to the
  endpoints that satisfy both — with the others named in the certificate under
  `region_not_allowed` / `prompt_retention_not_allowed`.
- The healthcare scenario from the priorities doc §6 runs end to end: four
  endpoints, an SSN request, two survivors, and the two exclusions explained in
  the operator's own vocabulary.
- An endpoint excluded for a data-handling reason is never reached by a failover
  or an escalation.
- An app that sets neither new column routes byte-identically to today, and every
  existing endpoint keeps working without backfill.
- `sp.endpoint_registry_version` changes when a declaration changes, and the
  certificate names the provenance of each attribute it relied on.
