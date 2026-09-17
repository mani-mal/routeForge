# Remove the NeMo Agent Runtime and Multi-Step Path — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete RouteForge's internal NeMo Agent Toolkit (NAT) agent and the whole
multi-step branch, so every request is routed as a single direct call and agent loops
stay in the calling application.

**Architecture:** Step 4 (Task Mode Router) survives as a constant decision that always
returns `"direct"`, so the "steps 1–10" numbering and the console drill-down are
unchanged. (Step 4 emits no `DecisionRecord` today and this plan does not add one —
that belongs to P1a's decision certificate.) Everything reachable only
from `multi_step` — the agent runtime, the internal per-hop endpoint, the NAT tools
package, the step-4 LLM classifier, and the agent-loop stage signals — is deleted along
with its config, deployment values, tests, and docs.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, `uv`, pytest, React + Vite (console),
Helm.

**Spec:** `docs/superpowers/specs/2026-09-17-remove-nemo-agent-design.md` (read it first;
this plan argues from it).

## Global Constraints

- **Branch:** `feat/remove-nemo-agent` (already created; the spec is committed there).
- **Tests must be run with `LITELLM_LOCAL_MODEL_COST_MAP=True`,** or pytest wedges in
  D-state on litellm's network cost-map fetch. Every `pytest` command in this plan
  already includes it.
- **Step 4 stays in the architecture.** Do not renumber the ten steps, do not remove the
  `task_mode` step key from the trace, the console, or Langfuse.
- **No behavior change outside the agent path.** Client-driven tool calling, Headroom
  compression, CCR, the semantic cache, memory, streaming modes, the policy gate, and
  the two-lever savings accounting must all behave exactly as before.
- **No database change.** No migration, no `ALTER`.
- **Stale config keys must never fail the boot.** A deployed `serving.yaml`,
  `policy.yaml`, or Helm values file may still carry `agent:`, `agent_budgets:`, or
  `stage_signals:` after this change.
- **Commit after every task** with the trailer:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- **Do not touch** `docs/archive/`, earlier `docs/superpowers/` specs and plans, or
  `docs/new/` (those handoffs stay until the P1a spec supersedes them).

---

## File Structure

**Deleted outright**

| Path | Why |
|---|---|
| `app/agent/` (`runtime.py`, `internal_endpoint.py`, `openai_sse.py`, `__init__.py`) | the NAT agent and its per-hop endpoint |
| `app/routing/task_mode_llm.py` | step-4 LLM classifier; only labels were `direct` / `multi_step` |
| `app/routing/signals.py` | agent-loop stage signals; consumed only by the per-hop endpoint |
| `nat_tools/` | demo tools (`calculator`, and the canned `doc_search` / `crm_lookup`) |
| `config/nat_workflow.yaml`, `config/prompts/task_mode_classifier/` | NAT workflow + classifier prompt |
| `.claude/skills/nemo-agent-toolkit/` | skill for a subsystem that no longer exists |

**Modified**

| Path | Responsibility after the change |
|---|---|
| `app/routing/task_mode.py` | returns the constant `"direct"` |
| `app/pipeline.py` | direct + tool-conversation paths only; no `_run_multi_step`, no classifier |
| `app/main.py` | no `internal_router` |
| `app/api/routes.py` | no `AgentCapExceeded` mapping |
| `app/config/schema.py`, `app/config/lint.py` | no `StageSignalsCfg`, no `agent_budgets` |
| `config/routing.yaml`, `config/policy.yaml`, `config/serving.yaml` | no `stage_signals`, `agent_budgets`, `agent:` |
| `deploy/helm/serving-plane/*`, `deploy/hpe/routeforge/*` | no `servingPlane.agent` |
| `ui/src/api.ts`, `ui/src/console/RequestDrilldown.tsx` | step 4 described as always direct |
| `tests/` | agent tests deleted; mixed tests keep their non-agent cases |
| `pyproject.toml`, `Dockerfile` | no `nvidia-nat`, no `nat_tools` |
| docs, `CLAUDE.md`, skills | describe a router with no internal agent |

---

### Task 1: Step 4 always decides `direct`

Removes the trigger phrases, the `mock_multistep` read, and the optional LLM classifier.
After this task `mode` is always `"direct"`, so the `multi_step` branches below it are
dead code that Task 2 deletes.

**Files:**
- Modify: `app/routing/task_mode.py` (whole file)
- Delete: `app/routing/task_mode_llm.py`, `config/prompts/task_mode_classifier/`
- Modify: `app/pipeline.py:28-29` (imports), `:252-274` (`_classifier_enabled`,
  `_decide_task_mode`), `:447`, `:784` (call sites)
- Delete: `tests/test_task_mode_classifier.py`
- Modify: `tests/test_compression_and_mode.py:24-41`, `tests/test_prompt_store.py`,
  `tests/test_prompt_optimizer.py`
- Test: `tests/test_compression_and_mode.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `app.routing.task_mode.decide(ctx, request) -> str`, always `"direct"`.
  `Pipeline._decide_task_mode` and `Pipeline._classifier_enabled` no longer exist;
  both call sites now use `decide_mode(ctx, request)` directly and pass
  `classifier_record=None` nowhere — the parameter is gone.

- [ ] **Step 1: Replace the task-mode tests with the new contract**

In `tests/test_compression_and_mode.py`, delete the three `multi_step` tests
(`test_task_mode_multi_step_on_mock_multistep_flag`,
`test_task_mode_multi_step_on_trigger_phrase`,
`test_task_mode_multi_step_on_step_by_step_phrase`) and replace them with:

```python
def test_task_mode_is_direct():
    req = ChatRequest(messages=[Message(role="user", content="hi")])
    assert decide(CTX, req) == "direct"


def test_task_mode_direct_on_former_trigger_phrases():
    """"step by step" / "look up" used to divert to the internal NAT agent.
    Agent loops are client-driven now: every call routes direct."""
    for content in ("Solve this step by step: 2+2",
                    "Look up account 42 please"):
        req = ChatRequest(messages=[Message(role="user", content=content)])
        assert decide(CTX, req) == "direct"


def test_task_mode_direct_ignores_mock_multistep_flag():
    """`mock_multistep` was a demo-only hint; ChatRequest has extra="allow",
    so a client that still sends it is simply ignored."""
    req = ChatRequest(messages=[Message(role="user", content="hi")],
                      mock_multistep=True)
    assert decide(CTX, req) == "direct"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_compression_and_mode.py -v
```

Expected: the two new tests FAIL (`assert 'multi_step' == 'direct'`).

- [ ] **Step 3: Make step 4 a constant decision**

Replace the whole body of `app/routing/task_mode.py` with:

```python
from __future__ import annotations

from app.models import ChatRequest, ExecutionPolicyContext


def decide(ctx: ExecutionPolicyContext, request: ChatRequest) -> str:
    """Step 4 (Task Mode Router) — always "direct".

    RouteForge runs no agent of its own: tools belong to the calling
    application, which drives its own loop and sends each model call as a
    separate request (every one of them re-enters steps 2-9). The step stays
    in the architecture and in the console step list; as before, it emits no
    DecisionRecord of its own.
    """
    return "direct"
```

- [ ] **Step 4: Delete the step-4 LLM classifier**

```bash
git rm app/routing/task_mode_llm.py tests/test_task_mode_classifier.py
git rm -r config/prompts/task_mode_classifier
```

In `app/pipeline.py`: delete the `classify_task_mode` import (line 29), delete
`_classifier_enabled` and `_decide_task_mode` (~lines 249-274), and replace both call
sites so they use the rules decision directly.

At `app/pipeline.py:447` (inside `run`), replace:

```python
            mode, crec = await self._decide_task_mode(ctx, sanitized, request)
```

with:

```python
            mode, crec = decide_mode(ctx, request), None
```

At `app/pipeline.py:784` (inside `run_stream`), replace:

```python
        mode, crec = await self._decide_task_mode(ctx, sanitized, request)
```

with:

```python
        mode, crec = decide_mode(ctx, request), None
```

Keep `crec` (always `None`) for now — Task 2 removes it along with the `multi_step`
branches, so this task stays a small, independently reviewable step. Leave the
`prompt_opt` framework, `PromptOptCfg`, and `sp.prompt_version` in place.

- [ ] **Step 5: Rename the classifier id in the prompt-framework tests**

`tests/test_prompt_store.py` and `tests/test_prompt_optimizer.py` write artifacts into
`tmp_path`, so the id is arbitrary — but it must not imply a live artifact. Replace every
occurrence of the string `"task_mode_classifier"` with `"test_artifact"` in those two
files only (including the directory names they create), leaving `"verify_judge"` and all
other ids untouched.

```bash
sed -i 's/task_mode_classifier/test_artifact/g' tests/test_prompt_store.py tests/test_prompt_optimizer.py
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest \
  tests/test_compression_and_mode.py tests/test_prompt_store.py \
  tests/test_prompt_optimizer.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A app/routing app/pipeline.py config/prompts tests
git commit -m "refactor(step4): task mode is always direct; drop the LLM classifier

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Delete the agent runtime, the multi-step branch, and the NAT dependency

**Files:**
- Delete: `app/agent/` (whole package), `nat_tools/` (whole package),
  `config/nat_workflow.yaml`
- Modify: `app/pipeline.py` (`StreamFallback` docstring ~:40-56, constructor comments
  ~:83-91, `run` branch ~:477-487, `run_stream` branch ~:784-789, `_run_multi_step`
  ~:918-1010 and its trace/cost tail)
- Modify: `app/main.py:11`, `:117` (comment), `:175`; `app/api/routes.py:18`, `:222-223`,
  `:391`; `app/gateway/backends/base.py:23` (comment)
- Modify: `pyproject.toml:43-44`, `:80` (+ the NAT-only pin comment near line 23 and any
  NAT-only lint/dep filter), `Dockerfile:34`
- Delete: `tests/test_agent_hop_compression.py`, `tests/test_agent_internal_endpoint.py`,
  `tests/test_agent_runtime.py`, `tests/test_agent_pipeline_e2e.py`,
  `tests/test_agent_mock_sse.py`, `tests/test_agent_telemetry_redaction.py`,
  `tests/test_litellm_multistep.py`, `tests/test_nat_workflow_config.py`
- Modify: `tests/conftest.py:95-130` (the `HOPS` fixture), `tests/test_error_taxonomy.py:38-50`,
  `tests/test_run_stream_pipeline.py:12`, `:139-155`, `tests/test_stream_keepalive.py:117`,
  `tests/test_stream_unverified.py:6`, `:150-180`, `tests/test_traffic_honesty.py:250-290`
- Test: `tests/test_run_stream_pipeline.py`, `tests/test_no_agent_imports.py` (new)

**Interfaces:**
- Consumes: `decide(ctx, request) -> "direct"` from Task 1.
- Produces: `Pipeline.run` and `Pipeline.run_stream` with no `mode` variable and no
  `crec`; `StreamFallback` raised for exactly two reasons (tool conversation; and the
  `onprem_only` / `require_verification` floor). `AgentCapExceeded` no longer exists, so
  nothing may import it.

- [ ] **Step 1: Write the failing guard test**

Create `tests/test_no_agent_imports.py`:

```python
"""RouteForge runs no agent of its own (2026-09-17 removal): tools belong to
the calling application. Guards against the NAT agent creeping back in."""
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


def test_no_module_imports_nat_or_app_agent():
    offenders = []
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            if (stripped.startswith(("import nat", "from nat"))
                    or "app.agent" in stripped):
                offenders.append(f"{path.relative_to(APP.parent)}: {stripped}")
    assert offenders == []


def test_app_agent_package_is_gone():
    assert not (APP / "agent").exists()


def test_nat_is_not_a_dependency():
    pyproject = (APP.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "nvidia-nat" not in pyproject
    assert "nat_tools" not in pyproject
```

Also replace `test_stream_fallback_on_multi_step` in
`tests/test_run_stream_pipeline.py:139` with a test asserting the opposite — the former
trigger phrase now streams instead of falling back:

```python
def test_former_trigger_phrase_streams_instead_of_falling_back(monkeypatch):
    """"step by step" used to divert to the internal agent and raise
    StreamFallback. With the agent gone it is an ordinary direct request."""
    from app.gateway.backends import litellm_backend

    monkeypatch.setattr(
        litellm_backend, "acompletion", make_streaming_acompletion(["x"]))
    s = _settings()
    pipe = _pipeline(s)

    async def _run():
        req = ChatRequest(messages=[Message(
            role="user", content="Explain step by step how to bake bread.")])
        deltas, get_meta = await pipe.run_stream(
            "req-1", "demo", req, app_policy=AppPolicyView(residency="any"))
        async for _ in deltas:
            pass
        return get_meta()

    meta = asyncio.run(_run())
    assert meta["verification"] == "waived"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest \
  tests/test_no_agent_imports.py \
  tests/test_run_stream_pipeline.py::test_former_trigger_phrase_streams_instead_of_falling_back -v
```

Expected: FAIL (`app/agent` still exists; `StreamFallback` still raised).

- [ ] **Step 3: Delete the agent, its tools, and its tests**

```bash
git rm -r app/agent nat_tools config/nat_workflow.yaml
git rm tests/test_agent_hop_compression.py tests/test_agent_internal_endpoint.py \
       tests/test_agent_runtime.py tests/test_agent_pipeline_e2e.py \
       tests/test_agent_mock_sse.py tests/test_agent_telemetry_redaction.py \
       tests/test_litellm_multistep.py tests/test_nat_workflow_config.py
```

- [ ] **Step 4: Remove the multi-step branches from the pipeline**

In `app/pipeline.py`:

1. Delete the whole `_run_multi_step` method (from `async def _run_multi_step` through
   the end of its return dict — it is the last multi-step-only method in the file).
2. In `run`, delete the branch at ~:477:

```python
        if mode == "multi_step":
            ...
            return await self._run_multi_step(
                request_id, ctx, request, san_messages, det, policy_mode,
                recall.record, classifier_record=crec)
```

3. In `run`, delete both `mode, crec = …` assignments (the `if tool_mode:` branch at
   ~:436 and the `else:` branch at ~:447) together with the task-mode comment block above
   the second one, replacing that comment with a one-liner stating that step 4 is always
   `direct` because agent loops are client-driven. No `mode` or `crec` variable survives
   in `run`.
4. Delete the classifier-record block in `run` at ~:561-566:

```python
        if crec is not None:
            ...
            records = [crec, *records]
```

   and the identical block in `run_stream` at ~:817-820. Both only ever prepended the
   step-4 classifier's record, which no longer exists.
5. Remove the now-unused `decide_mode` import if nothing else in `pipeline.py` calls it;
   `app/routing/task_mode.py` itself stays (it is the step-4 module, and other callers
   and tests import `decide` from it — grep before deciding).
6. In `run_stream`, delete the `mode, crec = ...` line and the `if mode == "multi_step":
   raise StreamFallback(...)` block entirely, keeping the tool-conversation fallback
   above it and the `ctx.onprem_only or ctx.require_verification` floor below it.
7. Update the `StreamFallback` docstring so it lists exactly two reasons: the
   residency/verification floor, and the client-driven tool conversation.
8. Delete the constructor's `self._self_port` and its comment if nothing else reads it
   (grep first: `/usr/bin/grep -rn "_self_port\|self_port" app`). If `main.py` passes
   `self_port=`, remove the argument at both ends; if any non-agent code still uses it,
   leave it and say so in the task report.
9. Fix the stale comments that name the agent: the `co_cfg_provider` comment (~:91),
   the `_cache` comment (~:121), the `run` tool-mode comment (~:427), and
   `_display_name`'s "multi-step `auto` sentinel" note.

- [ ] **Step 5: Unwire the internal endpoint and the agent exception**

In `app/main.py`: delete the `from app.agent.internal_endpoint import internal_router`
import (line 11), the `app.include_router(internal_router)` call (line 175), and the
"Stashed so the internal per-hop endpoint…" comment (~:117), keeping the
`app.state.co_cfg` / `app.state.ccr` assignments themselves (the direct path uses them).

In `app/api/routes.py`: delete the `AgentCapExceeded` import (line 18) and its mapping:

```python
    if isinstance(exc, AgentCapExceeded):
        return 500, "agent did not converge within its step limit"
```

Update the comment at `:391` so the floor reads `(onprem_only / require_verification)`.
Fix the stale agent comment in `app/gateway/backends/base.py:23`.

- [ ] **Step 6: Drop the NAT dependency**

In `pyproject.toml`: remove `"nvidia-nat[langchain]>=1.8,<2"` and `"nat_tools"` from
`dependencies`, the `nat_tools = { path = "nat_tools", editable = true }` source, the
transitive-pin comment near line 23 (and the pin itself if it exists only for NAT), and
the `langsmith` filter comment near line 72 if it names NAT as the reason. In
`Dockerfile`, delete `COPY nat_tools/ ./nat_tools/`.

```bash
uv lock && LITELLM_LOCAL_MODEL_COST_MAP=True uv run python -c "import app.main"
```

Expected: the lock updates and the app imports cleanly.

- [ ] **Step 7: Fix the tests that only borrowed the agent**

- `tests/conftest.py`: delete the `HOPS`-based fixture (~:95-130) and any fixture that
  only served it. If another test still requests that fixture name, grep and delete
  those tests too (they were per-hop tests).
- `tests/test_error_taxonomy.py:38-50`: delete the agent-cap case (the 500
  "did not converge" mapping no longer exists).
- `tests/test_stream_keepalive.py:117`: delete the `AgentCapExceeded` import and the
  case that raises it; if the test needs an error to exercise keepalive, raise
  `BackendUnavailable` from `app.gateway.backends.base` instead.
- `tests/test_stream_unverified.py`: delete the `pytest.importorskip("nat")` test and
  its `mock_multistep` request (~:150-180); update the module docstring so the floor
  reads `onprem_only` / `require_verification` only.
- `tests/test_traffic_honesty.py:250-290`: delete the `multi_step + PII` test
  (`importorskip("nat")`, `mock_multistep`). The `pii_bearing` persona test in the same
  file already covers "PII stays on-prem", so no replacement is needed; say so in the
  task report.

- [ ] **Step 8: Run the tests to verify they pass**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests -x -q
```

Expected: PASS (collection included). If a test outside this task's list fails, fix it
here only if it references the agent; otherwise stop and report.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor(agent): remove the NAT agent runtime and the multi-step path

Tools belong to the calling application: RouteForge returns tool_calls and
never executes them. The internal agent hijacked any request containing
'step by step' or 'look up', ran canned demo tools, and never gated its own
hops.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Remove agent-only config, stage signals, and deployment values

**Files:**
- Delete: `app/routing/signals.py`, `tests/test_routing_signals.py`,
  `tests/test_signal_wiring.py`
- Modify: `app/config/schema.py:25-42` (`StageSignalsCfg`), `:85`
  (`RoutingCfg.stage_signals`), `:111` (`PolicyCfg.agent_budgets`)
- Modify: `app/config/lint.py:42-44` (`ROLE_TIER_UNKNOWN`)
- Modify: `config/routing.yaml:77-95` (`stage_signals:`), `config/policy.yaml:38-43`
  (`agent_budgets:`), `config/serving.yaml:107-110` (`agent:`)
- Modify: `deploy/helm/serving-plane/values.yaml:83-84`,
  `deploy/helm/serving-plane/templates/configmap.yaml:67`,
  `deploy/hpe/routeforge/values.yaml:152-153`,
  `deploy/hpe/routeforge/templates/serving-config.yaml:64`
- Modify: `tests/test_config_lint.py:123`, `tests/test_semantic_cache_config.py:18`
- Test: `tests/test_stale_agent_config_keys.py` (new)

**Interfaces:**
- Consumes: nothing from Task 2 beyond the deleted agent package.
- Produces: `RoutingCfg` with no `stage_signals` attribute and `PolicyCfg` with no
  `agent_budgets` attribute. Any code reading either must be gone.

- [ ] **Step 1: Write the failing stale-key test**

Create `tests/test_stale_agent_config_keys.py`:

```python
"""A deployment upgrading past the 2026-09-17 agent removal still has
`agent:`, `agent_budgets:`, and `stage_signals:` in its mounted YAML. Unknown
keys must be ignored, never a boot failure."""
import shutil
from pathlib import Path

import yaml

from app.config.lint import enforce
from app.config.loader import load_settings


def test_boot_ignores_stale_agent_keys(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    shutil.copytree(Path("config"), cfg)

    serving = yaml.safe_load((cfg / "serving.yaml").read_text())
    serving["agent"] = {"runtime": "nat"}
    (cfg / "serving.yaml").write_text(yaml.safe_dump(serving))

    policy = yaml.safe_load((cfg / "policy.yaml").read_text())
    policy["agent_budgets"] = {"max_steps": 5, "max_spend_usd": 0.05,
                               "per_call_timeout_s": 10}
    (cfg / "policy.yaml").write_text(yaml.safe_dump(policy))

    routing = yaml.safe_load((cfg / "routing.yaml").read_text())
    routing["stage_signals"] = {"enabled": False, "role_tiers": {"plan": "frontier"}}
    (cfg / "routing.yaml").write_text(yaml.safe_dump(routing))

    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    enforce(settings)  # must not raise

    assert not hasattr(settings.routing, "stage_signals")
    assert not hasattr(settings.policy, "agent_budgets")
```

**Note for the implementer:** check how `load_settings()` finds the config directory
(`app/config/loader.py`). If it takes a path argument or reads an env var instead of the
CWD, use that mechanism rather than `monkeypatch.chdir`, and adjust the test.

- [ ] **Step 2: Run the test to verify it fails**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_stale_agent_config_keys.py -v
```

Expected: FAIL on the two `hasattr` assertions (the fields still exist).

- [ ] **Step 3: Delete stage signals**

```bash
git rm app/routing/signals.py tests/test_routing_signals.py tests/test_signal_wiring.py
```

In `app/config/schema.py`, delete the whole `StageSignalsCfg` class and the
`stage_signals: StageSignalsCfg = StageSignalsCfg()` line from `RoutingCfg`.

In `app/config/lint.py`, delete:

```python
    for role, t in s.routing.stage_signals.role_tiers.items():
        if t not in tiers:
            errs.append(f"ROLE_TIER_UNKNOWN {role}:{t} not in {sorted(tiers)}")
```

In `config/routing.yaml`, delete the `stage_signals:` block and the comment paragraph
above it that explains the agent-loop lever.

In `tests/test_config_lint.py:123`, delete the `ROLE_TIER_UNKNOWN` case (the test that
sets `s.routing.stage_signals.role_tiers["plan"] = "strong"`). In
`tests/test_semantic_cache_config.py:18`, drop `"stage_signals"` from the tuple.

- [ ] **Step 4: Delete the agent budgets and the infra block**

In `app/config/schema.py`, delete `agent_budgets: dict[str, float]` from `PolicyCfg`.
In `config/policy.yaml`, delete the `agent_budgets:` block and its comment. In
`config/serving.yaml`, delete the `agent:` block (`runtime: nat` and the caps comment).

Grep for stragglers and fix any hit:

```bash
/usr/bin/grep -rn "agent_budgets\|stage_signals\|runtime: nat" app config tests
```

- [ ] **Step 5: Remove the Helm and HPE values**

Delete `servingPlane.agent` from `deploy/helm/serving-plane/values.yaml:83-84` and
`deploy/hpe/routeforge/values.yaml:152-153`, and the
`runtime: {{ .Values.servingPlane.agent.runtime | quote }}` line (plus its parent
`agent:` key) from both configmap templates.

- [ ] **Step 6: Run the tests and render the charts**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest \
  tests/test_stale_agent_config_keys.py tests/test_config_lint.py \
  tests/test_semantic_cache_config.py -v
helm template deploy/helm/serving-plane >/dev/null && \
helm template deploy/hpe/routeforge >/dev/null && echo "charts render"
```

Expected: PASS, and both charts render.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "chore(config): drop stage signals, agent budgets, and the agent block

Stale keys in a mounted YAML are ignored, not a boot failure.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Replace the multi-step traffic persona

The `multi_step` persona carried the large duplicated log block that gives Headroom real
material to compress. Keep the compression material, drop the agent.

**Files:**
- Modify: `tests/personas.yaml:30-33`, `tests/traffic/personas.py:23`, `:140-155`, `:181`
- Modify: `tests/test_traffic_personas.py:71-72`
- Test: `tests/test_traffic_personas.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: persona `large_context` (weight 12, `api_key: acme-key`,
  `expect_onprem: false`), builder `_build_large_context(rng) -> dict`. Persona names are
  asserted in `tests/test_traffic_personas.py` and read from `tests/personas.yaml`.

- [ ] **Step 1: Update the persona test first**

In `tests/test_traffic_personas.py`, replace the `multi_step` case in
`test_persona_build_shapes` (lines 71-72) with:

```python
        if p.name == "large_context":
            assert "mock_multistep" not in body
            assert len(body["messages"][0]["content"]) > 2000
```

- [ ] **Step 2: Run it to verify it fails**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_traffic_personas.py -v
```

Expected: FAIL — `load_personas()` still yields `multi_step`, and no persona is named
`large_context`.

- [ ] **Step 3: Rename the persona**

In `tests/personas.yaml`, replace the `multi_step` entry with:

```yaml
  - name: large_context
    weight: 12
    api_key: acme-key
    expect_onprem: false
```

In `tests/traffic/personas.py`, rename `_build_multi_step` to `_build_large_context`,
drop the `mock_multistep` key, and rewrite the content so it no longer asks for an agent:

```python
def _build_large_context(rng: random.Random) -> dict:
    # large_tool_output overlay: the gate collapses all messages into a single
    # sanitized message (app/pipeline.py joins message content), so the
    # duplicated log block lives inside the one user message. It gives Headroom
    # real duplicate-line material to compress.
    content = (
        "Summarize the deployment status from the log below.\n\n"
        + _LARGE_LOG_BLOCK
    )
    return {"messages": [{"role": "user", "content": content}]}
```

Update the `_BUILDERS` entry to `"large_context": _build_large_context` and the comment at
line 23 that mentions the multi_step overlay.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest \
  tests/test_traffic_personas.py tests/test_traffic_generator.py \
  tests/test_traffic_honesty.py tests/test_traffic_endpoint.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A tests
git commit -m "test(traffic): replace the multi_step persona with large_context

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Console — step 4 always reads "direct"

**Files:**
- Modify: `ui/src/api.ts:474` (the `task_mode` `StepMeta` caption)
- Modify: `ui/src/console/RequestDrilldown.tsx:205-213` (the `task_mode` case),
  `:139` and `:498-500` (the `hops` prop and its derivation)
- Test: `ui` type-check / build

**Interfaces:**
- Consumes: traces produced after Task 2, which never contain an `agent_hop` step.
- Produces: no new exported symbols. `StepBody` keeps its `hops` prop only if some other
  case still uses it.

- [ ] **Step 1: Update the step caption**

In `ui/src/api.ts:474`:

```ts
  { key: "task_mode", label: "Task Mode", caption: "Direct — agent loops run in the client" },
```

- [ ] **Step 2: Update the drill-down body**

In `ui/src/console/RequestDrilldown.tsx`, replace the `task_mode` case:

```tsx
    case "task_mode":
      return (
        <>
          <Row label="mode">direct answer</Row>
          <p className="text-xs text-gray-500 pt-2">
            A single model call answers directly. RouteForge runs no agent of its own —
            an application's agent loop sends each call as its own request, and every one
            is gated, compressed, routed and verified independently.
          </p>
        </>
      );
```

- [ ] **Step 3: Simplify the hops derivation**

Grep for `hops` in `ui/src`. In `PlainSummary` (~:498) the `agent_hop` step no longer
exists; drop `|| r.step === "agent_hop"` from the filter, keeping `summary?.hops ?? …` as
the source of truth (older stored requests may still carry a hop count, and the history
API is unchanged). If `StepBody`'s `hops` prop has no remaining reader after the
`task_mode` case is rewritten, remove the prop and its call-site argument.

- [ ] **Step 4: Type-check and build**

```bash
cd ui && npm run build
```

Expected: build succeeds with no TypeScript errors (unused-variable errors mean a `hops`
reference was missed).

- [ ] **Step 5: Commit**

```bash
git add -A ui
git commit -m "feat(ui): step 4 always reads direct; drop the agent-hop copy

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Documentation, CLAUDE.md, skills, and the final gate

**Files:**
- Modify: `CLAUDE.md` (see the itemized list below)
- Modify: `docs/reference/routing-observability.md`, `per-app-config.md`,
  `api-reference.md`, `README.md`
- Modify: `docs/guides/getting-started.md`, `agentic-app-guide.md`,
  `creating-a-sample-application.md`, `reading-the-dashboard-and-trace.md`,
  `integrating-an-app.md`, `ui-guide.md`, `exporting-eval-datasets.md`, `README.md`
- Modify: `docs/architecture/logical-and-request-flow.md`, `sample-request-flows.md`,
  `routing-tier-selection.md`, and the diagram specs under
  `docs/architecture/diagrams/specs/`
- Modify: `docs/roadmap/ip-and-research-opportunities.md`
- Delete: `.claude/skills/nemo-agent-toolkit/`
- Modify: `.claude/skills/routing-and-gateway/SKILL.md`,
  `.claude/skills/traffic-and-tests/SKILL.md`, `.claude/skills/demo-ui/SKILL.md`

**Interfaces:**
- Consumes: the finished behavior from Tasks 1-5.
- Produces: documentation that matches the code. No code changes in this task.

- [ ] **Step 1: Amend CLAUDE.md**

Make exactly these edits:

1. Architecture table, step 4 row: `direct vs multi-step` → `always **direct**; agent
   loops are client-driven`. Delete the `— | Agent Runtime / Orchestrator |
   app/agent/runtime.py |` row.
2. Delete the paragraph beginning "Multi-step (step 4 → agent runtime) uses **NeMo Agent
   Toolkit**" and replace it with: "Agent loops belong to the calling application. Every
   model call an application's loop makes arrives as its own request and re-enters steps
   2–9, so compression (5) and the router (6) stay **per-call**. This is the single most
   important behaviour to get visibly right in the UI."
3. Delete the paragraph "**The step-4 classifier is a routed call.** …".
4. Routing scopes: delete the `**NAT Router Agent** (step 4)` bullet.
5. Routing observability: delete clause (2) about `routing.stage_signals`, renumbering
   the remaining clauses.
6. Configuration table: `policy.yaml` row, delete `(default + \`agent_budgets\`)`.
7. Tech stack: delete the `**NeMo Agent Toolkit** (\`nvidia-nat\`)` bullet; move Langfuse
   export to RouteForge's own OpenTelemetry exporter in the Langfuse bullet.
8. Skills list: delete the `nemo-agent-toolkit` bullet.
9. Build order: mark layer 5 "Agent runtime" as **removed 2026-09-17 (agent loops are
   client-driven)**.
10. Sales walkthrough: delete step 3 (complex multi-step task) and renumber; step 4
    (Headroom) now re-runs a long-context request (the `large_context` persona) instead
    of a multi-step task.
11. Pitfalls: the "Don't let the router see a request the gate hasn't cleared" entry
    stays; no pitfall mentions the agent after this edit (grep to confirm).

- [ ] **Step 2: Update reference and guide docs**

```bash
/usr/bin/grep -rln "multi_step\|multi-step\|NeMo Agent\|nvidia-nat\|stage_signals\|agent_budgets\|mock_multistep\|agent hop" docs .claude/skills README.md | grep -v "docs/archive\|docs/superpowers\|docs/new"
```

Work that list file by file. Rules:
- Remove multi-step branches from request-flow narratives; keep the per-call framing by
  describing a client-driven loop instead.
- `docs/reference/routing-observability.md`: delete the `routing.stage_signals` section
  and any `sp.` attribute it documented that no longer exists (verify each against
  `app/obs/export.py` before deleting).
- `docs/guides/agentic-app-guide.md`: this guide is about client-driven agents, so it
  gains a short note that RouteForge never runs the loop itself.
- Architecture decision records get a dated note, not a rewrite:
  `> **Superseded 2026-09-17:** the internal NAT agent and the multi-step path were
  removed; agent loops are client-driven.`

- [ ] **Step 3: Regenerate the diagrams**

Update `docs/architecture/diagrams/specs/request-flow.json` and
`logical-architecture.json` to drop the agent-runtime node and the multi-step branch, then
regenerate both `.drawio` files using the `drawio-diagrams` skill. Follow
`docs/architecture/diagrams/README.md` for the generation command.

- [ ] **Step 4: Record the research impact**

In `docs/roadmap/ip-and-research-opportunities.md`, under IP-1 and R3, add:

```markdown
> **2026-09-17 — re-targeted.** The internal NAT agent was removed, so the "hop" this
> mechanism ratchets over is no longer an in-process agent step. The equivalent setting
> is a client-driven agent loop, where each hop arrives as a separate request: the
> ratchet becomes per-conversation, which needs a conversation/session identifier
> (`x-routeforge-session` today, or the derived id). `app/routing/signals.py`'s stage
> heuristics were removed with the agent and are recoverable from git history if that
> work revives them.
```

- [ ] **Step 5: Update the skills**

```bash
git rm -r .claude/skills/nemo-agent-toolkit
```

In `.claude/skills/routing-and-gateway/SKILL.md`, describe step 4 as the constant direct
decision and delete the classifier guidance. In `.claude/skills/traffic-and-tests/SKILL.md`,
rename the `multi_step` persona to `large_context`. In `.claude/skills/demo-ui/SKILL.md`,
delete the multi-step hop visualization guidance.

- [ ] **Step 6: Full verification gate**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests -q
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/onboarding -q
cd ui && npm run build && cd ..
helm template deploy/helm/serving-plane >/dev/null && helm template deploy/hpe/routeforge >/dev/null
/usr/bin/grep -rn "nvidia-nat\|nat_tools\|app.agent\|multi_step\|mock_multistep\|stage_signals\|agent_budgets" app config tests ui/src pyproject.toml Dockerfile deploy
```

Expected: both suites pass, the UI builds, both charts render, and the final grep returns
**no** hits. Paste the real output into the task report — a claim without output is not
evidence.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "docs: RouteForge runs no agent of its own

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Manual verification (after Task 6)

The agent path touched serving, so exercise the real app once before opening the PR:

1. Boot: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run uvicorn app.main:create_app --factory --port 8080`
2. `GET /health` returns 200.
3. `POST /v1/chat/completions` with `"Explain step by step how to bake bread."` returns a
   normal answer whose trace contains no `agent_hop` records (it previously ran the
   internal agent).
4. The same request with `"mock_multistep": true` also returns 200.
5. The console request drill-down shows the Task Mode step reading "direct answer".
6. `GET /metrics` (with the `observability` extra installed) still exports the four
   `routeforge_*` families.

## Out of scope

- Priority 1 (P1a/P1b): the policy compiler, the decision certificate, and the
  noninterference harness.
- Any change to protection semantics (`policy_mode`, `capture_io`, `pii_detection`),
  streaming modes beyond deleting the `multi_step` fallback reason, or routing scoring.
- Deleting `docs/new/` handoffs.
- A conversation-level privacy ratchet for client-driven loops (future IP-1 work).
