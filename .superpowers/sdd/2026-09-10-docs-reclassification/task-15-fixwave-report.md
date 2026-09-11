# Task 15 — final fix wave for PR #39

Branch `docs/reclassification`, starting HEAD `53c644b`.

## Fix 1 (CRITICAL) — `capture_io: "full"` clamp, residency conjunct

**Evidence gathered before editing anything:**

- Read `app/api/admin_routes.py:648-661`. The clamp on `capture_io == "full"` raises
  422 on exactly two independent conditions:
  1. `load_app_langfuse(app) is None` (no own provisioned Langfuse project) — absolute,
     never relaxed.
  2. `eff_residency == "onprem_only" and eff_policy_mode != "passthrough"`.
  Condition 2 is a **conjunction of residency and policy_mode** — it does not fire for a
  non-`onprem_only` app regardless of `policy_mode`.
- Read and ran `tests/test_admin_api.py::test_policy_raw_capture_allowed_for_non_onprem_app_with_own_langfuse`
  (line 344). The app is created with no `default_residency` (so not `onprem_only`), is
  given its own Langfuse project, and the policy PUT sends `pii_detection: "off"` +
  `capture_io: "full"` with **no `policy_mode` field** (so it inherits `enforced`). The
  request returns 200 and the stored policy shows `capture_io == "full"`. Ran
  `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_admin_api.py -q`: 55 passed,
  exit 0.
- Conclusion matching the prompt's framing: a non-`onprem_only`, `enforced` app **can**
  reach `capture_io=full`. The two doc lines claiming otherwise ("at any residency") were
  wrong.

**Edits:**

- `docs/guides/faq.md` (~104-108, under "What has not changed, and will not"): restored
  the residency conjunct — the invariant is scoped to an `onprem_only` app in `enforced`
  posture, and the line now explicitly says a non-`onprem_only` `enforced` app already can
  hold `capture_io=full`, pointing at the next question's walkthrough of that side effect.
- Re-read the whole surrounding section (lines ~89-155) for coherence after the edit.
  The "Can I just set residency=..." callout (~126-132) that says raw capture is
  "unlocked by policy_mode, not by residency" still reads correctly in context: it is
  advising against the old residency-misdeclaration workaround now that `passthrough`
  exists as a dedicated, placement-neutral knob — it does not contradict the corrected
  invariant. No further edits needed there.
- `docs/guides/choosing-app-settings.md` (~508-512): the old text asserted the block
  applies "at any residency" and then separately said "an `onprem_only` + `enforced` app
  certainly cannot" — which was actually self-contradictory (redundant if the first half
  were true). Rewrote to condition the whole bullet on `onprem_only`, and noted a
  non-`onprem_only` `enforced` app already holds `capture_io=full` without any write.
- Left `docs/guides/admin-setter.md` (~206) and `docs/guides/getting-started.md`
  (~374-384, the Eval recipe) unchanged — both already state the residency-scoped rule
  correctly and were the reference wording matched against.

## Fix 2 — architecture doc's unsupported "default is regex" claim

Verified all four facts independently before editing:
- `app/policy/detectors.py:131-138` — `make_detector(mode: str)` has no default
  parameter and raises `ValueError` on an unknown mode.
- `config/serving.yaml:71` — `pii_detection: presidio`.
- `docs/guides/getting-started.md:195` — "`presidio` *(default; also what `null`
  inherits)*".
- `deploy/compose/.env.example:11` and `deploy/spark/.env.example:33` both set
  `SP_PII_DETECTION=regex`; `deploy/dgx_lab/routeforge-app.yaml:121` sets
  `SP_PII_DETECTION=presidio`.

Rewrote `docs/architecture/logical-and-request-flow.md` (~44) to say the configured
default is `presidio` (per `config/serving.yaml`), that `make_detector` takes no default
and raises on an unknown mode, and that the bundled Compose/Spark deploys override to
`regex` (offline-safe, no spaCy model download).

## Fix 3 — stale `config/serving.yaml` comments

Verified `app/pipeline.py` calls `recall_memory`/`write_memory` (lines 453, 556, 742) and
`self._cache.lookup`/`.store` (lines 494, 570) — both features are wired, contradicting
the two stale comments. Corrected the memory-block comment (~38, was "no pipeline wiring
yet") and the semantic-cache-block comment (~56, was "Task 2 wires the check/store calls
in", future tense). `git diff config/serving.yaml` confirmed comment-only — no
configuration value changed.

## Fix 4 — missing bucket READMEs

Created `docs/reference/README.md` and `docs/marketing/README.md`, matching the shape and
length of `docs/comparisons/README.md` / `docs/roadmap/README.md` (short intro stating
trust level, then a table of files with one-line summaries, then a cross-link back to
`guides/`). Updated `docs/README.md`'s `reference/` and `marketing/` rows to link the new
READMEs instead of the bare directories.

## Fix 5 — stale link text in `docs/backlog/bugs.md`

Lines 4, 5, 58, 59 displayed link text `docs/ui-open-items.md` /
`docs/eval-logging-backlog.md` while the hrefs correctly pointed at
`../archive/ui-open-items.md` / `../archive/eval-logging-backlog.md`. Updated the visible
text to `archive/ui-open-items.md` / `archive/eval-logging-backlog.md`; hrefs untouched.
(Left the pre-existing `docs/archive/eval-logging-backlog.md` link text at lines 853/869
alone — those already include `archive/` and were not part of the flagged defect.)

## Fix 6 — B31 backlog entry (no code change)

Added B31 to `docs/backlog/bugs.md`, placed newest-first (2026-09-11, after the
2026-09-10 sweep's B26-B30 block, before it in the file): CLAUDE.md rule 2 requires a
"declared no-protection posture" for raw capture, but the code's clamp only checks that
for `onprem_only` apps — a non-`onprem_only` app with `presidio` detection, its own
Langfuse project, and `capture_io: "full"` is accepted with no declared no-protection
posture at all (same test evidence as Fix 1). Noted this is also why the faq.md line read
as plausible before the fix. No code touched.

## DO NOT CHANGE items

Left `tests/test_history_store.py:125-126` and `docs/backlog/bugs.md:626,639` (stale
`ApplicationsPanel.tsx` line ranges inside resolved B6/B7 write-ups) untouched, per
instructions.

## Verification

- `python3 scripts/check_docs_links.py` → `0 broken internal link(s), 0 stale inbound
  path(s), 0 purged-vocabulary hit(s).` exit 0.
- `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_admin_api.py -q` → 55
  passed, exit 0 (run twice: once before editing to establish the Fix 1 evidence, once
  after all edits to confirm no regression).
- `git diff config/serving.yaml` → comment-only lines changed, no config values touched.
- Every line number cited above was verified by reading the file directly (Python
  indexed read / `grep -n`), not `cat -n | sed`.
