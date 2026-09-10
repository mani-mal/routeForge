# Docs reclassification & validity sweep — design

**Date:** 2026-09-10
**Status:** approved design, not yet implemented
**Scope:** the 45 non-`superpowers/` documents under `docs/`, plus every inbound
reference to them from code, config, skills, and the two root markdown files.

---

## 1. The problem

`docs/` has grown to 91 markdown files with no entry point and no placement rule.
Four specific failures:

1. **No index.** There is no `docs/README.md`. A reader arriving at 91 files has
   nothing telling them where to start or which pages are still true.
2. **`docs/` root is a junk drawer.** Thirteen loose files mix a beginner's setup
   guide (`getting-started.md`), an operator field reference (`per-app-config.md`),
   three separate backlogs, and one explicitly-historical review.
3. **`architecture/` conflates five genres.** Current-state description, scope
   decisions, external-tool analyses, binding feature specs, and IP research all sit
   in one flat directory. A reader cannot tell from the path whether a page describes
   shipped code or speculates about future work — the single most expensive
   ambiguity in the set.
4. **Drift is self-documented but never swept.** Twenty-five files carry a
   "superseded / historical / no longer" banner. Two of the three backlogs are
   entirely closed (`ui-open-items.md` is 7/7 done; `eval-logging-backlog.md` is
   marked BUILT) yet still sit alongside the live one.

## 2. Goals

- A reader can find the right page from `docs/README.md` in one hop.
- **The path tells you the epistemic status.** `architecture/` is verifiable against
  HEAD; `roadmap/` is not; `archive/` is dated and superseded. This is the load-bearing
  property of the whole scheme.
- Every retained page is verified against the code at HEAD, or carries a dated banner
  saying it isn't.
- One bug list, not three.
- Zero broken links when done — internal or inbound.

## 3. Non-goals

- **`docs/superpowers/` is untouched.** Its 44 plans and specs are an append-only
  SDD build log, not browsable documentation. `docs/README.md` names it as build
  history and moves on. The superpowers skills write there by convention; moving it
  would only mean future plans land back in the old path.
- **No merging of analysis docs.** Backlogs merge; nothing else does. The five
  comparison docs and five decision docs each answer a distinct question, and the
  reasoning is the value — merging risks losing it. They get cross-linked from
  bucket-level READMEs instead.
- **No rewriting for style.** Content changes are limited to correcting claims that
  are false at HEAD.
- Cleaning up the 11 stale `.worktrees/` directories — noted in §10 as a follow-up.

## 4. The taxonomy

Six top-level buckets plus three retained directories. Every placement is decided by
one question: **what is the reader's question, and can the claim be checked against
HEAD?**

| Bucket | Reader's question | Placement test |
|---|---|---|
| `guides/` | "How do I do X?" | Has ordered steps. The reader is *doing* something. |
| `reference/` | "What does this field/endpoint/metric do?" | Looked up, not read through. Tables. |
| `architecture/` | "How does it work today?" | Descriptive of shipped code. **Every claim checkable against HEAD.** |
| `architecture/decisions/` | "Why won't it do X?" | Records a scope call with a date and an owner. |
| `architecture/specs/` | "What must this feature honor?" | Binding contract. Code cites it by path. |
| `comparisons/` | "How does it compare to tool Y?" | Names an external tool or framework. |
| `roadmap/` | "What might we build?" | **Not built.** Makes no verifiable claim about HEAD. |
| `backlog/` | "What's broken?" | Defects and open feature gaps. One file. |
| `archive/` | — | Point-in-time or superseded. Retained for the decision trail. |
| `marketing/` | "How do I pitch it?" | Unchanged, plus demo narration. |
| `superpowers/` | "How was it built?" | Unchanged. Build history. |

**Ambiguity rule:** if a page could sit in `architecture/` or `roadmap/`, the
deciding question is whether its claims are checkable against HEAD *today*. If any
substantive claim is aspirational, it goes in `roadmap/`. This is what sends
`observability-metrics.md` to `roadmap/` — it catalogs what RouteForge *should*
surface; `reference/routing-observability.md` is the shipped counterpart.

## 5. File mapping — all 45 documents

### 5.1 `guides/` (12)

| From | To |
|---|---|
| `getting-started.md` | `guides/getting-started.md` |
| `onboarding-models.md` | `guides/onboarding-models.md` |
| `langfuse-per-app-projects.md` | `guides/langfuse-per-app-projects.md` |
| `dev-references/admin-setup.md` | `guides/admin-setup.md` |
| `dev-references/agentic-app-guide.md` | `guides/agentic-app-guide.md` |
| `help/choosing-app-settings.md` | `guides/choosing-app-settings.md` |
| `help/creating-a-sample-application.md` | `guides/creating-a-sample-application.md` |
| `help/integrating-an-app.md` | `guides/integrating-an-app.md` |
| `help/exporting-eval-datasets.md` | `guides/exporting-eval-datasets.md` |
| `help/ui-guide.md` | `guides/ui-guide.md` |
| `help/reading-the-dashboard-and-trace.md` | `guides/reading-the-dashboard-and-trace.md` |
| `help/faq.md` | `guides/faq.md` |

### 5.2 `reference/` (3)

| From | To |
|---|---|
| `dev-references/api-reference.md` | `reference/api-reference.md` |
| `per-app-config.md` | `reference/per-app-config.md` |
| `routing-observability.md` | `reference/routing-observability.md` |

### 5.3 `architecture/` (4 + diagrams)

| From | To |
|---|---|
| `architecture/routeforge-logical-and-request-response-architecture.md` | `architecture/logical-and-request-flow.md` |
| `architecture/routeforge-sample-request-flows.md` | `architecture/sample-request-flows.md` |
| `architecture/routing-tier-selection.md` | unchanged |
| `architecture/api-load-balancing.md` | unchanged |
| `architecture/diagrams/**` | unchanged |
| `architecture/README.md` | rewritten as the bucket index (§7) |

### 5.4 `architecture/decisions/` (5)

`inference-layer-boundary.md` · `compression-in-scope.md` ·
`app-vs-routeforge-boundary.md` · `self-improving-agent-feature-fit.md` ·
`per-app-config-flexibility.md` — all from `architecture/`, names unchanged.

### 5.5 `architecture/specs/` (2)

`semantic-cache-residency-spec.md` · `persistent-memory-residency-spec.md` — from
`architecture/`, names unchanged. **Both are cited by path from application code**
(§8.2), so these two moves carry the highest breakage risk in the set.

### 5.6 `comparisons/` (5)

`nemo-switchyard-comparison.md` · `switchyard-features-to-incorporate.md` ·
`external-router-feature-analysis.md` · `caching-cost-feature-fit.md` ·
`ai-governance-feature-fit.md` — all from `architecture/`, names unchanged.

### 5.7 `roadmap/` (4)

| From | To |
|---|---|
| `rlhf-readiness.md` | `roadmap/rlhf-readiness.md` |
| `architecture/ip-and-research-opportunities.md` | `roadmap/ip-and-research-opportunities.md` |
| `architecture/ip-review-handoff.md` | `roadmap/ip-review-handoff.md` |
| `observability-metrics.md` | `roadmap/observability-metrics-target-state.md` |

The rename on the last is deliberate: the current name reads as a reference to
shipped metrics, which it is not.

### 5.8 `backlog/` (1, consolidated from 3)

`bugs-backlog.md` → `backlog/bugs.md`. Before the other two are archived, any item
in them that is **not** closed is migrated in. Present belief from the audit is that
none qualifies — `ui-open-items.md` is 7/7 DONE/FIXED and `eval-logging-backlog.md`
is marked BUILT with a "What was built" section — but this must be **verified item by
item, not assumed**, and the verification recorded in the task report.

### 5.9 `marketing/` (3)

`positioning.md` and `routeforge-demo-talking-points.md` stay.
`demo-dashboard-notes.md` moves in from `docs/` root — it is demo narration
(captured while running the PCAI demo), not operator documentation.

### 5.10 `archive/` (3)

| From | To | Why |
|---|---|---|
| `architecture-review.md` | `archive/2026-07-25-architecture-review.md` | Self-declared historical snapshot; predates two pivots |
| `ui-open-items.md` | `archive/ui-open-items.md` | 7/7 items closed |
| `eval-logging-backlog.md` | `archive/eval-logging-backlog.md` | Marked BUILT 2026-07-29 |

Each gains a one-line header: `> Superseded by <path>. Retained for the decision trail.`
Date prefixes are added only where the file has a clear point-in-time identity.

### 5.11 Deleted (1)

`dev-references/README.md` — a two-audience onboarding path. Its content is absorbed
into `docs/README.md` and `guides/README.md`; keeping it would leave a third,
divergent index.

## 6. The validity sweep

The reclassification is the cheap half. This is the substantive half, and it runs
**before** any file moves (§9).

Each of the 45 documents gets one verdict:

| Verdict | Meaning | Action |
|---|---|---|
| **current** | Every substantive claim holds at HEAD | No change |
| **edit** | Specific claims are false | Fix those claims, leave the rest |
| **banner** | Still useful, but not worth re-verifying line by line | Add a dated status line at the top |
| **archive** | Superseded as a whole | Move per §5.10 |

### 6.1 What gets checked

- **Purged vocabulary.** `inference_backend`, `serving_paths`, `local-8b`, a `strong`
  tier, `mock` backend, `SP_INFERENCE_BACKEND` — all removed by the 2026-08-03
  pure-router pivot. Any surviving use outside `archive/` and `superpowers/` is a defect.
- **Config keys** against the four YAMLs (`serving`, `policy`, `routing`, `pricing`)
  and `app/config/schema.py`.
- **Endpoints and wire shapes** against `app/api/routes.py` and `app/api/admin_routes.py`.
- **Version claims.** Docs cite versions from `0.0.1` to `0.24.0`; the deployed API is
  `api:0.24.0`. Claims pinned to an older version need either re-verification or a banner.
- **The `policy_mode` split** (PR #34, 2026-09-05). Any page still treating
  `onprem_only` as a proxy for maximum protection is wrong. The 2026-09-05 sweep
  caught three; the remainder are unverified.
- **Cross-references** between docs — both that the target exists and that it still
  says what the citing page claims.

### 6.2 Method

Batched by bucket, one subagent per batch, each returning a per-file verdict table
with evidence (file:line for each correction). A subagent that cannot verify a claim
records it as unverified rather than guessing — **an unverified claim becomes a
banner, never a silent "current"**.

## 7. New files to author

| Path | Content |
|---|---|
| `docs/README.md` | The index. Routes by role (operator / app developer / engineer evaluating the design / contributor), then a table of all buckets with a one-line "what's in here and how current is it". Names `superpowers/` as build history. |
| `guides/README.md` | Ordered reading path for the two audiences inherited from `dev-references/README.md`. |
| `architecture/README.md` | Rewritten from the existing diagram-notes file. Keeps the canonical step-count note and the diagram pointers; adds the `decisions/` and `specs/` sub-bucket explanation. |
| `comparisons/README.md` | One line per comparison: which tool, what was decided, whether the decision is open. |
| `roadmap/README.md` | One line per item + its status. Must state plainly that nothing here is shipped. |
| `archive/README.md` | Why each file is here and what superseded it. |

## 8. Link repair

### 8.1 Intra-docs

136 relative `.md` links across the non-`superpowers` set. Every one is re-resolved
after the moves.

### 8.2 Inbound, from outside `docs/`

Verified inventory — roughly 30 source-controlled files. Grouped by what they cite:

| Cited doc | Files referencing it |
|---|---|
| `onboarding-models.md` | `CLAUDE.md` (5 refs), `README.md`, 3 × `.claude/skills/{greenlake-deploy,inference-backends,routing-and-gateway}/SKILL.md`, `app/gateway/backends/README.md`, `config/serving.yaml`, `deploy/compose/docker-compose.yml`, `deploy/spark/{README.md,docker-compose.yml}`, `tools/recommend/rules.py` |
| `semantic-cache-residency-spec.md` | `app/cache/store.py`, `app/config/lint.py`, `app/config/schema.py`, `app/main.py`, `app/models.py`, `app/pipeline.py`, `config/serving.yaml`, `tests/test_semantic_cache_{store,pipeline}.py` |
| `persistent-memory-residency-spec.md` | `app/memory/__init__.py`, `app/memory/store.py`, `app/config/schema.py`, `config/serving.yaml`, `tests/test_memory_{store,pipeline}.py` |
| `eval-logging-backlog.md` | `app/obs/export.py`, `app/agent/runtime.py`, `app/api/routes.py`, `app/config/schema.py`, `config/serving.yaml`, `tests/test_capture_io.py` |
| `per-app-config.md` | `CLAUDE.md`, `app/api/admin_routes.py`, `ui/src/api.ts`, `tests/test_streaming.py`, `.env.example` |
| `routing-observability.md` | `CLAUDE.md` |
| `self-improving-agent-feature-fit.md` | `CLAUDE.md` |
| `per-app-config-flexibility.md` | `tests/test_streaming.py` |
| `langfuse-per-app-projects.md` | `.env.example`, `deploy/dgx_lab/routeforge-app.yaml` |
| `choosing-app-settings.md` | `ui/src/admin/AppSettings.tsx` |
| `exporting-eval-datasets.md` | `tools/prompt_opt/dataset.py` |
| `integrating-an-app.md` | `examples/client/README.md`, `tests/traffic/README.md` |
| `ui-open-items.md` | `ui/src/admin/clipboard.tsx`, `tests/test_history_store.py` |
| `architecture-review.md` | `app/apps/bootstrap.py` |
| `dev-references/` (dir) | `README.md` |

Two of these cite documents that move to `archive/` (`eval-logging-backlog.md`,
`architecture-review.md`, `ui-open-items.md`). Those comments get repointed to the
archive path **and** to the live successor where one exists — a code comment pointing
only at an archived doc is worse than no comment.

### 8.3 Gate

A link-checker over all of `docs/` plus a grep for every moved basename across the
repo (excluding `.git`, `.venv`, `node_modules`, `__pycache__`, `.worktrees`). Both
must come back clean before the PR opens.

## 9. Execution order

Sweeping before moving keeps every diff single-purpose. A commit that both rewrites a
paragraph and renames its file is unreviewable.

1. **Validity sweep, in place.** Batched by bucket. Content-only commits, paths untouched.
2. **Backlog consolidation.** Verify item-by-item that the two dead backlogs hold
   nothing live; migrate anything that is; produce `backlog/bugs.md`.
3. **Structural moves.** `git mv` only — no content edits in these commits.
4. **Link repair + new index files.**
5. **Gate.** §8.3 clean, plus `pytest` — four test files cite doc paths in comments,
   and `app/apps/bootstrap.py` does too.

Branch `docs/reclassification` off current `main` (`27652dc`), worktree-isolated, PR
at the end. Per the global CLAUDE.md, tasks run subagent-driven with review between them.

## 10. Risks and follow-ups

| Risk | Mitigation |
|---|---|
| A code comment silently points at a moved path | §8.3 grep gate over the whole repo, not just `docs/` |
| The sweep "fixes" a claim that was actually right | Corrections carry file:line evidence; unverifiable → banner, not edit |
| A live item is lost when the dead backlogs are archived | §5.8 requires item-by-item verification, recorded in the task report |
| Reviewer sees one 60-file diff | Five commit groups per §9; moves and content never mix |

**Follow-ups, explicitly out of scope here:**

- **11 stale `.worktrees/` directories** (`b17-request-grouping`,
  `cluster-d-durable-dashboards`, `dspy-prompt-optimization`, `fix-b19-slug-routes`,
  `lateral-model-switch`, `per-app-flexibility`, `persistent-memory`,
  `quality-axis-and-strategy`, `recommend-and-orchestrate`, `role-aware-routing`,
  `semantic-cache`) with matching `.git/worktrees/` entries. Every one holds a full
  stale copy of `docs/` and `CLAUDE.md`, which corrupts any repo-wide grep. Worth a
  `git worktree prune` pass on its own.
- **B24** remains open and is a real 500. Unrelated to this work, but the sweep will
  keep surfacing it.

## 11. Acceptance criteria

1. `docs/README.md` exists and links every bucket.
2. All 45 documents are accounted for — 44 at their §5 destination, 1 deleted per §5.11 — and `docs/` root holds only `README.md`.
3. Every document has a recorded verdict from §6; every **edit** carries evidence.
4. One bug list at `backlog/bugs.md`; the other two are in `archive/` with headers,
   and the task report states explicitly that no live item was lost.
5. §8.3 gate clean: no broken intra-docs link, no stale basename anywhere in the repo.
6. `pytest` passes.
7. `git log --follow` resolves history for every moved file (i.e. `git mv`, not
   delete-and-add).
