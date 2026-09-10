# Docs Reclassification & Validity Sweep — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the 45 non-`superpowers/` documents under `docs/` into a taxonomy where the path tells you the epistemic status, and verify every retained page against the code at HEAD.

**Architecture:** Build an executable link/vocabulary gate first (Task 1), then sweep content in place (Tasks 2–9), then move files with `git mv` only (Task 10), then repair links and author indexes (Tasks 11–13), then run the full gate (Task 14). Content edits and renames never share a commit — a diff that does both is unreviewable.

**Tech Stack:** Python 3.12 (`uv` for deps), pytest, `git mv`. The gate is a plain stdlib script — no new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-10-docs-reclassification-design.md`

## Global Constraints

- **`docs/superpowers/` is never modified** except to add this plan and its spec. It is an append-only SDD build log. All greps, gates, and sweeps exclude it.
- **`docs/archive/` is exempt from the vocabulary gate.** Archived docs are allowed to contain purged vocabulary — that is what makes them historical records.
- **Moves use `git mv`,** never delete-and-add. `git log --follow` must resolve history for every moved file (spec §11.7).
- **No merging of analysis docs.** Backlogs merge; nothing else does (spec §3).
- **No rewriting for style.** Content edits are limited to claims that are false at HEAD (spec §3).
- **An unverifiable claim becomes a banner, never a silent "current"** (spec §6.2).
- **Purged vocabulary** (spec §6.1), forbidden outside `archive/` and `superpowers/`: `inference_backend`, `SP_INFERENCE_BACKEND`, `serving_paths`, `local-8b`, `mock` backend, a `strong` tier.
- **Canonical values:** provider is `onprem` (one word); residency flag is `onprem_only`; tiers are exactly `cheap | mid | frontier`; sensitivity enum is `public | internal | confidential | restricted`.
- **Branch:** `docs/reclassification` off `main` at `87ac5a3`. One PR at the end.
- Every task ends with a commit. Commit messages end with the two attribution lines used in this repo (see Task 1 Step 6 for the exact block).

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `scripts/check_docs_links.py` | The gate. Three checks: internal markdown links, inbound `docs/` paths from the rest of the repo, purged vocabulary. Stdlib only. |
| `tests/test_docs_links.py` | Makes the gate a pytest failure, so it runs in the normal suite. |
| `docs/README.md` | Root index. Routes by role. |
| `docs/guides/README.md` | Ordered reading path for operators and app developers. |
| `docs/comparisons/README.md` | One line per comparison + whether the decision is open. |
| `docs/roadmap/README.md` | One line per item + status. States plainly that nothing here ships. |
| `docs/archive/README.md` | Why each file is here and what superseded it. |
| `docs/backlog/bugs.md` | The single consolidated bug list. |

**Modified:** `docs/architecture/README.md` (rewritten as bucket index), the ~30 source files in spec §8.2, and whichever docs the sweep finds false claims in.

**Deleted:** `docs/dev-references/README.md` (content absorbed into `docs/README.md` + `docs/guides/README.md`).

---

## Task 1: The link + vocabulary gate

Everything after this task is verified by this script. Build it first and establish a clean baseline, so any breakage a later task introduces is unambiguously that task's fault.

**Files:**
- Create: `scripts/check_docs_links.py`
- Create: `tests/test_docs_links.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `check_docs_links.check_internal() -> list[str]`, `check_inbound() -> list[str]`, `check_vocabulary() -> list[str]`, `main() -> int`. Tasks 2–14 run `python3 scripts/check_docs_links.py` and require exit 0.

- [ ] **Step 1: Create the branch**

```bash
cd /home/manimal/projects/routeForge
git checkout main && git pull --ff-only origin main
git checkout -b docs/reclassification
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_docs_links.py`:

```python
"""The docs link + vocabulary gate, as a pytest failure.

scripts/check_docs_links.py is the implementation; this makes it run in the
normal suite so a stale doc path fails CI like any other regression.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_docs_links.py"


def _run() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_no_broken_internal_doc_links():
    result = _run()
    assert result.returncode == 0, (
        "docs link/vocabulary gate failed:\n" + result.stdout + result.stderr
    )
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/test_docs_links.py -v`
Expected: FAIL — the script does not exist yet, so the subprocess returns non-zero.

- [ ] **Step 4: Write the gate**

Create `scripts/check_docs_links.py`:

```python
#!/usr/bin/env python3
"""Link + vocabulary gate for docs/.

Three checks:
  1. internal   — every relative markdown link inside docs/ resolves.
  2. inbound    — every `docs/...` path mentioned elsewhere in the repo resolves.
  3. vocabulary — vocabulary purged by the 2026-08-03 pure-router pivot does not
                  appear outside docs/archive/ and docs/superpowers/.

Usage:  python3 scripts/check_docs_links.py
Exit:   0 clean, 1 problems found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".worktrees",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".superpowers",
}

SCAN_SUFFIXES = {
    ".md", ".py", ".ts", ".tsx", ".js", ".jsx", ".yaml", ".yml", ".toml", ".example",
}

# Docs allowed to contain purged vocabulary: they are historical records.
VOCAB_EXEMPT = ("docs/archive/", "docs/superpowers/")

# The SDD build log is append-only: never link-checked, never edited.
SKIP_PREFIXES = ("docs/superpowers/",)

SELF = Path(__file__).resolve()

# ](target) or ](target "title") — markdown inline links.
MD_LINK = re.compile(r"\]\(\s*<?([^)>\s]+)>?\s*(?:\"[^\"]*\")?\s*\)")
# docs/foo/bar.md mentioned in prose or a code comment.
DOCS_FILE = re.compile(r"(?<![\w/.-])docs/[A-Za-z0-9_][A-Za-z0-9_./-]*\.md")
# docs/foo/ mentioned as a directory.
DOCS_DIR = re.compile(r"(?<![\w/.-])docs/[A-Za-z0-9_-]+/(?![A-Za-z0-9_.-])")

FORBIDDEN = {
    "inference_backend": "the inference_backend axis was removed by the pure-router pivot",
    "SP_INFERENCE_BACKEND": "the inference_backend env override no longer exists",
    "serving_paths": "there is no serving_paths config block",
    "local-8b": "the bundled demo model was removed; the on-prem pool boots empty",
}


def walk(base: Path):
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def check_internal() -> list[str]:
    problems: list[str] = []
    for md in sorted(walk(DOCS)):
        if md.suffix != ".md":
            continue
        if md.relative_to(ROOT).as_posix().startswith(SKIP_PREFIXES):
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        for raw in MD_LINK.findall(text):
            target = raw.split("#", 1)[0].strip()
            if not target:
                continue  # pure anchor, e.g. ](#section)
            if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                continue  # http:, https:, mailto:, …
            if not (md.parent / target).exists():
                problems.append(f"{md.relative_to(ROOT)}: broken link -> {raw}")
    return problems


def check_inbound() -> list[str]:
    problems: list[str] = []
    for path in sorted(walk(ROOT)):
        if path.suffix not in SCAN_SUFFIXES:
            continue
        if DOCS in path.parents:
            continue  # internal links are check 1's job
        if path == SELF:
            continue  # this file names docs/ paths as data, not as references
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(ROOT)
        for match in sorted(set(DOCS_FILE.findall(text))):
            if not (ROOT / match).exists():
                problems.append(f"{rel}: stale docs path -> {match}")
        for match in sorted(set(DOCS_DIR.findall(text))):
            if not (ROOT / match).is_dir():
                problems.append(f"{rel}: stale docs dir -> {match}")
    return problems


def check_vocabulary() -> list[str]:
    problems: list[str] = []
    for md in sorted(walk(DOCS)):
        if md.suffix != ".md":
            continue
        rel = md.relative_to(ROOT).as_posix()
        if rel.startswith(VOCAB_EXEMPT):
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            for term, why in FORBIDDEN.items():
                if term in line:
                    problems.append(f"{rel}:{line_no}: '{term}' — {why}")
    return problems


def main() -> int:
    internal = check_internal()
    inbound = check_inbound()
    vocabulary = check_vocabulary()

    for problem in internal:
        print(f"INTERNAL    {problem}")
    for problem in inbound:
        print(f"INBOUND     {problem}")
    for problem in vocabulary:
        print(f"VOCABULARY  {problem}")

    total = len(internal) + len(inbound) + len(vocabulary)
    print(
        f"\n{len(internal)} broken internal link(s), "
        f"{len(inbound)} stale inbound path(s), "
        f"{len(vocabulary)} purged-vocabulary hit(s)."
    )
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the gate and fix the baseline**

Run: `python3 scripts/check_docs_links.py`

The current tree is **expected to have pre-existing failures** — nothing has ever checked this. Fix each one where the fix is unambiguous (a link whose target was renamed, a `docs/` path that moved in an earlier session).

Two hits are known in advance and must **not** be "fixed" by deleting the mention:
- `docs/architecture-review.md` and other pre-pivot docs legitimately discuss `inference_backend`. They move to `archive/` in Task 10, which exempts them. Until then, record them in the commit message as expected-and-deferred rather than editing the historical text.
- If a vocabulary hit is in a doc that is **not** heading to `archive/`, it is a genuine defect — fix the text.

If a hit cannot be resolved without a judgement call, leave it and record it in the task report for review. Do not guess.

Run again: `python3 scripts/check_docs_links.py`

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_docs_links.py -v`
Expected: PASS

If it cannot pass because of the deferred `archive/`-bound vocabulary hits from Step 5, add those specific file paths to `VOCAB_EXEMPT` **with a comment saying they are removed in Task 10**, and remove them from the exemption list in Task 10. Record this in the task report.

- [ ] **Step 7: Commit**

```bash
git add scripts/check_docs_links.py tests/test_docs_links.py
git add -A docs/
git commit -F - <<'MSG'
test(docs): add the link + purged-vocabulary gate

Three checks over docs/: relative markdown links resolve, `docs/...` paths
mentioned elsewhere in the repo resolve, and vocabulary purged by the
2026-08-03 pure-router pivot does not appear outside archive/ and superpowers/.

Stdlib only, wired into pytest so a stale doc path fails like any regression.
Establishes a clean baseline before the reclassification moves anything.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

## Tasks 2–8: The validity sweep

**These tasks change content only. No file moves, no renames.** Paths stay exactly as they are; Task 10 does all the moving.

### The sweep procedure — identical for Tasks 2–8

Every sweep task follows this, with only the file list and the focus areas differing.

For **each** file in the task's list:

1. Read the file completely.
2. For each substantive claim, find the code, config, or endpoint that proves or disproves it. Record `file:line` evidence.
3. Assign exactly one verdict:
   - **current** — every substantive claim holds. No edit.
   - **edit** — specific claims are false. Fix *those claims only*. Leave prose style alone.
   - **banner** — still useful but not worth line-by-line re-verification. Add a dated status line at the top: `> **Status check 2026-09-10.** <what is known stale>. Current authority: <path>.`
   - **archive** — superseded as a whole. **Do not move it** — note it in the report; Task 10 handles the move.
4. **An unverifiable claim becomes a banner, never a silent "current."**

**Universal checks for every file** (spec §6.1):
- Purged vocabulary: `inference_backend`, `SP_INFERENCE_BACKEND`, `serving_paths`, `local-8b`, `mock` backend, `strong` tier. (`strong` is checked by hand — it is too common a word to automate.)
- Canonical values: provider `onprem` one word; residency `onprem_only`; tiers exactly `cheap | mid | frontier`; sensitivity `public | internal | confidential | restricted`.
- The **`policy_mode` split** (PR #34, 2026-09-05): any page treating `onprem_only` as a proxy for maximum protection is **wrong**. Placement (`residency`) and protection (`policy_mode`) are separate axes.
- Version claims — deployed API is `api:0.24.0`. A claim pinned to an older version needs re-verification or a banner.
- Every cross-reference: the target exists **and** still says what the citing page claims.

**Each task ends with:**

```bash
python3 scripts/check_docs_links.py    # must exit 0
uv run pytest tests/test_docs_links.py -v
```

and a commit, plus a **report table** in the task report:

| File | Verdict | Evidence / what changed |
|---|---|---|

---

### Task 2: Sweep the operator guides

**Files (modify in place):**
- `docs/getting-started.md`
- `docs/onboarding-models.md`
- `docs/langfuse-per-app-projects.md`
- `docs/dev-references/admin-setup.md`
- `docs/help/creating-a-sample-application.md`
- `docs/help/choosing-app-settings.md`

**Interfaces:**
- Consumes: the gate from Task 1.
- Produces: a verdict table. Files stay at their current paths.

- [ ] **Step 1: Run the sweep procedure on all six files**

Focus areas for this bucket:
- **Admin endpoints** against `app/api/admin_routes.py` — every `/admin/*` path, method, and required field. `admin-setup.md` is 328 lines of HTTP reference and is the highest-risk file here.
- **Onboarding flow** against `app/apps/registry.py` (`ModelRegistry`) and the onboarding admin routes: the probe (`GET {endpoint}/models` contains the slug), `allowed_app_ids` semantics, the `enabled`/`probe_status` fields.
- **Config keys** against `config/serving.yaml`, `config/policy.yaml`, `config/routing.yaml`, `config/pricing.yaml` and `app/config/schema.py`.
- **`choosing-app-settings.md` recipes** against the write-time clamps in `app/api/admin_routes.py:127-132,497` — this file recommends concrete settings, so a stale recipe actively misleads. Check every recipe against the current `policy_mode` clamps.
- **Langfuse per-app provisioning** against `app/obs/` and the `LANGFUSE_ORG_*` env contract.

- [ ] **Step 2: Run the gate**

Run: `python3 scripts/check_docs_links.py`
Expected: exit 0

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -F - <<'MSG'
docs(sweep): verify the operator guides against HEAD

getting-started, onboarding-models, langfuse-per-app-projects, admin-setup,
creating-a-sample-application, choosing-app-settings — checked against the
admin routes, the ModelRegistry onboarding flow, the four config YAMLs, and
the post-PR-#34 write-time clamps.

<one line per file that changed, with the claim that was wrong>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

### Task 3: Sweep the developer & console guides

**Files (modify in place):**
- `docs/dev-references/agentic-app-guide.md`
- `docs/help/integrating-an-app.md`
- `docs/help/exporting-eval-datasets.md`
- `docs/help/ui-guide.md`
- `docs/help/reading-the-dashboard-and-trace.md`
- `docs/help/faq.md`

**Interfaces:**
- Consumes: the gate from Task 1.
- Produces: a verdict table.

- [ ] **Step 1: Run the sweep procedure on all six files**

Focus areas for this bucket:
- **`ui-guide.md` (727 lines, the largest in the set)** against `ui/src/console/` — every panel, tab, and column it describes must still exist. This file also historically carried `local-8b` cost tables; verify they are gone.
- **`reading-the-dashboard-and-trace.md`** already carries a banner saying its screenshots predate the B10/B11/B12 fixes. Verify that banner is still accurate and complete — if more values have drifted since, extend it. This file is a **banner** candidate, not an **edit** candidate: re-deriving every number is not worth it.
- **Tool calling** in `agentic-app-guide.md` and `integrating-an-app.md` against `tests/test_openai_compat.py` and `app/api/routes.py`. The client-driven loop (`tools` in request → `finish_reason: "tool_calls"` → app replays) is the supported path. Per **B25**, any text implying RouteForge executes an application's own tools is wrong and must be corrected — the internal NAT agent is a routing demonstration with three built-in tools, two of which are labelled FAKE in `nat_tools/src/nat_tools/tools.py:90,129`.
- **`exporting-eval-datasets.md`** against `tools/prompt_opt/dataset.py` and the `capture_io` behaviour in `app/obs/export.py`.
- **`faq.md`** — every answer touching residency/PII must reflect the `policy_mode` split.

- [ ] **Step 2: Run the gate**

Run: `python3 scripts/check_docs_links.py`
Expected: exit 0

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -F - <<'MSG'
docs(sweep): verify the developer & console guides against HEAD

agentic-app-guide, integrating-an-app, exporting-eval-datasets, ui-guide,
reading-the-dashboard-and-trace, faq — checked against ui/src/console/, the
OpenAI-compat tests, and the capture_io export path. Tool-execution language
corrected per B25: the client-driven loop is the supported path.

<one line per file that changed>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

### Task 4: Sweep the reference set

**Files (modify in place):**
- `docs/dev-references/api-reference.md`
- `docs/per-app-config.md`
- `docs/routing-observability.md`

**Interfaces:**
- Consumes: the gate from Task 1.
- Produces: a verdict table.

These three are **lookup surfaces** — a wrong field here breaks an integration directly, so they get the most literal check of any bucket.

- [ ] **Step 1: Run the sweep procedure on all three files**

Focus areas:
- **`api-reference.md`** — every request and response field against `app/api/routes.py` and the Pydantic models in `app/models.py`. Verify `Message.content` is documented as `str | None` (widened in `0546fab`). Verify the auth header is `Authorization: Bearer rf_live_…` and that `X-API-Key` is documented as *not* supported. Check the example `model` values are not `local-8b`.
- **`per-app-config.md`** — this is named in the spec as **the current authority on every clamp**, so it must be exactly right. Check every field (`policy_mode`, `pii_detection`, `pii_entity_denylist`, `pii_score_threshold`, `pii_exempt_patterns`, `stream_mode`, `capture_io`, `compression_enabled`, `residency`, `allowed_*`) against `app/apps/models.py` and the write-time clamps in `app/api/admin_routes.py:127-132,497`. Confirm the `09a28db` clamp removal and the PR #34 posture condition are both reflected.
- **`routing-observability.md`** — the four `routeforge_*` metric families against `app/obs/metrics.py`; `routing.stage_signals` default OFF against `config/routing.yaml`; the trace attributes `sp.candidates`, `sp.routing_policy_version`, `sp.prompt_version` against `app/obs/export.py`.

- [ ] **Step 2: Run the gate**

Run: `python3 scripts/check_docs_links.py`
Expected: exit 0

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -F - <<'MSG'
docs(sweep): verify the reference set against HEAD

api-reference, per-app-config, routing-observability — every field, endpoint
and metric name checked literally against app/api/, app/apps/models.py,
app/obs/ and the config YAMLs. per-app-config is the named authority on the
clamp set, so it gets the strictest pass.

<one line per file that changed>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

### Task 5: Sweep the architecture current-state docs

**Files (modify in place):**
- `docs/architecture/routeforge-logical-and-request-response-architecture.md`
- `docs/architecture/routeforge-sample-request-flows.md`
- `docs/architecture/routing-tier-selection.md`
- `docs/architecture/api-load-balancing.md`
- `docs/architecture/README.md`
- `docs/architecture/diagrams/README.md`

**Interfaces:**
- Consumes: the gate from Task 1.
- Produces: a verdict table. `architecture/README.md` is only *checked* here — Task 13 rewrites it as the bucket index.

This bucket carries the strictest standard in the taxonomy: **every claim must be checkable against HEAD** (spec §4). A claim that isn't belongs in `roadmap/`, and the report must say so.

- [ ] **Step 1: Run the sweep procedure on all six files**

Focus areas:
- Both large files carry "verified against the code at api:0.18.0" markers. **HEAD is 0.24.0.** Re-verify or re-date every status marker; a marker that cannot be re-verified becomes a banner.
- **`routing-tier-selection.md`** against `app/routing/router.py`, `app/routing/task_mode.py`, `config/routing.yaml`: tier = `max(length-based complexity, risk floor, escalation floor)` clamped to the best residency-permitted tier. Confirm the doc still says the router keys on **no** other dimension — not semantic difficulty, not modality, not capability.
- **`sample-request-flows.md`** — the three worked examples against `app/pipeline.py`. Confirm the per-call re-entry of compression (step 5) and the router (step 6) is described correctly on the multi-step path.
- **`api-load-balancing.md`** against the Helm chart in `deploy/` — the L4 Service and the two L7 proxies.
- **`diagrams/README.md`** — confirm the three `.drawio` files and the `specs/*.json` generators it names all still exist.
- Per **B25**, check whether any diagram description shows RouteForge calling a tool transparently to the application. If so, correct the text to label it the internal demo agent.

- [ ] **Step 2: Run the gate**

Run: `python3 scripts/check_docs_links.py`
Expected: exit 0

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -F - <<'MSG'
docs(sweep): verify the architecture current-state docs against HEAD

logical-and-request-response, sample-request-flows, routing-tier-selection,
api-load-balancing, and the two READMEs — re-verified from the api:0.18.0
markers to HEAD (0.24.0) against app/pipeline.py, app/routing/ and the Helm
chart. Claims that could not be re-verified carry a dated banner.

<one line per file that changed>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

### Task 6: Sweep the decision docs and binding specs

**Files (modify in place):**
- `docs/architecture/inference-layer-boundary.md`
- `docs/architecture/compression-in-scope.md`
- `docs/architecture/app-vs-routeforge-boundary.md`
- `docs/architecture/self-improving-agent-feature-fit.md`
- `docs/architecture/per-app-config-flexibility.md`
- `docs/architecture/semantic-cache-residency-spec.md`
- `docs/architecture/persistent-memory-residency-spec.md`

**Interfaces:**
- Consumes: the gate from Task 1.
- Produces: a verdict table.

Decision docs are judged differently from the rest: **a decision does not go stale because time passed.** It goes stale only if the decision was reversed or its stated rationale is now false. Preserve the original reasoning; correct only the facts it rests on. A superseded decision gets a banner naming what replaced it — never a silent rewrite.

- [ ] **Step 1: Run the sweep procedure on all seven files**

Focus areas:
- **The two `*-residency-spec.md` files are binding contracts cited from application code** (spec §5.5, §8.2). Each numbered guarantee (C1…Cn) must be checked against its call site: `app/cache/store.py`, `app/pipeline.py` (`_serve_from_cache`, `self._cache.lookup`, `self._cache.store`), `app/memory/store.py`, `app/config/lint.py:220`, `app/main.py:139`. Both features are **default OFF** — confirm `config/serving.yaml` still says so (`semantic_cache.enabled: false`, and the memory flag). If a guarantee is no longer honored by its call site, that is a **code bug, not a doc bug** — record it for the backlog, do not weaken the spec to match.
- **`per-app-config-flexibility.md`** already carries a supersession banner added on 2026-09-05. Verify it is complete and that the body's superseded sections are all marked.
- **`self-improving-agent-feature-fit.md`** is cited from `CLAUDE.md` as the authority for the scope decision. Confirm the boundary it states — orchestrated, not hosted — still matches CLAUDE.md rule #4.
- **`app-vs-routeforge-boundary.md`** is new (2026-09-06). Verify its claim that provider-side `cache_control` / `prompt_cache_key` markers pass through and are **stripped today** — check the gateway path in `app/gateway/`.

- [ ] **Step 2: Run the gate**

Run: `python3 scripts/check_docs_links.py`
Expected: exit 0

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -F - <<'MSG'
docs(sweep): verify the decision docs and binding specs against HEAD

Five decision docs plus the semantic-cache and persistent-memory residency
specs. Each numbered guarantee in the two specs re-checked against its call
site in app/cache/, app/memory/, app/pipeline.py and app/config/lint.py; both
features confirmed still default OFF.

Decisions were judged on whether they were reversed, not on age — original
reasoning preserved, superseding changes recorded as banners.

<one line per file that changed>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

### Task 7: Sweep the comparisons and roadmap docs

**Files (modify in place):**
- `docs/architecture/nemo-switchyard-comparison.md`
- `docs/architecture/switchyard-features-to-incorporate.md`
- `docs/architecture/external-router-feature-analysis.md`
- `docs/architecture/caching-cost-feature-fit.md`
- `docs/architecture/ai-governance-feature-fit.md`
- `docs/rlhf-readiness.md`
- `docs/architecture/ip-and-research-opportunities.md`
- `docs/architecture/ip-review-handoff.md`
- `docs/observability-metrics.md`

**Interfaces:**
- Consumes: the gate from Task 1.
- Produces: a verdict table.

These are **parked analyses**. The check is narrower than elsewhere: verify the claims each doc makes **about RouteForge** (those are checkable), and confirm each doc's stated status is still true. Claims about external tools are point-in-time by nature and are not re-researched.

- [ ] **Step 1: Run the sweep procedure on all nine files**

Focus areas:
- Every ✅/🟡/❌ status table claiming "we already do this" must be re-checked against code. These tables are the part that rots, and `caching-cost-feature-fit.md` and `ai-governance-feature-fit.md` both state their statuses are "confirmed against the code".
- **`rlhf-readiness.md`** carries updates saying F1–F5 shipped across `0.11.0` and F1 Phase D. Verify against `tools/recommend/`, `tools/prompt_opt/`, and `app/obs/export.py`. This doc is mostly-shipped and may warrant a status banner at the top saying so.
- **`ip-and-research-opportunities.md`** carries a verification caveat that its arXiv IDs are leads, not citations. **Do not attempt to verify the arXiv IDs** — that caveat is the doc's own honest disclosure and stays. Verify only the repo-inventory claims in it.
- **`observability-metrics.md`** — confirm it is genuinely target-state (it catalogs what RouteForge *should* surface). If parts of it now describe shipped metrics, note which, so Task 13's `roadmap/README.md` can say so accurately. Task 10 renames it to `observability-metrics-target-state.md`.
- **`nemo-switchyard-comparison.md`** — confirm the replace-decision is still **deferred, not rejected**.

- [ ] **Step 2: Run the gate**

Run: `python3 scripts/check_docs_links.py`
Expected: exit 0

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -F - <<'MSG'
docs(sweep): verify the comparison and roadmap analyses against HEAD

Five external-tool comparisons plus rlhf-readiness, the two IP docs and the
observability metrics catalog. Re-checked every "we already do this" status
claim against code; left point-in-time claims about external tools alone, and
left the arXiv-ID caveat in ip-and-research-opportunities standing as written.

<one line per file that changed>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

### Task 8: Sweep the marketing docs

**Files (modify in place):**
- `docs/marketing/positioning.md`
- `docs/marketing/routeforge-demo-talking-points.md`
- `docs/demo-dashboard-notes.md`

**Interfaces:**
- Consumes: the gate from Task 1.
- Produces: a verdict table.

- [ ] **Step 1: Run the sweep procedure on all three files**

Focus areas:
- **The demo walkthrough must start from an empty pool.** CLAUDE.md's walkthrough section is explicit: the operator onboards their own endpoint before step 1, because there is no bundled model. Any talking point implying a model is already present is wrong.
- The **two savings levers** (routing vs. compression) must be described as tracked separately.
- `positioning.md` mentions routing across "model capability, quantization, inference engine, endpoint capacity, GPU availability". Per `routing-tier-selection.md`, the router keys on **none** of those — it keys on length-based complexity, risk floor, escalation floor, and residency. **This is a probable overclaim; verify and correct it.**
- `demo-dashboard-notes.md` describes `POST /api/traffic` and the persona mix — check against `tests/traffic/generator.py` and `tests/personas.yaml`.

- [ ] **Step 2: Run the gate**

Run: `python3 scripts/check_docs_links.py`
Expected: exit 0

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -F - <<'MSG'
docs(sweep): verify the marketing and demo docs against HEAD

positioning, demo-talking-points, demo-dashboard-notes — checked the demo
narrative starts from an empty on-prem pool, that routing and compression
savings are described as separate levers, and that positioning claims match
what the step-6 router actually keys on.

<one line per file that changed>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

## Task 9: Consolidate the three backlogs

**Files:**
- Modify: `docs/bugs-backlog.md` (absorbs live items; moved to `backlog/bugs.md` in Task 10)
- Read: `docs/ui-open-items.md`, `docs/eval-logging-backlog.md`

**Interfaces:**
- Consumes: the gate from Task 1.
- Produces: `docs/bugs-backlog.md` containing every live item from all three sources. Task 10 moves it.

**The one irreversible risk in this plan is losing a live item here.** Verification is item-by-item and is recorded, never assumed.

- [ ] **Step 1: Enumerate every item in the two candidate-for-archive backlogs**

```bash
grep -nE "^## " docs/ui-open-items.md
grep -nE "^## " docs/eval-logging-backlog.md
```

- [ ] **Step 2: Verify each item's closed status against the code**

For **each** heading, do not trust the heading's own status text — confirm against code or a commit.

Present belief from the 2026-09-09 survey, to be confirmed or refuted:
- `ui-open-items.md` — all 7 items marked DONE or FIXED IN CODE. Items 3, 4, 5 say "FIXED IN CODE (pending :0.4.0)"; HEAD is far past `0.4.0`, so confirm each shipped.
- `eval-logging-backlog.md` — marked BUILT 2026-07-29 with a "What was built" section covering both sub-items.

Produce this table in the task report — it is the audit trail:

| Source | Item | Claimed status | Verified how | Live? |
|---|---|---|---|---|

- [ ] **Step 3: Migrate anything live into `docs/bugs-backlog.md`**

If Step 2 finds a live item, add it to `docs/bugs-backlog.md` under a new heading following the existing `B<n>` convention, preserving the original write-up and noting the source file. If nothing is live — the expected outcome — make no content change and say so explicitly in the report.

- [ ] **Step 4: Add a consolidation note to `docs/bugs-backlog.md`**

Replace the `Sibling backlogs: ...` line near the top with a note that this is now the single list, naming where the other two went and stating that each of their items was verified closed.

- [ ] **Step 5: Run the gate**

Run: `python3 scripts/check_docs_links.py`
Expected: exit 0

- [ ] **Step 6: Commit**

```bash
git add docs/
git commit -F - <<'MSG'
docs(backlog): consolidate three backlogs into one list

Verified every item in ui-open-items.md (7) and eval-logging-backlog.md
item-by-item against code rather than trusting the headings — the audit table
is in the task report. <N live items migrated | No live item was lost>.

bugs-backlog.md is now the single list; the other two are archived in the
following commit.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

## Task 10: The structural moves

**Files:** all 45 documents. **`git mv` only — zero content edits in this commit.**

**Interfaces:**
- Consumes: the swept content from Tasks 2–9.
- Produces: the final tree. Tasks 11–13 repair what this breaks.

The gate **will fail** at the end of this task — that is expected and correct. Tasks 11–12 repair it. Do not "fix" links here; mixing renames with edits is exactly what this task exists to avoid.

- [ ] **Step 1: Create the directories**

```bash
cd /home/manimal/projects/routeForge
mkdir -p docs/guides docs/reference docs/architecture/decisions \
         docs/architecture/specs docs/comparisons docs/roadmap \
         docs/backlog docs/archive
```

- [ ] **Step 2: Move the guides (12)**

```bash
git mv docs/getting-started.md                        docs/guides/getting-started.md
git mv docs/onboarding-models.md                      docs/guides/onboarding-models.md
git mv docs/langfuse-per-app-projects.md              docs/guides/langfuse-per-app-projects.md
git mv docs/dev-references/admin-setup.md             docs/guides/admin-setup.md
git mv docs/dev-references/agentic-app-guide.md       docs/guides/agentic-app-guide.md
git mv docs/help/choosing-app-settings.md             docs/guides/choosing-app-settings.md
git mv docs/help/creating-a-sample-application.md     docs/guides/creating-a-sample-application.md
git mv docs/help/integrating-an-app.md                docs/guides/integrating-an-app.md
git mv docs/help/exporting-eval-datasets.md           docs/guides/exporting-eval-datasets.md
git mv docs/help/ui-guide.md                          docs/guides/ui-guide.md
git mv docs/help/reading-the-dashboard-and-trace.md   docs/guides/reading-the-dashboard-and-trace.md
git mv docs/help/faq.md                               docs/guides/faq.md
```

- [ ] **Step 3: Move the reference set (3)**

```bash
git mv docs/dev-references/api-reference.md  docs/reference/api-reference.md
git mv docs/per-app-config.md                docs/reference/per-app-config.md
git mv docs/routing-observability.md         docs/reference/routing-observability.md
```

- [ ] **Step 4: Rename the two architecture current-state docs (2)**

```bash
git mv docs/architecture/routeforge-logical-and-request-response-architecture.md \
       docs/architecture/logical-and-request-flow.md
git mv docs/architecture/routeforge-sample-request-flows.md \
       docs/architecture/sample-request-flows.md
```

`routing-tier-selection.md`, `api-load-balancing.md`, `README.md` and `diagrams/` stay put.

- [ ] **Step 5: Move the decision docs (5) and binding specs (2)**

```bash
git mv docs/architecture/inference-layer-boundary.md          docs/architecture/decisions/
git mv docs/architecture/compression-in-scope.md              docs/architecture/decisions/
git mv docs/architecture/app-vs-routeforge-boundary.md        docs/architecture/decisions/
git mv docs/architecture/self-improving-agent-feature-fit.md  docs/architecture/decisions/
git mv docs/architecture/per-app-config-flexibility.md        docs/architecture/decisions/

git mv docs/architecture/semantic-cache-residency-spec.md     docs/architecture/specs/
git mv docs/architecture/persistent-memory-residency-spec.md  docs/architecture/specs/
```

- [ ] **Step 6: Move the comparisons (5)**

```bash
git mv docs/architecture/nemo-switchyard-comparison.md        docs/comparisons/
git mv docs/architecture/switchyard-features-to-incorporate.md docs/comparisons/
git mv docs/architecture/external-router-feature-analysis.md  docs/comparisons/
git mv docs/architecture/caching-cost-feature-fit.md          docs/comparisons/
git mv docs/architecture/ai-governance-feature-fit.md         docs/comparisons/
```

- [ ] **Step 7: Move the roadmap docs (4)**

```bash
git mv docs/rlhf-readiness.md                              docs/roadmap/rlhf-readiness.md
git mv docs/architecture/ip-and-research-opportunities.md  docs/roadmap/ip-and-research-opportunities.md
git mv docs/architecture/ip-review-handoff.md              docs/roadmap/ip-review-handoff.md
git mv docs/observability-metrics.md                       docs/roadmap/observability-metrics-target-state.md
```

- [ ] **Step 8: Move the backlog, marketing addition, and archive (7)**

```bash
git mv docs/bugs-backlog.md         docs/backlog/bugs.md
git mv docs/demo-dashboard-notes.md docs/marketing/demo-dashboard-notes.md

git mv docs/architecture-review.md    docs/archive/2026-07-25-architecture-review.md
git mv docs/ui-open-items.md          docs/archive/ui-open-items.md
git mv docs/eval-logging-backlog.md   docs/archive/eval-logging-backlog.md
```

- [ ] **Step 9: Delete the absorbed index and the emptied directories**

```bash
git rm docs/dev-references/README.md
rmdir docs/dev-references docs/help   # must already be empty; if not, something was missed
```

If `rmdir` fails, a file was not moved. Find it with `ls docs/dev-references docs/help` and reconcile against §5 of the spec before continuing.

- [ ] **Step 10: Verify the moves preserved history and nothing was dropped**

```bash
# docs/ root must now hold only directories
ls -p docs/ | grep -v /

# history must follow through the rename
git log --follow --oneline -- docs/guides/onboarding-models.md | head -3
git log --follow --oneline -- docs/reference/per-app-config.md | head -3

# count must still be 45 non-superpowers docs
find docs -name "*.md" -not -path "docs/superpowers/*" | wc -l
```

Expected: the first command prints nothing (no loose files); both `git log --follow` calls show history predating the move; the count is **44** (45 minus the deleted `dev-references/README.md`).

- [ ] **Step 11: Remove the Task 1 temporary vocabulary exemptions**

If Task 1 Step 6 added specific file paths to `VOCAB_EXEMPT`, remove them now — those files are in `docs/archive/` and are covered by the directory-level exemption.

- [ ] **Step 12: Commit (the gate is expected to fail; Tasks 11–12 repair it)**

```bash
git add -A
git commit -F - <<'MSG'
docs: move all 45 documents into the new taxonomy

git mv only — no content changes in this commit, so the renames stay
reviewable on their own.

guides/ (12) · reference/ (3) · architecture/ + decisions/ (5) + specs/ (2) ·
comparisons/ (5) · roadmap/ (4) · backlog/bugs.md · marketing/ (+1) ·
archive/ (3). dev-references/README.md deleted, absorbed into the new indexes.

Links are broken as of this commit and are repaired in the next two.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

## Task 11: Repair the intra-docs links

**Files:** every `.md` under `docs/` except `docs/superpowers/`.

**Interfaces:**
- Consumes: the moved tree from Task 10, the gate from Task 1.
- Produces: `check_internal()` returning zero problems.

- [ ] **Step 1: List what is broken**

```bash
python3 scripts/check_docs_links.py 2>&1 | grep '^INTERNAL'
```

- [ ] **Step 2: Repair each broken link**

Re-point each to the target's new path from spec §5, relative to the **new** location of the citing file. A guide linking a sibling guide is now `./faq.md`; a guide linking the reference set is now `../reference/per-app-config.md`.

Do this by reading and editing, not by blind `sed` — a `sed` over 136 links will corrupt any link whose text happens to match a path fragment.

Watch for the two renames: `routeforge-logical-and-request-response-architecture.md` → `architecture/logical-and-request-flow.md`, and `observability-metrics.md` → `roadmap/observability-metrics-target-state.md`.

- [ ] **Step 3: Run the gate**

Run: `python3 scripts/check_docs_links.py 2>&1 | grep '^INTERNAL'`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add docs/
git commit -F - <<'MSG'
docs: repair intra-docs links after the move

All relative markdown links re-pointed to the new taxonomy. Internal check of
the docs gate is clean; the inbound check is repaired in the next commit.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

## Task 12: Repair the inbound references

**Files:** the ~30 source files in spec §8.2 — code, config, skills, and the two root markdown files.

**Interfaces:**
- Consumes: the moved tree, the gate.
- Produces: `check_inbound()` returning zero problems.

- [ ] **Step 1: List what is broken**

```bash
python3 scripts/check_docs_links.py 2>&1 | grep '^INBOUND'
```

- [ ] **Step 2: Repair by citation group**

Work through spec §8.2's table. The high-count groups:

| Cited doc → new path | Files to update |
|---|---|
| `docs/onboarding-models.md` → `docs/guides/onboarding-models.md` | `CLAUDE.md` (5 refs), `README.md`, `.claude/skills/{greenlake-deploy,inference-backends,routing-and-gateway}/SKILL.md`, `app/gateway/backends/README.md`, `config/serving.yaml`, `deploy/compose/docker-compose.yml`, `deploy/spark/{README.md,docker-compose.yml}`, `tools/recommend/rules.py` |
| `docs/architecture/semantic-cache-residency-spec.md` → `docs/architecture/specs/…` | `app/cache/store.py`, `app/config/lint.py`, `app/config/schema.py`, `app/main.py`, `app/models.py`, `app/pipeline.py`, `config/serving.yaml`, `tests/test_semantic_cache_{store,pipeline}.py` |
| `docs/architecture/persistent-memory-residency-spec.md` → `docs/architecture/specs/…` | `app/memory/__init__.py`, `app/memory/store.py`, `app/config/schema.py`, `config/serving.yaml`, `tests/test_memory_{store,pipeline}.py` |
| `docs/per-app-config.md` → `docs/reference/per-app-config.md` | `CLAUDE.md`, `app/api/admin_routes.py`, `ui/src/api.ts`, `tests/test_streaming.py`, `.env.example` |
| `docs/routing-observability.md` → `docs/reference/routing-observability.md` | `CLAUDE.md` |
| `docs/architecture/self-improving-agent-feature-fit.md` → `docs/architecture/decisions/…` | `CLAUDE.md` |
| `docs/architecture/per-app-config-flexibility.md` → `docs/architecture/decisions/…` | `tests/test_streaming.py` |
| `docs/langfuse-per-app-projects.md` → `docs/guides/…` | `.env.example`, `deploy/dgx_lab/routeforge-app.yaml` |
| `docs/help/choosing-app-settings.md` → `docs/guides/…` | `ui/src/admin/AppSettings.tsx` |
| `docs/help/exporting-eval-datasets.md` → `docs/guides/…` | `tools/prompt_opt/dataset.py` |
| `docs/help/integrating-an-app.md` → `docs/guides/…` | `examples/client/README.md`, `tests/traffic/README.md` |
| `docs/dev-references/` (dir) → `docs/guides/` + `docs/reference/` | `README.md` |

- [ ] **Step 3: Handle the three archive-bound citations specially**

Three code comments cite docs that moved to `archive/`. Per spec §8.2, each is repointed to the archive path **and** to the live successor — a comment pointing only at an archived doc is worse than no comment.

| File | Cites | Repoint to |
|---|---|---|
| `app/apps/bootstrap.py` | `docs/architecture-review.md` | `docs/archive/2026-07-25-architecture-review.md`, plus `docs/architecture/` for current state |
| `app/obs/export.py`, `app/agent/runtime.py`, `app/api/routes.py`, `app/config/schema.py`, `config/serving.yaml`, `tests/test_capture_io.py` | `docs/eval-logging-backlog.md` | `docs/archive/eval-logging-backlog.md`, plus `docs/reference/per-app-config.md` for the live `capture_io` contract |
| `ui/src/admin/clipboard.tsx`, `tests/test_history_store.py` | `docs/ui-open-items.md` | `docs/archive/ui-open-items.md`, plus `docs/backlog/bugs.md` |

- [ ] **Step 4: Update `CLAUDE.md`'s nine doc references**

```bash
grep -n "docs/" CLAUDE.md
```

Update each to its §5 destination. `CLAUDE.md` is the highest-traffic file in the repo — a stale path here is the most expensive one in the set.

- [ ] **Step 5: Run the gate and the test suite**

```bash
python3 scripts/check_docs_links.py
uv run pytest tests/ -q
```

Expected: gate exits 0; pytest passes. Four test files and `app/apps/bootstrap.py` carry doc paths in comments, so a broken edit shows up here.

Set `LITELLM_LOCAL_MODEL_COST_MAP=True` before pytest, or the suite wedges in D-state on litellm's network cost-map fetch at import.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -F - <<'MSG'
docs: repoint every inbound reference to the new taxonomy

~30 source files: CLAUDE.md (9 refs), README.md, three skills, four deploy and
config files, and the application/test comments that cite a doc by path.

Comments citing a now-archived doc get both paths — the archive path and the
live successor — since a pointer to an archived-only doc is worse than none.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

## Task 13: Author the index files

**Files:**
- Create: `docs/README.md`, `docs/guides/README.md`, `docs/comparisons/README.md`, `docs/roadmap/README.md`, `docs/archive/README.md`
- Modify: `docs/architecture/README.md` (rewrite as bucket index)

**Interfaces:**
- Consumes: the verdict tables from Tasks 2–9 — the status column in each index comes from those, not from guesswork.
- Produces: the entry point.

- [ ] **Step 1: Write `docs/README.md`**

Structure, in order:

1. One sentence on what RouteForge is, linking `../CLAUDE.md` and `../README.md`.
2. **"Start here" routed by role** — four rows, each naming one first document:
   - *Operator setting RouteForge up* → `guides/getting-started.md`
   - *Developer integrating an app* → `guides/integrating-an-app.md`
   - *Engineer evaluating the design* → `architecture/logical-and-request-flow.md`
   - *Contributor* → `../CLAUDE.md`, then `backlog/bugs.md`
3. **The bucket table** — one row per bucket: what's in it, and **how current it is**. This is the load-bearing part of the whole reorganization; state plainly that `architecture/` is verified against HEAD, `roadmap/` is not built, and `archive/` is superseded.
4. A closing note that `superpowers/` is the SDD build history — how the product was built, not what is true now.

Do not list all 44 files here. The bucket READMEs do that.

Use this skeleton, filling the italicised parts from the sweep verdicts:

```markdown
# RouteForge documentation

RouteForge is a self-hostable, OpenAI-compatible, multi-tenant model router and
policy gateway. For the product's rules and module architecture, read
[`../CLAUDE.md`](../CLAUDE.md); for a two-minute overview, [`../README.md`](../README.md).

## Start here

| You are | Read first |
|---|---|
| An **operator** setting RouteForge up | [`guides/getting-started.md`](guides/getting-started.md) |
| A **developer** integrating an application | [`guides/integrating-an-app.md`](guides/integrating-an-app.md) |
| An **engineer** evaluating the design | [`architecture/logical-and-request-flow.md`](architecture/logical-and-request-flow.md) |
| A **contributor** | [`../CLAUDE.md`](../CLAUDE.md), then [`backlog/bugs.md`](backlog/bugs.md) |

## What's in each directory

**The second column is the point of this layout: the path tells you how much to
trust the page.**

| Directory | Contents | How current |
|---|---|---|
| [`guides/`](guides/README.md) | Task-oriented how-to for operators and app developers | Verified against HEAD _(date)_ |
| [`reference/`](reference/) | Field, endpoint and metric lookup | Verified against HEAD _(date)_ |
| [`architecture/`](architecture/README.md) | How RouteForge works today | **Every claim checkable against HEAD** |
| [`architecture/decisions/`](architecture/README.md) | Scope and boundary calls — why RouteForge won't do X | Dated; superseded ones say so |
| [`architecture/specs/`](architecture/README.md) | Binding contracts for shipped features, cited from code | Verified against their call sites |
| [`comparisons/`](comparisons/README.md) | RouteForge vs. external tools | Point-in-time analysis; see each doc's date |
| [`roadmap/`](roadmap/README.md) | Possible future work | **Nothing here is shipped** |
| [`backlog/`](backlog/bugs.md) | The single list of open defects and gaps | Live |
| [`marketing/`](marketing/) | Positioning and demo narration | — |
| [`archive/`](archive/README.md) | Superseded and point-in-time documents | **Historical — do not rely on these** |

## Build history

[`superpowers/`](superpowers/) holds the specs and task plans for how RouteForge
was built, written by the SDD workflow as work happened. It is an append-only
record of *how we got here* — **not a description of what is true now.** When it
disagrees with `architecture/`, `architecture/` wins.
```

- [ ] **Step 2: Write `docs/guides/README.md`**

Two ordered reading paths, inherited from the deleted `dev-references/README.md`:
- **Admin/operator:** getting-started → onboarding-models → creating-a-sample-application → choosing-app-settings → admin-setup → langfuse-per-app-projects
- **Application developer:** integrating-an-app → `../reference/api-reference.md` → agentic-app-guide → exporting-eval-datasets

Then the console pages (`ui-guide`, `reading-the-dashboard-and-trace`) and `faq`.

- [ ] **Step 3: Rewrite `docs/architecture/README.md`**

Keep from the existing file: the canonical step-count note (**steps 1–10**) and the diagram pointers. Add: what distinguishes `architecture/` (verifiable against HEAD) from `decisions/` (a scope call, dated) and `specs/` (a binding contract cited from code). List the files in each with a one-line summary. Move the boundary/comparison pointers that now live in other buckets to point at their new paths.

- [ ] **Step 4: Write `docs/comparisons/README.md`**

One row per doc: which external tool, what was decided, and whether the decision is still open. The Switchyard replace-decision is **deferred, not rejected** — say so, since that distinction is the whole point of keeping the doc.

- [ ] **Step 5: Write `docs/roadmap/README.md`**

Open with one sentence: **nothing in this directory is shipped.** Then one row per doc with its status, using the Task 7 verdicts — `rlhf-readiness.md` in particular describes substrate that has largely shipped, so its row must say so rather than implying it is all future work.

- [ ] **Step 6: Write `docs/archive/README.md`**

One row per file: what it was, why it is archived, what superseded it. Include the archive date.

- [ ] **Step 7: Run the gate**

```bash
python3 scripts/check_docs_links.py
```

Expected: exit 0. The new READMEs add many links; this catches typos in them.

- [ ] **Step 8: Commit**

```bash
git add docs/
git commit -F - <<'MSG'
docs: add the index files

docs/README.md routes by role and states how current each bucket is —
architecture/ is verified against HEAD, roadmap/ is not built, archive/ is
superseded. That status column is the point of the reorganization.

Plus bucket indexes for guides/, architecture/, comparisons/, roadmap/ and
archive/. Status columns come from the sweep verdicts, not from guesswork.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
MSG
```

---

## Task 14: The acceptance gate and PR

**Files:** none modified unless the gate finds something.

**Interfaces:**
- Consumes: everything.
- Produces: a merged PR.

- [ ] **Step 1: Verify all seven acceptance criteria from spec §11**

```bash
cd /home/manimal/projects/routeForge

# 1. docs/README.md exists and links every bucket
test -f docs/README.md && grep -cE "guides/|reference/|architecture/|comparisons/|roadmap/|backlog/|archive/" docs/README.md

# 2. docs/ root holds only README.md; 44 docs outside superpowers/
ls -p docs/ | grep -v /
find docs -name "*.md" -not -path "docs/superpowers/*" | wc -l

# 5. the gate
python3 scripts/check_docs_links.py

# 6. the suite
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/ -q

# 7. history follows every moved file
for f in docs/guides/onboarding-models.md docs/reference/per-app-config.md \
         docs/architecture/specs/semantic-cache-residency-spec.md \
         docs/comparisons/nemo-switchyard-comparison.md \
         docs/roadmap/rlhf-readiness.md docs/backlog/bugs.md \
         docs/archive/ui-open-items.md; do
  printf "%-60s %s\n" "$f" "$(git log --follow --oneline -- "$f" | wc -l)"
done
```

Expected: `docs/` root prints nothing loose; count is **44**; gate exits 0; pytest passes; every `git log --follow` count is **> 1** (a count of 1 means history was lost — the file was added rather than moved).

Criteria 3 and 4 are verified from the task reports: every document has a recorded verdict, and Task 9's audit table states no live item was lost.

- [ ] **Step 2: Confirm no stale basename remains anywhere**

```bash
for b in bugs-backlog ui-open-items eval-logging-backlog architecture-review \
         demo-dashboard-notes observability-metrics rlhf-readiness \
         routeforge-sample-request-flows routeforge-logical-and-request-response-architecture; do
  hits=$(grep -rl -- "docs/$b" . 2>/dev/null \
    | grep -vE "^\./(\.git|\.venv|\.worktrees|node_modules)/" \
    | grep -v "__pycache__" | grep -v "^\./docs/superpowers/")
  [ -n "$hits" ] && { echo "STALE: docs/$b"; echo "$hits" | sed 's/^/    /'; }
done
echo "(no STALE lines above = clean)"
```

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin docs/reclassification
```

Then create the PR against `main` via the GitHub REST API (the `gh` CLI is not installed; the token is in `~/.git-credentials`, read it without printing it). Title:

`docs: reclassify docs/ by reader question and epistemic status`

Body must cover: the taxonomy and its rule, the sweep results (how many **current** / **edit** / **banner** / **archive**), the backlog-consolidation audit result, and the gate output. Link the spec. End with the two attribution lines.

- [ ] **Step 4: Report before merging**

Do **not** auto-merge. Report to the user: the verdict counts, anything the sweep found that became a code bug rather than a doc fix (spec §6 anticipates this for the residency specs), and the Task 9 audit result. Merge on their say-so.

---

## Follow-ups recorded, not done here

Per spec §10, out of scope for this plan:

- **11 stale `.worktrees/` directories** with matching `.git/worktrees/` entries, each holding a full stale copy of `docs/` and `CLAUDE.md`. They corrupt repo-wide greps and are excluded by the gate rather than fixed. A `git worktree prune` pass belongs on its own.
- **B24** — `task_mode.decide()` crashes on `content: null` → 500. Open, unrelated, and will keep surfacing during the sweep.
- Any guarantee the Task 6 sweep finds unhonored by its call site: that is a code bug and goes to `docs/backlog/bugs.md`, not a weakened spec.
