# NeMo Agent Toolkit (nvidia-nat 1.8.0) Integration Spike — Findings

Status: **DONE**. A `tool_calling_agent` NAT workflow ran end-to-end (LLM call -> tool
call -> tool result -> final LLM call) three separate ways -- `nat run`, `nat serve`,
and a plain Python script calling NAT's internal API -- all pointed at a local stub
OpenAI-compatible server standing in for our internal router. All artifacts live
under `scratch-nat-spike/` and are reproducible.

## VERDICT

**Practical -- with one packaging fix.** Real NAT, wired to an internal
OpenAI-compatible endpoint via the `openai` LLM provider's `base_url`, works exactly
as hoped: every LLM call the agent makes is a plain HTTP POST to our endpoint, using
a dummy API key NAT never validates. The only blocker was self-inflicted: the
`[agent]` extra as currently defined in `pyproject.toml`
(`nvidia-nat>=1.8,<2`, no sub-extra) **does not include NAT's built-in agents**
(`tool_calling_agent`, `react_agent`, etc.) -- those ship in the separate
`nvidia-nat-langchain` distribution, pulled in via `nvidia-nat[langchain]`. Fix:

```diff
 agent = [
-    "nvidia-nat>=1.8,<2",
+    "nvidia-nat[langchain]>=1.8,<2",
 ]
```

This is exactly what `nat workflow create` scaffolds by default in a fresh
workflow package's `pyproject.toml` (`nvidia-nat[langchain]~=1.8`), so it's the
toolkit's own expected shape, not a workaround.

Everything else -- deterministic mocking, step caps, programmatic invocation,
Python-function tools -- worked on the first architecturally-correct attempt once
that packaging gap was found.

## 1. Workflow YAML that actually validated and ran

`scratch-nat-spike/nat_spike_calc/src/nat_spike_calc/configs/config.yml`:

```yaml
functions:
  nat_spike_calc:
    _type: nat_spike_calc
    prefix: "Result:"

llms:
  stub_llm:
    _type: openai
    model_name: stub-model
    base_url: "http://127.0.0.1:9099/v1"
    api_key: "sk-dummy-not-checked"
    temperature: 0.0

workflow:
  _type: tool_calling_agent
  llm_name: stub_llm
  tool_names: [nat_spike_calc]
  max_iterations: 4
  verbose: true
```

Validated with:
```
uv run --extra agent --extra observability nat validate --config_file <path>
```
(after adding `nvidia-nat-langchain` to the environment -- see Blockers section).

### `openai` LLM provider -- exact fields (from `nat info components -t llm_provider -q openai`)

```
_type: openai
api_key: SecretStr | None          # OpenAI API key. NEVER format-validated or
                                    # called against api.openai.com -- a dummy
                                    # string works fine as long as base_url points
                                    # elsewhere.
base_url: str | None               # <-- point this at the internal router,
                                    # e.g. "http://router.internal:8080/v1"
model_name: str                    # required
temperature: float | None
top_p: float | None
seed: int | None
max_retries: int = 10
request_timeout: float | None
verify_ssl: bool = True
api_type: "chat_completion" (default) | other APITypeEnum values
do_auto_retry: bool = True
num_retries: int = 5
retry_on_status_codes / retry_on_errors: list
```
(`azure_openai` and `oci` providers exist too, with analogous `base_url`-style
overrides, in case Azure-shaped or OCI-shaped internal endpoints are ever needed.)

### Agent types available (all require the `langchain` extra -- see below)

Discovered via `nat info components -q tool_calling_agent|react_agent -f component_name`
once `nvidia-nat-langchain` was present:

- **`tool_calling_agent`** (`nat.plugins.langchain.agent.tool_calling_agent`) -- uses
  native OpenAI-format `tool_calls`. Key fields: `llm_name`, `tool_names`,
  `max_iterations` (default 15, **this is the step cap**), `max_history` (default
  15), `handle_tool_errors` (default True), `system_prompt`,
  `max_empty_response_retries`, `return_direct`. **Used for this spike.**
- **`react_agent`** (`nat.plugins.langchain.agent.react_agent`) -- classic
  Thought/Action/Action-Input text-parsing ReAct loop, with an opt-in
  `use_native_tool_calling: bool = False` flag that, if set `True`, switches it to
  native `tool_calls` too. Step cap: `max_tool_calls` (default 15).
  `retry_agent_response_parsing_errors` / `parse_agent_response_max_retries` control
  resilience to malformed text output.
- **`reasoning_agent`**, **`rewoo_agent`**, **`router_agent`**,
  **`responses_api_agent`**, **`per_user_react_agent`** also exist in
  `nvidia-nat-langchain` (plan/execute-style and multi-agent-routing variants) --
  not explored further, out of scope for the spike.

Both `tool_calling_agent` and `react_agent` validated (`nat validate`) with probe
configs.

### How tools are registered

`tool_names: [nat_spike_calc]` under `workflow:` -- a list of names that must match
keys under the top-level `functions:` block. Order given is not significant; the LLM
sees each tool as an OpenAI `tools[].function` schema (name, description, JSON-schema
parameters) auto-derived from the Python function's signature/docstring (see section 4).

### Step/iteration cap

`max_iterations` (tool_calling_agent) / `max_tool_calls` (react_agent) on the
`workflow:` block -- an integer, hard-capping the number of tool-call rounds before
the agent is forced to stop. Set to `4` in the spike; only 1 was actually used.

## 2. Exact LLM response format needed to trigger a tool call

**OpenAI native `tool_calls` JSON -- but streamed as SSE, not returned as one JSON
body.** This was the single sharp edge in the whole spike (see Blockers). NAT's
`openai` LLM provider is LangChain's `ChatOpenAI` under the hood, which sends
`"stream": true` by default. A stub that answers with a normal
`chat.completion` JSON body gets HTTP 200'd but LangChain throws
`"No generation chunks were returned"` and the agent dies -- the HTTP call
*looks* successful, so this failure mode is non-obvious.

Concrete request -> response pair captured from the stub's own log
(`scratch-nat-spike/stub_requests.log`), hop 1 of a real run:

**Request** (`POST http://127.0.0.1:9099/v1/chat/completions`):
```json
{
  "messages": [{"content": "What is 12 times 7?", "role": "user"}],
  "model": "stub-model",
  "stream": true,
  "stream_options": {"include_usage": true},
  "temperature": 0.0,
  "tools": [{
    "type": "function",
    "function": {
      "name": "nat_spike_calc",
      "description": "Evaluates a simple arithmetic expression of the form \"<number> <op> <number>\" ...",
      "parameters": {"properties": {"expression": {"type": "string"}},
                     "required": ["expression"], "type": "object"}
    }
  }]
}
```

**Response** -- SSE stream, `Content-Type: text/event-stream`:
```
data: {"id":"chatcmpl-stub-...","object":"chat.completion.chunk","created":...,
       "model":"stub-model","choices":[{"index":0,"delta":{"role":"assistant",
       "tool_calls":[{"index":0,"id":"call_0c30b55f2231","type":"function",
       "function":{"name":"nat_spike_calc","arguments":"{\"expression\": \"12 * 7\"}"}}]},
       "finish_reason":null}]}

data: {"id":"chatcmpl-stub-...","object":"chat.completion.chunk", ...,
       "choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}

data: [DONE]
```

Hop 2 request then includes the assistant tool-call message plus a
`{"role": "tool", "content": "Result: 84.0", "tool_call_id": "call_0c30b55f2231"}`
message; the stub detects the `role == "tool"` message and streams a final chunk
with `delta.content` set and `finish_reason: "stop"` instead of `tool_calls`.

**Implication for the real mock backend**: it must implement true OpenAI-style SSE
chunk streaming (`text/event-stream`, incremental `delta`, terminal `data: [DONE]`),
not just return single JSON bodies -- even in "deterministic canned response" mode.
The full working stub is `scratch-nat-spike/stub_server.py`.

## 3. Programmatic invocation API

Traced from `nat/front_ends/simple_base/simple_front_end_plugin_base.py` (what
`nat run`/`nat serve` call internally) and `nat/runtime/loader.py`
(`load_workflow`, whose docstring literally says "This is the primary entry point
for running NAT workflows"). Working script:
`scratch-nat-spike/programmatic_run.py`, run and verified end to end:

```python
import asyncio
from nat.runtime.loader import load_workflow

async def main(question: str) -> str:
    config_file = "nat_spike_calc/src/nat_spike_calc/configs/config.yml"
    async with load_workflow(config_file) as session_manager:
        async with session_manager.session() as session:
            async with session.run(question) as runner:
                return await runner.result(to_type=str)

asyncio.run(main("What is 12 times 7?"))
# -> "The result of 12 * 7 is 84 (tool reported: Result: 84.0)."
```

Lower-level equivalent (what `load_workflow` composes, useful if finer control over
the `Config` object or builder lifetime is needed):

```python
from nat.runtime.loader import load_config          # yaml -> validated Config
from nat.builder.workflow_builder import WorkflowBuilder
from nat.runtime.session import SessionManager

config = load_config(config_file)
async with WorkflowBuilder.from_config(config=config) as builder:
    session_manager = await SessionManager.create(config=config, shared_builder=builder)
    try:
        async with session_manager.session() as session:
            async with session.run(question) as runner:
                result = await runner.result(to_type=str)
    finally:
        await session_manager.shutdown()
```

This maps directly onto `app/agent/runtime.py`'s job: call `load_workflow(...)`
once at startup (or per-request if config varies), then `session_manager.session()`
+ `.run(input)` + `.result(to_type=str)` per request. `SessionManager.create` also
accepts `max_concurrency` for bounding parallel in-flight workflow runs.

One caveat: this bypasses NAT's CLI-level plugin discovery timing. In our script we
had to explicitly `import nat_spike_calc.nat_spike_calc` before calling
`load_workflow` to force the `@register_function` decorator to run and register our
tool's type into the `GlobalTypeRegistry`, because `load_config`'s built-in
`discover_and_register_plugins()` call only walks **installed packages' entry
points** (`nat.components` group) -- it doesn't independently import arbitrary local
modules. In production this isn't an issue: our tool packages will be properly
`pip install`-ed (declaring their `nat.components` entry point, see section 4), so
`discover_and_register_plugins()` finds them automatically without any manual import.

## 4. Defining a Python-function tool

Official mechanism: `nat workflow create <name>` scaffolds an installable package;
we customized its generated tool. Pattern (from
`scratch-nat-spike/nat_spike_calc/src/nat_spike_calc/nat_spike_calc.py`):

```python
from pydantic import Field
from nat.plugin_api import Builder, FunctionBaseConfig, FunctionInfo, LLMFrameworkEnum, register_function

class NatSpikeCalcFunctionConfig(FunctionBaseConfig, name="nat_spike_calc"):
    """Docstring becomes the tool's config description."""
    prefix: str = Field(default="Result:", description="...")

@register_function(config_type=NatSpikeCalcFunctionConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def nat_spike_calc_function(config: NatSpikeCalcFunctionConfig, builder: Builder):
    async def _calculator(expression: str) -> str:
        """This docstring becomes the OpenAI tool-schema `description` the LLM sees."""
        ...
        return f"{config.prefix} {result}"
    yield FunctionInfo.from_fn(_calculator, description=_calculator.__doc__)
```

- `FunctionBaseConfig` subclass with `name="..."` fixes the YAML `_type:` string;
  Pydantic fields become the tool's YAML-configurable parameters.
  `@register_function(config_type=..., framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])`
  registers it; the function is an async **generator** that `yield`s a
  `FunctionInfo` wrapping the actual callable. The callable's parameter type
  annotations and docstring drive the auto-generated OpenAI `tools[].function`
  JSON schema -- no manual schema authoring needed for simple string-in/string-out
  tools.
- Discovery requires the tool live in a package that declares the NAT entry point,
  e.g. `scratch-nat-spike/nat_spike_calc/pyproject.toml`:
  ```toml
  [project.entry-points.'nat.components']
  nat_spike_calc = "nat_spike_calc.register"
  ```
  where `register.py` just imports the module containing `@register_function` to
  trigger it. This is the same shape our real calculator/doc-search/CRM tools
  should use: one small installable package (or a `nat_tools` package with several
  such modules under one entry point) added as a workspace/editable dependency.

## 5. Endpoint hit count for one multi-step run

**Exactly 2 hits** per single-question run with `tool_calling_agent` +
`max_iterations: 4`: hop 1 = tool-call decision, hop 2 = final answer after the
tool result is appended to history. Verified three independent ways, all
landing on the stub's `/hits` counter incrementing by exactly 2 each time:
- `nat run --config_file ... --input "What is 12 times 7?"`
- `nat serve` + `POST /generate {"input_message": "..."}`
- the programmatic `load_workflow(...)` script

This directly demonstrates per-hop routing is capturable: every hop is an
independent HTTP POST to our endpoint, so the real router's step 5/6/7 pipeline
re-enters exactly once per hop, matching CLAUDE.md's requirement that "every LLM
call the agent makes re-enters compression (5) and the router (6) -- both are
per-call, not per-request."

**No built-in per-call correlation header.** Inspecting the actual headers NAT's
LangChain-based `AsyncOpenAI` client sends per hop:
```json
{
  "host": "127.0.0.1:9099",
  "authorization": "Bearer sk-dummy-not-checked",
  "content-type": "application/json",
  "user-agent": "AsyncOpenAI/Python 2.46.0",
  "x-stainless-lang": "python", "x-stainless-package-version": "2.46.0",
  "x-stainless-os": "Linux", "x-stainless-arch": "x64",
  "x-stainless-runtime": "CPython", "x-stainless-runtime-version": "3.12.3",
  "x-stainless-async": "async:asyncio", "x-stainless-retry-count": "0"
}
```
Only standard OpenAI-Python-SDK (Stainless-generated client) headers -- no
`traceparent`, no NAT request/session ID, nothing to correlate hops within a single
logical agent run at the HTTP layer. **Our router/gateway must generate its own
`request_id`/hop-index at the HTTP boundary** (e.g. derive it from the persistent
`Authorization`/session context our own middleware sets, or just treat "N
sequential calls sharing a connection/short time window" as one logical run) --
don't expect NAT to hand us one for free. NAT's own internal correlation (for
Langfuse tracing) happens via its OpenTelemetry span tree, not via outbound HTTP
headers to the LLM endpoint, so that story is separate from ours and doesn't
interfere.

## 6. Blockers and sharp edges

1. **`nvidia-nat` base install has no built-in agents.** `nvidia-nat-core`
   (pulled in by plain `nvidia-nat`) registers zero `llm_provider`-consuming agent
   function types -- `nat info components` shows nothing named `*_agent`, and
   `nat validate` on a `_type: react_agent` config fails with *"Requested workflow
   type `react_agent` not found... Have you ensured the necessary package has been
   installed?"* `tool_calling_agent`/`react_agent`/`reasoning_agent`/etc. all live
   in the **`nvidia-nat-langchain`** distribution (pulled in via
   `nvidia-nat[langchain]`). Easy to miss because `nat workflow create`'s default
   scaffold template (`config.yml.j2`) hard-codes `_type: react_agent` as if it's
   always available -- it silently assumes you installed with the right extra. Fix
   in `pyproject.toml`'s `agent` extra (see VERDICT).
2. **`uv run` re-syncs the venv from the lockfile on every invocation**, silently
   reverting any ad-hoc `pip install -e` (e.g. from `nat workflow create --install`)
   made in a prior `uv run` call. Use `uv run --with-editable <path>` (and
   `--with <package>` for extra ephemeral deps like `nvidia-nat-langchain`) on every
   invocation instead of relying on a one-time install persisting.
3. **Streaming is the default and is load-bearing** (see section 2) -- a mock
   backend that only speaks plain-JSON `chat.completion` responses will pass a
   manual `curl` smoke test but fail inside the agent with a misleading
   `"No generation chunks were returned"` error that gives no hint the issue is
   `stream: true` vs `false`.
4. **API key is never validated.** `api_key: "sk-dummy-not-checked"` worked with
   zero complaints -- no format check, no call to any OpenAI-owned endpoint,
   confirming the `openai` provider is a pure `base_url` + `Authorization: Bearer`
   passthrough. Good news for pointing it at our internal router.
5. **No GPU, no extra env vars required.** Nothing beyond `base_url`/`api_key` in
   the YAML was needed to get the `openai` provider working against a local stub --
   no `OPENAI_API_KEY` env var, no `.env` file.
6. **Everything is async.** `load_workflow`/`session.run`/`runner.result` are all
   `async def`/async context managers; `app/agent/runtime.py` will need an event
   loop (which a FastAPI app already has) -- no sync wrapper was needed or found
   necessary for our use case, though `nat.builder.sync_builder.SyncBuilder` exists
   if a sync call site is ever unavoidable (not exercised in this spike).
7. **Pydantic v2 (2.13.4), Python 3.12.3** -- no version friction encountered;
   NAT's own config models, our tool's `FunctionBaseConfig` subclass, and FastAPI
   in the same venv coexisted without conflict.
8. A background dependency-resolution quirk: `nat info components` at startup logs
   a `WARNING - Failed to import plugin 'nat_tools'` /
   `ModuleNotFoundError: No module named 'langchain_core'` when `nvidia-nat-langchain`
   isn't installed (because `nat.tool.register` unconditionally imports
   `nvidia_rag`, which needs `langchain_core`). Harmless once `langchain` extra is
   present (which we need anyway for the agents), but shows up as scary red text on
   every `nat` invocation until then.

## Reproduction

```bash
cd /home/mani/projects/routeForge

# 1. Start the deterministic stub (background)
uv run --extra agent --extra observability python scratch-nat-spike/stub_server.py &

# 2a. Run via CLI
uv run --extra agent --extra observability \
  --with-editable scratch-nat-spike/nat_spike_calc \
  nat run --config_file scratch-nat-spike/nat_spike_calc/src/nat_spike_calc/configs/config.yml \
  --input "What is 12 times 7?"

# 2b. Run via FastAPI front end
uv run --extra agent --extra observability \
  --with-editable scratch-nat-spike/nat_spike_calc \
  nat serve --config_file scratch-nat-spike/nat_spike_calc/src/nat_spike_calc/configs/config.yml --port 8321 &
curl -X POST http://127.0.0.1:8321/generate -H "Content-Type: application/json" \
  -d '{"input_message": "What is 12 times 7?"}'

# 2c. Run programmatically
cd scratch-nat-spike
uv run --project /home/mani/projects/routeForge --extra agent --extra observability \
  --with nvidia-nat-langchain --with-editable nat_spike_calc \
  python programmatic_run.py "What is 12 times 7?"

# Inspect proof of multi-hop routing
curl http://127.0.0.1:9099/hits
cat scratch-nat-spike/stub_requests.log
```

## Spike artifacts

- `scratch-nat-spike/stub_server.py` -- deterministic OpenAI-compatible SSE stub.
- `scratch-nat-spike/stub_requests.log` -- captured request/response pairs proving
  multi-hop routing (JSONL, `direction: request|response` per line).
- `scratch-nat-spike/nat_spike_calc/` -- installable NAT workflow package
  (`nat workflow create` scaffold, customized): calculator tool + workflow config.
- `scratch-nat-spike/programmatic_run.py` -- working programmatic invocation script.
- `scratch-nat-spike/FINDINGS.md` -- this file.
