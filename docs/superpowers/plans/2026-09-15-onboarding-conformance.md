# Onboarding Conformance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new application onboarded onto RouteForge and pointed at a real OpenAI-compatible client either gets served, or gets a 4xx whose message names the policy field to change. It never gets a 500 or 502, and it never gets a rejection with no explanation.

**Architecture:** Build a conformance test suite (`tests/onboarding/`) that replays real client request shapes against a set of per-app policy recipes and model setups. Known bugs are marked `xfail(strict=True)` and point at the task that fixes them. Each later task fixes one class of failure and deletes its xfail line:

- the B24 crash;
- dropped generation params;
- a wrong budget estimate;
- no tier fallback when over budget;
- no per-app per-request cap;
- opaque rejection messages;
- no save-time validation.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy async, pytest + `fastapi.testclient.TestClient`, LiteLLM (faked in tests), React/TS (one field).

**Spec:** This plan is the spec. The design decisions below were agreed with the owner on 2026-09-15: tests **plus** bug fixes; a per-app per-request cap; a corrected estimate; tier fallback when over budget.

## Why this exists (root cause, from the incident history)

Sixteen onboarding incidents (millwise, demo, kit-gtm) share four causes:

1. **Tests use tiny synthetic requests, never real client traffic.** Each of these broke in production, not in tests:
   - content as a list of parts;
   - `stream:true` on every turn;
   - replayed tool turns;
   - `/v1/models`;
   - 25K-token prompts;
   - `content:null`.
2. **Policy fields are tested one at a time**, never in combination.
3. **The admin API accepts configurations that can never route:**
   - tier or provider typos;
   - an unknown model slug;
   - an empty allowlist, which means "allow all";
   - `max_rpm: 0`;
   - a model price that pushes every realistic request over the $0.05 cap.
4. **Rejections don't say what to change.** Examples: "no candidates after budget filter"; a 502 that hides the upstream error; a stream 400 that doesn't mention `stream_mode`.

## Global Constraints

- Run tests with `LITELLM_LOCAL_MODEL_COST_MAP=True` (otherwise pytest hangs fetching the litellm cost map). The command is `LITELLM_LOCAL_MODEL_COST_MAP=True /home/manimal/projects/routeForge/.venv/bin/python -m pytest -q -p no:cacheprovider <target>`. The `rtk` shell hook can hide pytest summary lines, so wrap the command in `rtk proxy bash -c '...'` and rely on the exit code.
- Tests use no network and no GPU. LiteLLM is faked (`tests/_litellm_fake.py`), and `SP_PII_DETECTION=regex` is set by `tests/conftest.py`.
- CLAUDE.md non-negotiables still hold:
  - `onprem_only` is absolute (never widened by any fallback);
  - residency beats the risk floor;
  - fail closed;
  - no secrets in code;
  - one owner per config concern: a new budget key lives **only** in `config/policy.yaml`.
- Protocol gotcha: when you add a parameter to `InferenceBackend.generate`/`generate_stream`, every implementation must accept it. That includes the test fakes. A `TypeError` there is swallowed by the gateway as a failover and shows up as a 502 or 422.
- There are no DB migrations. A new `app_policies` column must be added to `scripts/migrate_app_policies.py`.
- Existing error-message prefixes stay unchanged, because tests match on them: `no candidates after residency filter`, `no candidates after budget filter`, `requested model`, `no backend could serve this request`, `streaming is not supported`. Only append detail after them.
- Commit after each task on the working branch, ending every commit message with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01JM4FJ3rT8FENvaEMAj8MCb
  ```

---

### Task 1: Conformance harness, real-client fixtures, and the matrix

**Files:**
- Create: `tests/onboarding/__init__.py` (empty)
- Create: `tests/onboarding/clients.py`
- Create: `tests/onboarding/harness.py`
- Create: `tests/onboarding/test_conformance.py`

**Interfaces:**
- Produces:
  - `tests.onboarding.harness.conformance_client()`: a context manager yielding a `TestClient` with the DB portal live.
  - `onboard(client, app_id, **policy) -> OnboardedApp` (has `.chat(payload)`, `.headers`, `.app_id`).
  - `add_model(client, slug, *, tier="cheap", price_input=0.0, price_output=0.0, allowed_app_ids=None, enabled=True)`.
  - `set_policy_direct(client, app_id, **fields)`.
  - `disable_seeded_models(client)`.
  - `install_dual_fake(monkeypatch)`.
  - `tests.onboarding.clients.CLIENTS: dict[str, Callable[..., dict]]` and `STREAM_CLIENTS: frozenset[str]`.
  - `tests.onboarding.test_conformance.RECIPES`, `SETUPS`, `HINT_WORDS`, `known_bug()`.

- [ ] **Step 1: Create `tests/onboarding/__init__.py`** as an empty file.

- [ ] **Step 2: Write `tests/onboarding/clients.py`**

```python
"""Request shapes captured from real OpenAI-compatible clients. Every onboarding
incident so far was a shape like these that no test had sent. Synthetic
content only -- never real customer data."""
from __future__ import annotations

# ~108K chars -> ~27K tokens by count_tokens (len // 4): a real Qwen Code
# system prompt is ~27K chars of instructions plus tool docs.
BIG_SYSTEM = "You are Qwen Code, an interactive CLI agent. " * 2400

TOOL_NAMES = [
    "read_file", "write_file", "edit", "list_directory", "glob", "grep",
    "run_shell_command", "web_fetch", "web_search", "save_memory", "todo_write",
    "task", "read_many_files", "exit_plan_mode", "lsp_hover", "lsp_definition",
    "lsp_references", "notebook_edit", "ask_user", "skill", "diff", "apply_patch",
]
TOOLS = [
    {"type": "function", "function": {
        "name": name,
        "description": f"{name}: operate on the user's workspace. " * 10,
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}},
                       "required": ["path"]}}}
    for name in TOOL_NAMES
]


def qwen_code_turn(model: str = "auto") -> dict:
    """Qwen Code 0.23: content parts with cache_control, 22 tools, stream,
    max_tokens 8192, ~27K-token system prompt."""
    return {
        "model": model, "stream": True, "max_tokens": 8192, "temperature": 0.3,
        "tools": TOOLS,
        "messages": [
            {"role": "system", "content": [
                {"type": "text", "text": BIG_SYSTEM, "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": [
                {"type": "text", "text": "Reply with exactly: ROUTEFORGE_OK"}]},
        ],
    }


def openai_sdk_tool_replay(model: str = "auto") -> dict:
    """openai SDK agent loop, second turn: assistant tool_call (content None)
    and the tool result replayed back."""
    return {
        "model": model, "tools": TOOLS[:3],
        "messages": [
            {"role": "system", "content": "You are a helpful agent."},
            {"role": "user", "content": "Read README.md and summarise it."},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{\"path\": \"README.md\"}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "# RouteForge\nA router."},
        ],
    }


def chat_ui_null_content(model: str = "auto") -> dict:
    """A chat UI that sends a trailing user turn with content null (B24)."""
    return {
        "model": model,
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": None},
        ],
    }


def langchain_parts_chat(model: str = "auto") -> dict:
    """LangChain ChatOpenAI: content parts, temperature 0, no tools."""
    return {
        "model": model, "temperature": 0,
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "Be brief."}]},
            {"role": "user", "content": [{"type": "text", "text": "What is the capital of France?"}]},
        ],
    }


def plain_stream_chat(model: str = "auto") -> dict:
    """Any chat UI with streaming on and no tools."""
    return {"model": model, "stream": True,
            "messages": [{"role": "user", "content": "Say hi"}]}


CLIENTS = {
    "qwen_code_turn": qwen_code_turn,
    "openai_sdk_tool_replay": openai_sdk_tool_replay,
    "chat_ui_null_content": chat_ui_null_content,
    "langchain_parts_chat": langchain_parts_chat,
    "plain_stream_chat": plain_stream_chat,
}
STREAM_CLIENTS = frozenset({"qwen_code_turn", "plain_stream_chat"})
```

- [ ] **Step 3: Write `tests/onboarding/harness.py`**

```python
"""Onboard an application the way an operator does (admin API: create app and
token), then shape its policy and model pool for a conformance case."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from fastapi.testclient import TestClient

from app.apps import store
from app.gateway.backends import litellm_backend
from app.main import create_app
from tests._litellm_fake import CALLS, make_response, _FakeStreamChunk
from tests.test_admin_api import _login


@dataclass
class OnboardedApp:
    client: TestClient
    app_id: str
    headers: dict

    def chat(self, payload: dict):
        return self.client.post("/v1/chat/completions", headers=self.headers, json=payload)


@contextmanager
def conformance_client():
    # Context-managed so client.portal exists (DB writes run on the app's loop).
    with TestClient(create_app()) as c:
        yield c


def _run(client: TestClient, coro_fn) -> None:
    client.portal.call(coro_fn)


def add_model(client: TestClient, slug: str, *, tier: str = "cheap",
              price_input: float = 0.0, price_output: float = 0.0,
              allowed_app_ids: list[str] | None = None, enabled: bool = True) -> None:
    app = client.app

    async def go():
        async with app.state.db.session() as session:
            await store.upsert_onboarded(
                session, slug, display_name=slug, provider="onprem", tier=tier,
                endpoint="http://test-local/v1", api_key_env=None,
                price_input=price_input, price_output=price_output, quality=None,
                context_window=None, allowed_app_ids=allowed_app_ids, notes=None,
                enabled=enabled, probe_status="healthy" if enabled else "unreachable",
                probe_error=None)
            await session.commit()
        await app.state.registry.refresh(app.state.db)

    _run(client, go)


def disable_seeded_models(client: TestClient) -> None:
    """Take the test-seeded local-8b/local-32b out of the pool, so the case's own
    model is the only on-prem candidate."""
    add_model(client, "local-8b", tier="cheap", price_input=0.05, price_output=0.05,
              enabled=False)
    add_model(client, "local-32b", tier="mid", price_input=0.10, price_output=0.10,
              enabled=False)


def set_policy_direct(client: TestClient, app_id: str, **fields) -> None:
    """Write policy columns straight to the store (no admin clamps): the
    conformance matrix tests request-time behaviour of a stored policy."""
    app = client.app

    async def go():
        async with app.state.db.session() as session:
            await store.upsert_policy(session, app_id, **fields)
            await session.commit()

    _run(client, go)


def onboard(client: TestClient, app_id: str, **policy) -> OnboardedApp:
    _login(client)
    r = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code in (200, 201), r.text
    r = client.post(f"/admin/applications/{app_id}/tokens", json={"name": "conformance"})
    assert r.status_code in (200, 201), r.text
    if policy:
        set_policy_direct(client, app_id, **policy)
    return OnboardedApp(client, app_id, {"Authorization": f"Bearer {r.json()['token']}"})


def install_dual_fake(monkeypatch) -> None:
    """A litellm.acompletion fake that streams when asked to, else returns a
    canned completion. Records kwargs in tests._litellm_fake.CALLS."""
    async def _call(**kwargs):
        CALLS.append(kwargs)
        if kwargs.get("stream"):
            async def _gen():
                yield _FakeStreamChunk(content="ROUTEFORGE_OK")
                yield _FakeStreamChunk(empty_choices=True,
                                       usage={"prompt_tokens": 12, "completion_tokens": 3})
            return _gen()
        return make_response(content="ROUTEFORGE_OK")

    monkeypatch.setattr(litellm_backend, "acompletion", _call)
```

- [ ] **Step 4: Write `tests/onboarding/test_conformance.py`**

```python
"""Onboarding conformance matrix: real client shape x app policy recipe x model
setup. Every cell must be served (200), or rejected with a 4xx whose detail
names the thing to change. Never 500/502/503, never an unexplained reject.

Known bugs are xfail(strict=True) via known_bug(); the task that fixes one
deletes its line, so a fix that silently regresses flips the cell red."""
from __future__ import annotations

import pytest

from tests.onboarding.clients import CLIENTS, STREAM_CLIENTS
from tests.onboarding.harness import (
    add_model, conformance_client, disable_seeded_models, install_dual_fake, onboard,
)

MODEL = "conf-model"

# Per-app policy recipes an operator actually configures (console presets and
# the three live apps). Written straight to the store; save-time clamps are
# tested separately in tests/test_policy_save_validation.py.
RECIPES = {
    "default": {},
    "kit_gtm_router": dict(residency="any", policy_mode="passthrough",
                           capture_io="full", stream_mode="stream_unverified"),
    "onprem_enforced": dict(residency="onprem_only", stream_mode="verified_then_stream"),
    "millwise_like": dict(residency="onprem_only", policy_mode="passthrough",
                          capture_io="off", stream_mode="verified_then_stream"),
    "scoped_model": dict(allowed_models=[MODEL], stream_mode="incremental_verify"),
}

# Model pool setups: (price_input, price_output) per 1M for the only on-prem model.
SETUPS = {
    "free_onprem": (0.0, 0.0),
    "priced_10": (10.0, 10.0),    # the $10/1M incident (2026-09-15)
}

# A rejection is actionable when its detail names at least one of these.
HINT_WORDS = (
    "stream_mode", "max_cost_usd_per_request", "allowed_models", "allowed_tiers",
    "allowed_providers", "allowed_app_ids", "residency", "onboard", "disabled",
    "max_rpm", "max_spend_usd_per_day",
)


def known_bug(client: str, recipe: str, setup: str) -> str | None:
    if client == "chat_ui_null_content":
        return "B24: task_mode calls .lower() on None content -> 500 (Task 2)"
    if recipe == "default" and client in STREAM_CLIENTS:
        return "stream 400 does not name stream_mode (Task 7)"
    if setup == "priced_10" and client == "qwen_code_turn":
        return "budget 422 does not name max_cost_usd_per_request (Task 7)"
    return None


@pytest.fixture
def dual_fake(_fake_acompletion, monkeypatch):
    # Depends on the autouse fake so this one is installed after it and wins.
    install_dual_fake(monkeypatch)


@pytest.mark.parametrize("setup", sorted(SETUPS))
@pytest.mark.parametrize("recipe", sorted(RECIPES))
@pytest.mark.parametrize("client_name", sorted(CLIENTS))
def test_real_client_is_served_or_told_why(client_name, recipe, setup, dual_fake, request):
    reason = known_bug(client_name, recipe, setup)
    if reason:
        request.applymarker(pytest.mark.xfail(strict=True, reason=reason))
    price_in, price_out = SETUPS[setup]
    with conformance_client() as c:
        disable_seeded_models(c)
        add_model(c, MODEL, tier="cheap", price_input=price_in, price_output=price_out)
        app = onboard(c, f"conf-{recipe}-{client_name}-{setup}".replace("_", "-"),
                      **RECIPES[recipe])
        r = app.chat(CLIENTS[client_name]())

    assert r.status_code not in (500, 502, 503), r.text
    if r.status_code == 200:
        return
    assert r.status_code in (400, 422, 429), r.text
    detail = r.json().get("detail", "")
    assert any(w in detail for w in HINT_WORDS), f"unactionable rejection: {detail!r}"


@pytest.mark.parametrize("recipe", sorted(RECIPES))
def test_every_listed_model_is_servable(recipe, dual_fake):
    """GET /v1/models must never list an id that a plain request then rejects."""
    with conformance_client() as c:
        add_model(c, MODEL, tier="cheap")
        app = onboard(c, f"models-{recipe}".replace("_", "-"), **RECIPES[recipe])
        listed = c.get("/v1/models", headers=app.headers).json()["data"]
        assert listed, "recipe lists no models"
        for m in listed:
            r = app.chat(CLIENTS["langchain_parts_chat"](model=m["id"]))
            assert r.status_code == 200, (m["id"], r.text)
```

- [ ] **Step 5: Run the suite and check the result is exactly the expected red/green**

Run: `rtk proxy bash -c 'cd <worktree> && LITELLM_LOCAL_MODEL_COST_MAP=True /home/manimal/projects/routeForge/.venv/bin/python -m pytest -q -p no:cacheprovider tests/onboarding -rxXf 2>&1 | tail -40'`

Expected: exit 0. Every xfail listed has one of the three `known_bug` reasons, and there are no XPASS and no FAILED lines.

If any cell FAILS or XPASSES that `known_bug` doesn't cover, **stop and report the cell and its response body to the controller**. Don't add new xfail reasons on your own. An unexpected failure is exactly what this suite exists to surface.

If the problem is the harness itself (for example an import name), fix the harness, not the expectation.

- [ ] **Step 6: Commit**

```bash
git add tests/onboarding
git commit -m "test(onboarding): real-client conformance matrix across app policy recipes"
```

---

### Task 2: B24 — `content: null` user turn returns 500

**Files:**
- Modify: `app/routing/task_mode.py:22-25`
- Test: `tests/test_task_mode_null_content.py` (new)
- Modify: `tests/onboarding/test_conformance.py` (delete the B24 line in `known_bug`)
- Modify: `docs/backlog/bugs.md` (mark B24 resolved)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing test**

```python
from app.models import ChatRequest
from app.routing.task_mode import decide


def test_null_content_last_user_turn_is_direct_not_a_crash():
    req = ChatRequest.model_validate({"messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": None}]})
    assert decide(req) == "direct"


def test_null_content_http_is_served(client):
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [{"role": "user", "content": None}]})
    assert r.status_code == 200, r.text
```

Before writing the test, check the function name: run `grep -n "^def " app/routing/task_mode.py`. If the function that reads `last_user` is not called `decide`, use its real name in both the import and the call.

- [ ] **Step 2: Run it and confirm it fails** with `AttributeError: 'NoneType' object has no attribute 'lower'` (unit test) and a 500 (HTTP test).

- [ ] **Step 3: Fix.** In `app/routing/task_mode.py`, change `last_user = m.content` to:

```python
            last_user = m.content or ""
```

- [ ] **Step 4: Run** `tests/test_task_mode_null_content.py tests/onboarding`. Expected: the new tests pass. In `known_bug`, delete these two lines:
  ```python
      if client == "chat_ui_null_content":
          return "B24: task_mode calls .lower() on None content -> 500 (Task 2)"
  ```
  Then rerun `tests/onboarding`. Expected: exit 0, and no `chat_ui_null_content` cell fails.

- [ ] **Step 5: Backlog.** In `docs/backlog/bugs.md`, append ` ✅ RESOLVED (2026-09-15)` to the end of the `## B24` heading line. Under it, add one line: `Fixed: task_mode coerces None content to ""; regression tests/test_task_mode_null_content.py and the conformance matrix.`

- [ ] **Step 6: Commit** `fix(routing): B24 null user content no longer 500s in task_mode`

---

### Task 3: Forward generation parameters (`max_tokens`, `temperature`, …) to the model

Today `ChatRequest` keeps them in `model_extra`, and `LiteLLMBackend.generate` never sends them. So a client's `max_tokens: 8192` and `temperature: 0` are silently ignored.

**Files:**
- Modify: `app/models.py` (`ChatRequest`)
- Modify: `app/gateway/backends/base.py` (`InferenceBackend.generate`, `generate_stream`)
- Modify: `app/gateway/backends/litellm_backend.py` (`generate`, `generate_stream`)
- Modify: `app/gateway/gateway.py` (`run`, `run_stream`)
- Modify: `app/pipeline.py` (the `self._gateway.run(` call near line 542 and the `self._gateway.run_stream(` call near line 768)
- Modify: `tests/_litellm_fake.py` (`_DeterministicLiteLLM.generate`) and **every** other test double that defines `generate`/`generate_stream` (find them with `grep -rn "def generate" tests`)
- Test: `tests/test_generation_params.py` (new)

**Interfaces:**
- Produces:
  - `ChatRequest.generation_params() -> dict`.
  - `FORWARDED_PARAMS` (a tuple in `app/models.py`).
  - A new keyword `params: dict | None = None` on `InferenceBackend.generate`, `InferenceBackend.generate_stream`, `Gateway.run` and `Gateway.run_stream`.

- [ ] **Step 1: Write the failing test** (`tests/test_generation_params.py`)

```python
from tests import _litellm_fake
from tests.onboarding.harness import conformance_client, install_dual_fake, onboard

_AUTH = {"Authorization": "Bearer demo-key"}


def test_generation_params_reach_the_model(client):
    _litellm_fake.CALLS.clear()
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 77, "temperature": 0.2, "top_p": 0.9, "stop": ["END"], "seed": 7})
    assert r.status_code == 200, r.text
    call = _litellm_fake.CALLS[-1]
    assert call["max_tokens"] == 77
    assert call["temperature"] == 0.2
    assert call["top_p"] == 0.9
    assert call["stop"] == ["END"]
    assert call["seed"] == 7


def test_unknown_or_structural_extras_are_not_forwarded(client):
    _litellm_fake.CALLS.clear()
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "hello"}], "n": 3, "foo": "bar"})
    assert r.status_code == 200, r.text
    call = _litellm_fake.CALLS[-1]
    assert "n" not in call and "foo" not in call
    assert call["messages"][-1]["role"] == "user"


def test_generation_params_reach_the_model_when_streaming(_fake_acompletion, monkeypatch):
    install_dual_fake(monkeypatch)
    _litellm_fake.CALLS.clear()
    with conformance_client() as c:
        app = onboard(c, "params-stream", stream_mode="stream_unverified")
        r = app.chat({"stream": True, "max_tokens": 55, "temperature": 0.1,
                      "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    call = _litellm_fake.CALLS[-1]
    assert call["stream"] is True and call["max_tokens"] == 55 and call["temperature"] == 0.1
```

- [ ] **Step 2: Run it and confirm it fails** with `KeyError: 'max_tokens'`.

- [ ] **Step 3: `app/models.py`.** Add this above `class ChatRequest`:

```python
# OpenAI sampling/length knobs a client sets and expects the model to honour.
# Structural keys (model, messages, stream, tools, n) are deliberately absent:
# RouteForge owns those.
FORWARDED_PARAMS = (
    "max_tokens", "max_completion_tokens", "temperature", "top_p", "stop", "seed",
    "presence_penalty", "frequency_penalty", "response_format", "parallel_tool_calls",
)
```

Add this method inside `ChatRequest`, after `tool_choice`:

```python
    def generation_params(self) -> dict:
        """The client's sampling/length parameters, to pass to the model."""
        extra = self.model_extra or {}
        return {k: extra[k] for k in FORWARDED_PARAMS if extra.get(k) is not None}
```

- [ ] **Step 4: Backend protocol and implementation.**

In `app/gateway/backends/base.py`, add `params: dict | None = None` as the last parameter of both `generate` and `generate_stream`, after `tool_choice`.

In `app/gateway/backends/litellm_backend.py`, add the same parameter to both methods. In each, right after the `if tool_choice is not None: call["tool_choice"] = tool_choice` lines, add:

```python
        for k, v in (params or {}).items():
            call.setdefault(k, v)   # never overrides model/messages/stream/tools
```

In `tests/_litellm_fake.py`, change `_DeterministicLiteLLM.generate`'s signature to `async def generate(self, model, messages, price, tier, hint, tools=None, tool_choice=None, params=None):`.

Run `grep -rn "def generate" tests app` and add `params=None` to every other `generate`/`generate_stream` you find.

- [ ] **Step 5: Gateway.** In `app/gateway/gateway.py`:
  - add `params: dict | None = None` after `tool_choice` in both `run` and `run_stream`;
  - pass `params=params` to `self._backend.generate(...)` in `run` and to `self._backend.generate_stream(...)` in `run_stream`.

- [ ] **Step 6: Pipeline.** In `app/pipeline.py`, change the direct-path call (near line 542) to:

```python
        result, records = await self._gateway.run(
            ctx, comp.messages, comp.tokens_after, complexity, hint,
            tools=tools, tool_choice=tool_choice, params=request.generation_params())
```

Change the streaming call (near line 768) to:

```python
        stream, records = await self._gateway.run_stream(
            ctx, comp.messages, comp.tokens_after, complexity, hint,
            params=request.generation_params())
```

- [ ] **Step 7: Run** `tests/test_generation_params.py`, then the **full suite**. Expected: exit 0. If a test double somewhere raises `TypeError: unexpected keyword argument 'params'`, give it the parameter too.

- [ ] **Step 8: Commit** `fix(gateway): forward client max_tokens/temperature/top_p/stop/seed to the model`

---

### Task 4: Correct the per-request cost estimate

Today the estimate is `tokens_after × (price.input + price.output)`, which prices output at the prompt's size. Tool schemas and replayed `tool_calls` are also left out of the token count.

The new estimate is `prompt_tokens × input + expected_output × output`. `expected_output` is the request's `max_completion_tokens` or `max_tokens`, or else the new `budgets.default_estimated_output_tokens` value.

**Files:**
- Modify: `config/policy.yaml` (`budgets`)
- Modify: `app/routing/router.py` (`_est_cost`, `select`)
- Modify: `app/gateway/gateway.py` (`run`, `run_stream`: pass `output_tokens`)
- Modify: `app/pipeline.py` (tool-mode token count near line 530; add `import json`)
- Test: `tests/test_budget_estimate.py` (new)

**Interfaces:**
- Consumes: `params` on `Gateway.run`/`run_stream` (Task 3).
- Produces:
  - `Router.select(ctx, tokens_after, complexity, min_tier=None, exclude=frozenset(), output_tokens: int | None = None)`.
  - `Router._est_cost(cand, tokens_after, output_tokens) -> float`.
  - `app.routing.router.expected_output_tokens(settings, params: dict | None) -> int`.

- [ ] **Step 1: Write the failing test** (`tests/test_budget_estimate.py`)

```python
import pytest

from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.policy.context import build_context
from app.routing.router import Router, expected_output_tokens
from tests._litellm_fake import make_test_registry
from tests.onboarding.harness import (
    add_model, conformance_client, disable_seeded_models, onboard,
)


def _router_ctx():
    s = load_settings()
    reg = make_test_registry(s)
    ctx = build_context(s, "req-b", "acme", [], None,
                        app_policy=AppPolicyView(residency="any"), registry=reg)
    return s, reg, ctx


def test_estimate_prices_output_tokens_at_output_price():
    s, reg, ctx = _router_ctx()
    d = Router(s, registry=reg).select(ctx, 1000, 0.1, output_tokens=8192)
    assert d.chosen.name == "local-8b"            # $0.05 in / $0.05 out per 1M
    assert d.est_cost_usd == pytest.approx((1000 * 0.05 + 8192 * 0.05) / 1_000_000)


def test_estimate_uses_default_output_tokens_when_unset():
    s, reg, ctx = _router_ctx()
    default = int(s.policy.budgets["default_estimated_output_tokens"])
    d = Router(s, registry=reg).select(ctx, 1000, 0.1)
    assert d.est_cost_usd == pytest.approx((1000 * 0.05 + default * 0.05) / 1_000_000)


def test_expected_output_tokens_prefers_max_completion_tokens():
    s = load_settings()
    assert expected_output_tokens(s, {"max_completion_tokens": 100, "max_tokens": 9}) == 100
    assert expected_output_tokens(s, {"max_tokens": 9}) == 9
    assert expected_output_tokens(s, None) == int(s.policy.budgets["default_estimated_output_tokens"])


def test_max_tokens_counts_toward_the_budget_over_http():
    # 1 prompt token * $10 + 8192 output * $10 = $0.0819 > $0.05 default cap.
    with conformance_client() as c:
        disable_seeded_models(c)
        add_model(c, "conf-model", price_input=10.0, price_output=10.0)
        app = onboard(c, "est-http")
        big = app.chat({"max_tokens": 8192, "messages": [{"role": "user", "content": "hi"}]})
        small = app.chat({"messages": [{"role": "user", "content": "hi"}]})
    assert big.status_code == 422, big.text
    assert small.status_code == 200, small.text


def test_tool_schemas_count_as_prompt_tokens(client):
    tools = [{"type": "function", "function": {"name": f"t{i}", "description": "x" * 400,
              "parameters": {"type": "object", "properties": {}}}} for i in range(5)]
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"tools": tools, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    comp = next(e for e in r.json()["trace"] if "tokens_before" in e)
    assert comp["tokens_before"] >= 500     # 5 x ~400 chars of schema / 4
```

- [ ] **Step 2: Run it and confirm it fails.** Expected: `ImportError` (no `expected_output_tokens`) or `TypeError` (no `output_tokens`).

- [ ] **Step 3: Config.** In `config/policy.yaml`, under `budgets:`, add:

```yaml
  # Output tokens assumed by the per-request cost estimate when a request sets
  # no max_tokens / max_completion_tokens.
  default_estimated_output_tokens: 512
```

- [ ] **Step 4: Router.** In `app/routing/router.py`, add at module level after `_ORDER`:

```python
def expected_output_tokens(settings: Settings, params: dict | None) -> int:
    """Output tokens to price a request at: the client's own cap, else the
    deployment's `budgets.default_estimated_output_tokens`."""
    p = params or {}
    for key in ("max_completion_tokens", "max_tokens"):
        if p.get(key):
            return int(p[key])
    return int(settings.policy.budgets["default_estimated_output_tokens"])
```

Replace `_est_cost` with:

```python
    def _est_cost(self, cand: PoolCandidate, tokens_after: int, output_tokens: int) -> float:
        p = (self._reg.price_for(cand.name) if self._reg
             else self._s.pricing.models[cand.name])
        return (tokens_after * p.input + output_tokens * p.output) / 1_000_000
```

Add `output_tokens: int | None = None` as the last parameter of `select`. As the first line of its body, add:

```python
        out_tokens = (output_tokens if output_tokens is not None
                      else expected_output_tokens(self._s, None))
```

Change `est = self._est_cost(c, tokens_after)` to `est = self._est_cost(c, tokens_after, out_tokens)`.

- [ ] **Step 5: Gateway.** In `app/gateway/gateway.py`:
  - import `expected_output_tokens` from `app.routing.router`.
  - In `run`, before `while True:`, add `out_tokens = expected_output_tokens(self._s, params)`, and pass `output_tokens=out_tokens` to `self._router.select(...)`.
  - In `run_stream`, pass `output_tokens=expected_output_tokens(self._s, params)` to its `self._router.select(...)`.

- [ ] **Step 6: Pipeline tool-mode tokens.** In `app/pipeline.py`, add `import json` after `import logging`. Replace `tb = sum(count_tokens(m.content) for m in san_messages)` with:

```python
            # Tool schemas and replayed tool_call arguments are billed prompt
            # tokens too; leaving them out under-priced every agent turn.
            tb = (sum(count_tokens(m.content) for m in san_messages)
                  + sum(count_tokens(json.dumps(m.tool_calls))
                        for m in san_messages if m.tool_calls)
                  + (count_tokens(json.dumps(tools)) if tools else 0))
```

- [ ] **Step 7: Run** `tests/test_budget_estimate.py`, then the **full suite**.

Tests that assert an exact `est_cost_usd` computed with the old formula may fail. For each one, recompute the expected value with the new formula, `(prompt × in + output × out) / 1e6` with output defaulting to 512, and update the literal. Never loosen an assertion to `>0`. List every test you changed in your report.

Expected: exit 0.

- [ ] **Step 8: Commit** `fix(routing): price output at max_tokens x output price; count tool schemas as prompt`

---

### Task 5: Fall back to a cheaper tier when the target tier is over budget

Today the router picks one tier (complexity, risk floor, escalation), drops over-budget candidates **only within that tier**, and returns 422, even when a cheaper allowed tier fits. The fallback may go down as far as the complexity-driven target allows, but **never below the risk floor or an escalation `min_tier`**. It only ever considers `allowed` candidates, so residency is untouched.

**Files:**
- Modify: `app/routing/router.py` (`select`, steps 3–4)
- Test: `tests/test_router_budget_fallback.py` (new)

**Interfaces:**
- Consumes: `output_tokens` / `_est_cost(c, tokens_after, out_tokens)` (Task 4).
- Produces: when the fallback is taken, `RoutingDecision.reason` contains `"budget fallback from <tier>"`. The rejected candidates in the target tier are labelled `"over budget"`.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.errors import NoEligibleCandidate
from app.policy.context import build_context
from app.routing.router import Router
from tests._litellm_fake import make_test_registry

# local-8b: cheap $0.05/$0.05; local-32b: mid $0.10/$0.10 (per 1M).
# 2000 prompt tokens -> complexity 1.0 -> frontier target, clamped to mid.
# mid est = (2000*.10 + 100*.10)/1e6 = 0.00021; cheap est = 0.000105.


def _ctx(risk_class="low", cap=0.00015):
    s = load_settings()
    reg = make_test_registry(s)
    ctx = build_context(s, "req-f", "acme", [], None,
                        app_policy=AppPolicyView(residency="any"), registry=reg)
    return s, reg, ctx.model_copy(update={"max_cost_usd": cap, "risk_class": risk_class})


def test_over_budget_target_tier_falls_back_to_cheaper_allowed_tier():
    s, reg, ctx = _ctx()
    d = Router(s, registry=reg).select(ctx, 2000, 1.0, output_tokens=100)
    assert d.chosen.name == "local-8b"
    assert "budget fallback from mid" in d.reason
    assert {r.name: r.reason for r in d.rejected}["local-32b"] == "over budget"


def test_fallback_never_goes_below_the_risk_floor():
    s, reg, ctx = _ctx(risk_class="medium")        # risk floor mid
    with pytest.raises(NoEligibleCandidate, match="no candidates after budget filter"):
        Router(s, registry=reg).select(ctx, 2000, 1.0, output_tokens=100)


def test_fallback_never_goes_below_escalation_min_tier():
    s, reg, ctx = _ctx()
    with pytest.raises(NoEligibleCandidate, match="no candidates after budget filter"):
        Router(s, registry=reg).select(ctx, 2000, 1.0, min_tier="mid", output_tokens=100)


def test_no_fallback_needed_keeps_old_reason():
    s, reg, ctx = _ctx(cap=1.0)
    d = Router(s, registry=reg).select(ctx, 2000, 1.0, output_tokens=100)
    assert d.chosen.name == "local-32b" and "fallback" not in d.reason
```

Before relying on them, check the risk floors: `grep -n -A4 "risk_floors" config/routing.yaml` must show `low: cheap` and `medium: mid`. If not, choose the `risk_class` whose floor is `mid` for the second test.

- [ ] **Step 2: Run it and confirm it fails.** The first test should raise `NoEligibleCandidate`.

- [ ] **Step 3: Implement.** In `Router.select`, replace this block:

```python
        if not eligible:
            raise NoEligibleCandidate("no candidates after budget filter")
```

with:

```python
        if not eligible:
            # Budget fallback: step DOWN to the most capable cheaper allowed
            # tier that fits the cap. Floor = risk floor / escalation min_tier
            # (never undercut; complexity is the only thing budget may
            # override). Only `allowed` candidates are considered, so
            # residency and app scope are untouched (rules 3/8).
            floor_tiers = [self._s.routing.risk_floors[ctx.risk_class]]
            if min_tier:
                floor_tiers.append(min_tier)
            floor = max(floor_tiers, key=lambda t: _ORDER[t])
            lower = sorted((t for t in avail
                            if _ORDER[floor] <= _ORDER[t] < _ORDER[tier_pick]),
                           key=lambda t: -_ORDER[t])
            for t in lower:
                fits = [(c, e) for c, e in
                        ((c, self._est_cost(c, tokens_after, out_tokens))
                         for c in allowed if c.tier == t)
                        if e <= ctx.max_cost_usd]
                if fits:
                    fits_names = {c.name for c, _ in fits}
                    rejected = [r for r in rejected
                                if not (r.tier == t and r.reason == "below target tier")]
                    rejected.extend(
                        RejectedCandidate(name=c.name, provider=c.provider, tier=c.tier,
                                          display_name=c.display_name, reason="over budget")
                        for c in allowed if c.tier == t and c.name not in fits_names)
                    fallback_from, tier_pick, eligible = tier_pick, t, fits
                    break
        if not eligible:
            raise NoEligibleCandidate("no candidates after budget filter")
```

Initialise `fallback_from: str | None = None` just above the `# 3. keep only the target tier` comment. Change the final `reason=` to:

```python
            reason=(f"tier {chosen.tier} (target {target}, budget fallback from {fallback_from})"
                    if fallback_from else f"tier {chosen.tier} (target {target})"),
```

- [ ] **Step 4: Run** `tests/test_router_budget_fallback.py`, then the **full suite**. Expected: exit 0.

- [ ] **Step 5: Commit** `feat(routing): fall back to a cheaper allowed tier when the target tier is over budget`

---

### Task 6: Per-app `max_cost_usd_per_request`

**Files:**
- Modify: `app/apps/models.py` (`AppPolicy`: new column after `max_spend_usd_per_day`)
- Modify: `app/apps/store.py:240` (`POLICY_FIELDS`)
- Modify: `app/apps/policy.py` (`AppPolicyView` field, `version()`, `policy_view_from`)
- Modify: `app/api/admin_routes.py` (`PolicyIn`, `_policy_dict`, `set_policy` range check)
- Modify: `app/policy/context.py` (`build_context` `max_cost_usd=`)
- Modify: `scripts/migrate_app_policies.py` (`COLUMNS`)
- Modify: `ui/src/api.ts` (policy type near line 248), `ui/src/admin/AppSettings.tsx` (lines ~132, ~372, ~950, ~1562)
- Modify: `docs/reference/per-app-config.md` (new field)
- Test: `tests/test_per_request_cap.py` (new)

**Interfaces:**
- Produces: `AppPolicyView.max_cost_usd_per_request: float | None`. This is also a policy column and a `PolicyIn` field of the same name. `ctx.max_cost_usd` equals it when set, else `budgets.default_max_spend_usd`.

- [ ] **Step 1: Write the failing test**

```python
from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.policy.context import build_context
from tests._litellm_fake import make_test_registry
from tests.onboarding.clients import qwen_code_turn
from tests.onboarding.harness import (
    add_model, conformance_client, disable_seeded_models, onboard, set_policy_direct,
)
from tests.test_admin_api import _login


def _ctx(view):
    s = load_settings()
    return s, build_context(s, "r", "acme", [], None, app_policy=view,
                            registry=make_test_registry(s))


def test_cap_defaults_to_deployment_value():
    s, ctx = _ctx(AppPolicyView())
    assert ctx.max_cost_usd == s.policy.budgets["default_max_spend_usd"]


def test_cap_overrides_deployment_value():
    _, ctx = _ctx(AppPolicyView(max_cost_usd_per_request=1.5))
    assert ctx.max_cost_usd == 1.5


def test_unset_cap_leaves_policy_version_unchanged_and_set_cap_moves_it():
    assert AppPolicyView(max_cost_usd_per_request=None).version() == AppPolicyView().version()
    assert AppPolicyView(max_cost_usd_per_request=1.0).version() != AppPolicyView().version()


def test_admin_put_validates_and_round_trips(client):
    _login(client)
    client.post("/admin/applications", json={"id": "capapp", "name": "capapp"})
    bad = client.put("/admin/applications/capapp/policy", json={"max_cost_usd_per_request": 0})
    assert bad.status_code == 422 and "max_cost_usd_per_request" in bad.json()["detail"]
    ok = client.put("/admin/applications/capapp/policy", json={"max_cost_usd_per_request": 1.0})
    assert ok.status_code == 200, ok.text
    assert ok.json()["policy"]["max_cost_usd_per_request"] == 1.0


def test_per_app_cap_unblocks_a_large_agent_turn(_fake_acompletion, monkeypatch):
    from tests.onboarding.harness import install_dual_fake
    install_dual_fake(monkeypatch)
    turn = {**qwen_code_turn(), "stream": False}
    with conformance_client() as c:
        disable_seeded_models(c)
        add_model(c, "conf-model", price_input=10.0, price_output=10.0)
        app = onboard(c, "cap-agent")
        assert app.chat(turn).status_code == 422
        set_policy_direct(c, "cap-agent", max_cost_usd_per_request=1.0)
        r = app.chat(turn)
    assert r.status_code == 200, r.text
```

- [ ] **Step 2: Run it and confirm it fails.** Expected: `TypeError: unexpected keyword argument 'max_cost_usd_per_request'`.

- [ ] **Step 3: Model, store, view.**

In `app/apps/models.py`, add this directly after the `max_spend_usd_per_day` column:

```python
    # Per-request cost cap (USD) for this app. null = inherit
    # policy.yaml budgets.default_max_spend_usd. Agent clients send 25K-token
    # turns, which a deployment-wide $0.05 cap rejects on any priced model.
    max_cost_usd_per_request: Mapped[float | None] = mapped_column(Float, nullable=True)
```

In `app/apps/store.py` `POLICY_FIELDS`, add `"max_cost_usd_per_request"`.

In `app/apps/policy.py`:
- Add the field `max_cost_usd_per_request: float | None = None` after `max_spend_usd_per_day`.
- In `policy_view_from`, add `max_cost_usd_per_request=p.max_cost_usd_per_request,`.
- In `version()`, change `json.dumps(\n            {` so the dict gets one extra entry only when the field is set. That keeps existing apps' version hashes unchanged. Replace the opening `{` of the dict literal with `{**({"max_cost_usd_per_request": self.max_cost_usd_per_request} if self.max_cost_usd_per_request is not None else {}),`, keep all the existing entries, and close as before.

- [ ] **Step 4: Context.** In `app/policy/context.py` `build_context`, replace `max_cost_usd=settings.policy.budgets["default_max_spend_usd"],` with:

```python
        max_cost_usd=(app_policy.max_cost_usd_per_request
                      if app_policy is not None
                      and app_policy.max_cost_usd_per_request is not None
                      else settings.policy.budgets["default_max_spend_usd"]),
```

- [ ] **Step 5: Admin.** In `app/api/admin_routes.py`:
- Add `max_cost_usd_per_request: float | None = None` to `PolicyIn` after `max_spend_usd_per_day`.
- Add `"max_cost_usd_per_request": p.max_cost_usd_per_request,` to `_policy_dict`.
- In `set_policy`, right after the `pii_score_threshold` range check, add:

```python
        if body.max_cost_usd_per_request is not None and not body.max_cost_usd_per_request > 0:
            raise HTTPException(
                422, "max_cost_usd_per_request must be > 0. Omit it, or name it in "
                "`reset`, to inherit the deployment's budgets.default_max_spend_usd.")
```

- [ ] **Step 6: Migration script.** In `scripts/migrate_app_policies.py` `COLUMNS`, append after the `pii_score_threshold` entry:

```python
    ("policy_mode", "VARCHAR(16)", "VARCHAR(16)"),
    ("pii_exempt_patterns", "JSON", "JSON"),
    # 2026-09-15 onboarding conformance — per-app per-request cost cap.
    ("max_cost_usd_per_request", "DOUBLE PRECISION", "FLOAT"),
```

- [ ] **Step 7: UI.**
- In `ui/src/api.ts`, add `max_cost_usd_per_request: number | null;` right after `max_spend_usd_per_day: number | null;`.
- In `ui/src/admin/AppSettings.tsx`:
  - line ~132: change `"max_spend_usd_per_day"]` to `"max_spend_usd_per_day", "max_cost_usd_per_request"]`.
  - line ~372: after the `$/day` push, add `if (p.max_cost_usd_per_request) limits.push(\`$${p.max_cost_usd_per_request}/request\`);`.
  - line ~950: add `max_cost_usd_per_request: null,` next to `max_spend_usd_per_day: null,`.
  - after the `budget $/day` `<Row>` (line ~1566), add:

```tsx
        <Row label="cost cap $/request" htmlFor={`${uid}-reqcap`}
          hint="estimated cost cap per request; blank = deployment default (agent clients need more)">
          <input id={`${uid}-reqcap`} type="number" min={0} step="0.01" value={p.max_cost_usd_per_request ?? ""}
            onChange={(e) => setP({ ...p, max_cost_usd_per_request: e.target.value ? Number(e.target.value) : null })}
            className={`${inputCls} w-24`} />
        </Row>
```

Then type-check with `rtk proxy bash -c 'cd <worktree>/ui && npx tsc --noEmit -p . 2>&1 | tail -5'`. If `node_modules` is missing, skip the check and say so in your report.

- [ ] **Step 8: Docs.** In `docs/reference/per-app-config.md`, add a subsection next to wherever `max_rpm` or the daily budget is described. If neither is described, add it at the end of the field reference:

```markdown
### `max_cost_usd_per_request`

Estimated-cost cap for one request, in USD. `null` inherits `budgets.default_max_spend_usd` (policy.yaml, 0.05). The router estimates `prompt_tokens × input price + (max_tokens or budgets.default_estimated_output_tokens) × output price`. It falls back to a cheaper allowed tier before rejecting, and never goes below the risk floor. A coding agent (≈25K prompt tokens, `max_tokens` 8192) on a model priced $10/$10 per 1M estimates ≈$0.33 and needs this raised, or the model priced realistically. On-prem models are often priced 0.
```

- [ ] **Step 9: Run** `tests/test_per_request_cap.py`, then the **full suite**. Expected: exit 0.

- [ ] **Step 10: Commit** `feat(policy): per-app max_cost_usd_per_request cap`

---

### Task 7: Rejections that name what to change

**Files:**
- Modify: `app/routing/router.py` (both `NoEligibleCandidate` raises)
- Modify: `app/policy/context.py` (new `pinned_model_reason`)
- Modify: `app/pipeline.py:412-414` (pinned-model message)
- Modify: `app/gateway/gateway.py` (`BackendUnavailable` messages carry the last upstream error)
- Modify: `app/api/routes.py` (502 detail; both stream-400 sites)
- Modify: `tests/test_error_taxonomy.py:37`
- Modify: `tests/onboarding/test_conformance.py` (delete the two Task 7 lines in `known_bug`)
- Test: `tests/test_rejection_messages.py` (new)

**Interfaces:**
- Consumes: `ctx.max_cost_usd`, `out_tokens` (Tasks 4–6).
- Produces: `app.policy.context.pinned_model_reason(pool, ctx, registry, tenant_id, name) -> str`.

- [ ] **Step 1: Write the failing test**

```python
from tests.onboarding.clients import qwen_code_turn
from tests.onboarding.harness import (
    add_model, conformance_client, disable_seeded_models, onboard,
)


def _detail(r):
    return r.json()["detail"]


def test_onprem_only_with_no_onprem_model_names_residency_and_onboarding():
    with conformance_client() as c:
        disable_seeded_models(c)
        app = onboard(c, "msg-res", residency="onprem_only")
        r = app.chat({"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 422
    assert "no candidates after residency filter" in _detail(r)
    assert "residency" in _detail(r) and "onboard" in _detail(r)


def test_scope_miss_names_the_allowlist():
    with conformance_client() as c:
        app = onboard(c, "msg-scope", allowed_models=["no-such-model"])
        r = app.chat({"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 422 and "allowed_models" in _detail(r)


def test_budget_rejection_shows_estimate_cap_and_field():
    with conformance_client() as c:
        disable_seeded_models(c)
        add_model(c, "conf-model", price_input=10.0, price_output=10.0)
        app = onboard(c, "msg-budget")
        r = app.chat({**qwen_code_turn(), "stream": False})
    d = _detail(r)
    assert r.status_code == 422 and "no candidates after budget filter" in d
    assert "max_cost_usd_per_request" in d and "conf-model" in d and "$0.0500" in d


def test_pinned_model_rejection_names_the_gate():
    with conformance_client() as c:
        app = onboard(c, "msg-pin", allowed_tiers=["cheap"])
        r = app.chat({"model": "local-32b", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 422
    assert _detail(r).startswith("model request rejected: requested model 'local-32b'")
    assert "allowed_tiers" in _detail(r)


def test_502_carries_the_upstream_error(client, monkeypatch):
    from app.gateway.backends import litellm_backend

    async def refuse(**k):
        raise RuntimeError("connection refused by 10.0.0.5:8000")

    monkeypatch.setattr(litellm_backend, "acompletion", refuse)
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 502
    assert _detail(r).startswith("no backend could serve this request")
    assert "connection refused by 10.0.0.5:8000" in _detail(r)


def test_stream_rejection_names_stream_mode():
    with conformance_client() as c:
        app = onboard(c, "msg-stream")
        r = app.chat({"stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400 and "stream_mode" in _detail(r)
```

- [ ] **Step 2: Run it and confirm it fails.** Expect every test except possibly the first to fail on its message assertion.

- [ ] **Step 3: Router messages.** In `app/routing/router.py`, add at module level:

```python
from collections import Counter

# What an operator changes for each per-candidate rejection reason.
_FIX_HINTS = {
    "excluded · residency": "request or application is onprem_only: onboard and enable an on-prem model",
    "not permitted for application": "check the application's allowed_providers / allowed_tiers / allowed_models and the model's allowed_app_ids",
    "not requested model": "the request pinned a different model (see GET /v1/models)",
    "disabled by admin": "the model is disabled: re-enable it under Infrastructure",
    "excluded · prior failure": "the model already failed on this request",
}


def _explain(rejected) -> str:
    counts = Counter(r.reason for r in rejected)
    return "; ".join(f"{n} {reason} ({_FIX_HINTS.get(reason, reason)})"
                     for reason, n in counts.most_common())
```

Replace `raise NoEligibleCandidate("no candidates after residency filter")` with:

```python
            if not pool:
                raise NoEligibleCandidate(
                    "no candidates after residency filter: the model pool is empty; "
                    "onboard a model (residency cannot be satisfied by an empty pool)")
            raise NoEligibleCandidate(
                f"no candidates after residency filter: {_explain(rejected)}")
```

For the budget raise:
- Initialise `over: list[tuple[PoolCandidate, float]] = []` next to `eligible`.
- In the tier loop's over-budget branch, add `over.append((c, est))`.
- Replace the final `raise NoEligibleCandidate("no candidates after budget filter")` (the one left after Task 5's fallback) with:

```python
            cheapest, cheapest_est = min(over, key=lambda ce: ce[1])
            raise NoEligibleCandidate(
                f"no candidates after budget filter: cheapest candidate {cheapest.name} "
                f"(tier {cheapest.tier}) is estimated at ${cheapest_est:.4f} "
                f"({tokens_after} prompt + {out_tokens} output tokens), over the per-request "
                f"cap ${ctx.max_cost_usd:.4f}. Raise the application's "
                "max_cost_usd_per_request, lower max_tokens, or lower the model's price.")
```

`over` is never empty when this raise is reached: every target-tier candidate either landed in `eligible` or in `over`.

- [ ] **Step 4: Pinned model.** In `app/policy/context.py`, add after `registry_permits`:

```python
def pinned_model_reason(pool, ctx: ExecutionPolicyContext, registry: "ModelRegistry | None",
                        tenant_id: str, name: str) -> str:
    """Which gate refused a pinned `model`, phrased as what to change."""
    c = next((c for c in pool if c.name == name), None)
    if c is None:
        return "it is not onboarded, or is disabled (see GET /v1/models)"
    if c.provider not in ctx.allowed_providers:
        return (f"provider {c.provider!r} is excluded by residency or the "
                "application's allowed_providers")
    if ctx.model_scope_active and name not in ctx.allowed_models:
        return "it is outside the application's allowed_tiers / allowed_models"
    if registry is not None and not registry.is_allowed_for_app(name, tenant_id):
        return "the model's allowed_app_ids does not include this application"
    return "it is disabled"
```

In `app/pipeline.py`, add `pinned_model_reason` to the existing `from app.policy.context import ...` line, and replace the `raise PipelineRejected(...)` at line ~413 with:

```python
                raise PipelineRejected(
                    f"requested model {ctx.requested_model!r} is not permitted or "
                    f"unavailable: {pinned_model_reason(pool, ctx, self._reg, tenant_id, ctx.requested_model)}")
```

- [ ] **Step 5: 502 detail.** In `app/gateway/gateway.py` `run`:
- Initialise `last_error = ""` next to `failed_models`.
- In the `except Exception as exc:` block, first line, add `last_error = f"{cand.name}: {type(exc).__name__}: {str(exc)[:200]}"`.
- Change both `BackendUnavailable("backend failed on all reachable candidates for this request")` messages to:
  ```python
  f"backend failed on all reachable candidates for this request (last error: {last_error})"
  ```

In `app/api/routes.py`:
- change `except BackendUnavailable:` to `except BackendUnavailable as e:`;
- change the detail to `f"no backend could serve this request: {e}"`.

In `tests/test_error_taxonomy.py:37`, change the equality to `assert r.json()["detail"].startswith("no backend could serve this request")`.

- [ ] **Step 6: Stream 400.** In `app/api/routes.py`, add at module level:

```python
_STREAM_REJECT = ("streaming is not supported for this application: set the "
                  "application's stream_mode (verified_then_stream, stream_unverified "
                  "or incremental_verify)")
```

Replace both `HTTPException(400, "streaming is not supported for this application")` with `HTTPException(400, _STREAM_REJECT)`.

- [ ] **Step 7: Conformance.** In `tests/onboarding/test_conformance.py` `known_bug`, delete the lines:

```python
    if recipe == "default" and client in STREAM_CLIENTS:
        return "stream 400 does not name stream_mode (Task 7)"
    if setup == "priced_10" and client == "qwen_code_turn":
        return "budget 422 does not name max_cost_usd_per_request (Task 7)"
```

If `known_bug` now has no branches left, keep the function with just `return None` so the test body is unchanged. Remove the now-unused `STREAM_CLIENTS` import if your linter flags it.

- [ ] **Step 8: Run** `tests/test_rejection_messages.py tests/onboarding tests/test_error_taxonomy.py`, then the **full suite**. Expected: exit 0, with **no xfail** left in `tests/onboarding`.

- [ ] **Step 9: Commit** `fix(api): every rejection names the policy field, estimate or upstream error to act on`

---

### Task 8: Refuse impossible policies at save time; warn about unroutable ones

**Files:**
- Create: `app/apps/policy_lint.py`
- Modify: `app/api/admin_routes.py` (`set_policy`, `onboard_model`, `edit_onboarded_model`)
- Test: `tests/test_policy_save_validation.py` (new)

**Interfaces:**
- Consumes:
  - `requestable_models(settings, registry, tenant_id, app_policy)` from `app/policy/context.py`;
  - `policy_view_from` from `app/apps/policy.py`;
  - `store.list_onboarded(session)`;
  - `AppPolicyView.max_cost_usd_per_request` (Task 6).
- Produces:
  - `app.apps.policy_lint.policy_warnings(settings, registry, app_id, view) -> list[str]`;
  - `app.apps.policy_lint.price_warnings(settings, price_input, price_output) -> list[str]`;
  - a `"warnings"` key on the responses of `PUT /admin/applications/{id}/policy`, `POST /admin/infra/models` and `PUT /admin/infra/models/{slug}`.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from tests.onboarding.harness import conformance_client, disable_seeded_models
from tests.test_admin_api import _login


@pytest.fixture
def admin():
    with conformance_client() as c:
        _login(c)
        c.post("/admin/applications", json={"id": "lint", "name": "lint"})
        yield c


def _put(c, **body):
    return c.put("/admin/applications/lint/policy", json=body)


@pytest.mark.parametrize("body,needle", [
    ({"allowed_tiers": ["strong"]}, "unknown tier"),
    ({"allowed_providers": ["on-prem"]}, "unknown provider"),
    ({"allowed_models": ["no-such-model"]}, "unknown model"),
    ({"allowed_tiers": []}, "empty list"),
    ({"allowed_models": []}, "empty list"),
    ({"max_rpm": 0}, "max_rpm"),
    ({"max_spend_usd_per_day": 0}, "max_spend_usd_per_day"),
])
def test_impossible_policy_is_refused(admin, body, needle):
    r = _put(admin, **body)
    assert r.status_code == 422, r.text
    assert needle in r.json()["detail"]


def test_valid_policy_saves_with_no_warnings(admin):
    r = _put(admin, allowed_models=["local-8b"])
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == []


def test_disabled_onboarded_model_is_a_known_slug(admin):
    from tests.onboarding.harness import add_model
    add_model(admin, "parked", enabled=False)
    assert _put(admin, allowed_models=["parked"]).status_code == 200


def test_unroutable_policy_saves_with_a_warning(admin):
    disable_seeded_models(admin)
    r = _put(admin, residency="onprem_only")
    assert r.status_code == 200, r.text
    assert any("no model is currently requestable" in w for w in r.json()["warnings"])


def test_price_that_blocks_agent_turns_warns_on_onboarding(admin, monkeypatch):
    import app.api.admin_routes as ar

    async def ok_probe(*a, **k):
        return True, ""

    monkeypatch.setattr(ar, "probe_endpoint", ok_probe)
    r = admin.post("/admin/infra/models", json={
        "slug": "pricey", "display_name": "pricey", "tier": "cheap",
        "endpoint": "http://test-local/v1", "price_input": 10.0, "price_output": 10.0})
    assert r.status_code == 200, r.text
    assert any("max_cost_usd_per_request" in w for w in r.json()["warnings"])
    cheap = admin.post("/admin/infra/models", json={
        "slug": "free", "display_name": "free", "tier": "cheap",
        "endpoint": "http://test-local/v1", "price_input": 0.0, "price_output": 0.0})
    assert cheap.json()["warnings"] == []


def test_app_warns_when_every_model_is_over_its_cap(admin):
    from tests.onboarding.harness import add_model
    disable_seeded_models(admin)
    add_model(admin, "pricey", price_input=10.0, price_output=10.0)
    r = _put(admin, allowed_models=["pricey"])
    assert r.status_code == 200
    assert any("typical coding-agent turn" in w for w in r.json()["warnings"])
```

Check the `OnboardIn` required fields with `grep -n "class OnboardIn" -A20 app/api/admin_routes.py`. If it requires more fields than the test sends, add them to both POST bodies with valid values.

- [ ] **Step 2: Run it and confirm it fails.** Expected: 200 instead of 422, and `KeyError: 'warnings'`.

- [ ] **Step 3: Write `app/apps/policy_lint.py`**

```python
"""Operator feedback at save time: configurations that will reject real traffic.

Hard impossibilities are refused (422) in admin_routes.set_policy. Everything
here is a WARNING: the configuration is legal and may be intentional or
temporary (e.g. an app created before its model is onboarded), but the
operator must see that requests will be rejected before a client finds out."""
from __future__ import annotations

from app.apps.policy import AppPolicyView
from app.config.schema import Settings
from app.policy.context import requestable_models

# A typical coding-agent turn (Qwen Code / Claude-Code-style clients).
AGENT_PROMPT_TOKENS = 25_000
AGENT_OUTPUT_TOKENS = 8_192


def _agent_turn_cost(price_input: float, price_output: float) -> float:
    return (AGENT_PROMPT_TOKENS * price_input + AGENT_OUTPUT_TOKENS * price_output) / 1_000_000


def price_warnings(settings: Settings, price_input: float, price_output: float) -> list[str]:
    cap = settings.policy.budgets["default_max_spend_usd"]
    est = _agent_turn_cost(price_input, price_output)
    if est <= cap:
        return []
    return [f"at ${price_input}/${price_output} per 1M tokens, a typical coding-agent turn "
            f"({AGENT_PROMPT_TOKENS} prompt + {AGENT_OUTPUT_TOKENS} output tokens) is estimated "
            f"at ${est:.4f}, over the deployment's default per-request cap ${cap:.4f}. Apps using "
            "this model need max_cost_usd_per_request raised, or lower the price (on-prem models "
            "are often priced 0)."]


def policy_warnings(settings: Settings, registry, app_id: str, view: AppPolicyView) -> list[str]:
    models = requestable_models(settings, registry, app_id, view)
    if not models:
        return ["no model is currently requestable for this application, so every request "
                "will be rejected with 422. Check residency (onprem_only needs an enabled "
                "on-prem model), allowed_providers, allowed_tiers, allowed_models, and the "
                "models' allowed_app_ids."]
    cap = (view.max_cost_usd_per_request if view.max_cost_usd_per_request is not None
           else settings.policy.budgets["default_max_spend_usd"])
    over = [c.name for c in models
            if _agent_turn_cost(registry.price_for(c.name).input,
                                registry.price_for(c.name).output) > cap]
    if len(over) == len(models):
        return [f"a typical coding-agent turn ({AGENT_PROMPT_TOKENS} prompt + "
                f"{AGENT_OUTPUT_TOKENS} output tokens) exceeds this application's per-request "
                f"cap ${cap:.4f} on every requestable model ({', '.join(sorted(over))}). Raise "
                "max_cost_usd_per_request or lower the model price."]
    return []
```

- [ ] **Step 4: Save-time refusals in `set_policy`.** In `app/api/admin_routes.py`, add near `_static_validate_onboard`:

```python
async def _validate_policy_values(body: "PolicyIn", settings, session) -> None:
    """Refuse values that can never route. Empty allowlists are refused because
    the store reads [] as 'no restriction', the opposite of what an operator
    who clears a list means."""
    for field in ("allowed_tiers", "allowed_providers", "allowed_models"):
        v = getattr(body, field)
        if v is not None and len(v) == 0:
            raise HTTPException(
                422, f"{field} is an empty list, which RouteForge reads as 'no "
                "restriction'. Omit the field to leave it unchanged, or name it in "
                "`reset` to clear it.")
    if body.allowed_tiers:
        bad = sorted(set(body.allowed_tiers) - set(settings.routing.tiers))
        if bad:
            raise HTTPException(
                422, f"unknown tier(s) {bad}; legal tiers: {list(settings.routing.tiers)}")
    if body.allowed_providers:
        bad = sorted(set(body.allowed_providers) - set(settings.serving.providers))
        if bad:
            raise HTTPException(
                422, f"unknown provider(s) {bad}; legal providers: "
                f"{sorted(settings.serving.providers)}")
    if body.allowed_models:
        known = ({c.name for c in settings.routing.model_pool}
                 | {m.slug for m in await store.list_onboarded(session)})
        bad = sorted(set(body.allowed_models) - known)
        if bad:
            raise HTTPException(
                422, f"unknown model(s) {bad}: not in the YAML pool and not onboarded")
    if body.max_rpm is not None and body.max_rpm < 1:
        raise HTTPException(
            422, "max_rpm must be >= 1 (0 rejects every request). Omit it, or reset it, "
            "for no rate limit.")
    if body.max_spend_usd_per_day is not None and not body.max_spend_usd_per_day > 0:
        raise HTTPException(
            422, "max_spend_usd_per_day must be > 0 (0 rejects every request). Omit it, "
            "or reset it, for no daily cap.")
```

In `set_policy`, directly after the line `stored = app.policy`, add `await _validate_policy_values(body, settings, session)`.

Replace the final `return _app_dict(full)` of `set_policy` with:

```python
        registry = request.app.state.registry
        await registry.ensure_loaded(request.app.state.db)
        from app.apps.policy import policy_view_from
        from app.apps.policy_lint import policy_warnings
        return {**_app_dict(full),
                "warnings": policy_warnings(settings, registry, app_id, policy_view_from(full))}
```

- [ ] **Step 5: Onboarding warnings.** In both `onboard_model` and `edit_onboarded_model`, add `"warnings": price_warnings(s, body.price_input, body.price_output),` to the returned dict. Add `from app.apps.policy_lint import price_warnings` to the module imports. If that import is circular, import it inside each function instead.

- [ ] **Step 6: Run** `tests/test_policy_save_validation.py`, then the **full suite**.

Existing admin tests that PUT an empty list, an unknown tier or provider, or `max_rpm: 0` will now 422. For each, decide:
- if the test was relying on the old permissive behaviour by accident, give it a valid value;
- if it deliberately asserts that the value is stored, change it to assert the 422.

List every changed test in your report. Expected: exit 0.

- [ ] **Step 7: Commit** `feat(admin): refuse unroutable policy values; warn when an app or model price cannot serve agent traffic`

---

### Task 9: Remaining untested rejection points, at the HTTP level

These points exist in code, but no test sends them through `/v1/chat/completions`. The behaviour is expected to be correct already, so this task is tests only. If a test fails, report it: that's a real bug, don't paper over it.

**Files:**
- Test: `tests/onboarding/test_rejection_points.py` (new)

- [ ] **Step 1: Write the tests**

```python
from app.apps import store
from tests.onboarding.harness import conformance_client, onboard

_HI = {"messages": [{"role": "user", "content": "hi"}]}


def test_daily_budget_exhausted_is_429_and_named():
    with conformance_client() as c:
        app = onboard(c, "rp-daily", max_spend_usd_per_day=0.5)

        async def spend():
            async with c.app.state.db.session() as session:
                await store.record_usage(
                    session, application_id="rp-daily", request_id="seed", model="local-8b",
                    provider="onprem", tier="cheap", prompt_tokens=1, completion_tokens=1,
                    total_tokens=2, cost_usd=1.0, baseline_usd=1.0, latency_ms=1.0,
                    sensitivity="public", onprem_only=False, status="ok")
                await session.commit()

        c.portal.call(spend)
        r = app.chat(_HI)
    assert r.status_code == 429
    assert "daily budget exceeded" in r.json()["detail"]


def test_rate_limit_is_429_after_max_rpm():
    with conformance_client() as c:
        app = onboard(c, "rp-rpm", max_rpm=1)
        assert app.chat(_HI).status_code == 200
        r = app.chat(_HI)
    assert r.status_code == 429 and "rate limit" in r.json()["detail"]


def test_required_verification_failure_is_422_not_the_raw_answer(monkeypatch):
    from app.gateway.backends import litellm_backend
    from tests._litellm_fake import make_recording_acompletion, make_response

    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="UNVERIFIABLE")))
    with conformance_client() as c:
        app = onboard(c, "rp-verify")
        # A US SSN (regex detector) -> onprem_only + require_verification.
        r = app.chat({"messages": [{"role": "user", "content": "my ssn is 482-73-1946"}]})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("response failed verification")
    assert "UNVERIFIABLE" not in r.text.replace(r.json()["detail"], "")


def test_revoked_token_is_401():
    with conformance_client() as c:
        app = onboard(c, "rp-revoke")
        tokens = c.get("/admin/applications/rp-revoke/tokens").json()
        tok_id = (tokens if isinstance(tokens, list) else tokens["tokens"])[0]["id"]
        assert c.post(f"/admin/tokens/{tok_id}/revoke", json={}).status_code == 200
        r = app.chat(_HI)
    assert r.status_code == 401
```

Check two things before running:
- `grep -n "def record_usage" -A6 app/apps/store.py`: the keywords must match. Fix the test call if they differ.
- The SSN rule must set `require_verification: true`: run `grep -n -B2 -A6 "US_SSN" config/policy.yaml`. If it doesn't, replace that test's content with an entity whose rule does set it, and note that in your report.

- [ ] **Step 2: Run** `tests/onboarding/test_rejection_points.py`. Expected: pass. If a test fails on product behaviour rather than a test typo, **stop and report it**.

- [ ] **Step 3: Commit** `test(onboarding): HTTP-level coverage for daily budget, rpm, verification and revoked-token rejections`

---

### Task 10: Docs and backlog

**Files:**
- Modify: `docs/reference/api-reference.md` (error table around line 239)
- Modify: `docs/backlog/bugs.md` (new B34 entry at the top, above B33)

- [ ] **Step 1: API reference.** Find the error table with `grep -n "daily budget exceeded" docs/reference/api-reference.md`. Update or add these rows, keeping the table's existing column format:

```markdown
| `400` | `streaming is not supported for this application: set the application's stream_mode …` | The app has no `stream_mode`; set one, or send `stream: false`. |
| `422` | `no model is available for this request's policy: no candidates after residency filter: <counts and fixes>` | Nothing survived placement or app scope; the detail lists each reason and the field to change. |
| `422` | `no model is available for this request's policy: no candidates after budget filter: cheapest candidate … estimated at $X … over the per-request cap $Y …` | Raise the app's `max_cost_usd_per_request`, lower `max_tokens`, or lower the model's price. The router has already tried cheaper allowed tiers. |
| `422` | `model request rejected: requested model 'x' is not permitted or unavailable: <gate>` | The pinned `model` failed the named gate; `GET /v1/models` lists what this app can pin. |
| `502` | `no backend could serve this request: … (last error: <model>: <ErrorType>: <message>)` | Every candidate failed upstream; the last upstream error is included. |
```

Also document that `max_tokens`, `max_completion_tokens`, `temperature`, `top_p`, `stop`, `seed`, `presence_penalty`, `frequency_penalty`, `response_format` and `parallel_tool_calls` are forwarded to the model. Put this in the `POST /v1/chat/completions` request-body section.

- [ ] **Step 2: Backlog.** Add at the top of the entries (above `## B33`):

```markdown
## B34 — Every new app onboarding hit a different policy failure  ✅ RESOLVED (2026-09-15)

**Root cause.** Tests sent tiny synthetic requests, one policy field at a time; real clients (Qwen Code, openai SDK, LangChain) send content parts, `stream:true` on every turn, 20+ tool schemas, replayed tool turns, `content:null` and 25K-token prompts. The admin API also accepted configurations that could never route, and rejections did not name the field to change, so each incident took several rounds (16 incidents across millwise, demo, kit-gtm).

**Fixed.** `tests/onboarding/` conformance matrix (real client shape × policy recipe × model price, "served or told why"); B24; generation params forwarded; budget estimate prices output at `max_tokens` and counts tool schemas; tier fallback when over budget; per-app `max_cost_usd_per_request`; actionable 400/422/502 details; save-time refusal of impossible policies plus `warnings` for unroutable apps and agent-blocking model prices.

**Rule for new request-path work.** Add the client shape to `tests/onboarding/clients.py` and let the matrix run it under every recipe.
```

- [ ] **Step 3: Run** `tests/test_docs_links.py`, then the **full suite**. Expected: exit 0.

- [ ] **Step 4: Commit** `docs: onboarding conformance, forwarded params, actionable rejections (B34)`

---

## After all tasks (controller, not a subagent)

1. Run a final whole-branch review, then open one PR with one commit per task. Merge only after the full suite passes.
2. Before deploying to the DGX Spark, get the owner's OK. The deploy steps:
   - `git pull` on the Spark;
   - run `scripts/migrate_app_policies.py` against the Spark Postgres (adds `max_cost_usd_per_request`);
   - rebuild `api ui`;
   - verify `/health`;
   - re-run the Qwen-Code-sized e2e request.
