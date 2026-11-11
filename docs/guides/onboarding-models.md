# Onboarding an on-prem model

routeForge is a pure router. It points at externally-deployed
OpenAI-compatible endpoints and deploys, scales, or load-balances nothing.
There is no bundled demo model and no `inference_backend` config axis — the <!-- purged-ok -->
on-prem model pool boots **empty**, and every on-prem candidate the router
can pick is one an operator has explicitly onboarded through the admin API
or UI.

## (a) Deploy the model — outside routeForge

Stand up your own OpenAI-compatible inference server. How you serve it is
entirely out of scope for routeForge — any of these work:

- vLLM (`vllm serve <model> --served-model-name <slug> --enable-prompt-tokens-details`).
  Add `--enable-prompt-tokens-details`: without it vLLM leaves
  `usage.prompt_tokens_details` out, so routeForge cannot see prefix-cache hits
  and cached-token pricing never applies (the cost record shows "not reported"). The Infrastructure page flags an
  endpoint that never reports it with an amber "No cache data" badge, once it
  has served 20 requests.
- NVIDIA Dynamo (fronting TensorRT-LLM or vLLM workers)
- NVIDIA NIM
- Any other server that speaks the OpenAI `/v1/chat/completions` and
  `/v1/models` API shape

The one thing that matters to routeForge: whatever `<slug>` you pass to
`--served-model-name` (or the equivalent flag for your server) is the id
that must appear in that server's `GET /models` response — routeForge uses
it as the onboarding `slug` and probes for it. See the `inference-backends`
skill for what the operator runs behind the endpoint; none of it is
routeForge's responsibility.

## (b) Onboard it

Two equivalent paths, both admin-authed:

- **UI**: Infrastructure → Onboard a model
- **API**: `POST /admin/infra/models`

Field reference (`OnboardIn`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `slug` | str | yes | Stable id the router/pricing key on. Must match the id your server reports for `served-model-name`. |
| `display_name` | str | yes | What the UI shows customers. |
| `tier` | str | yes | One of `cheap \| mid \| frontier`. There is no `strong` tier. |
| `endpoint` | str (URL) | yes | Base URL of your OpenAI-compatible server. |
| `api_key_env` | str \| null | no | Name of the env var holding the bearer token for this endpoint, if any — never the key value itself. See (b3). |
| `api_key` | str \| null | no | The endpoint's bearer token itself, stored **encrypted** and never returned — see (b3). Never together with `api_key_env`. On `PUT`, omitted = keep the stored key, `null` = remove it. Needs `ENCRYPTION_KEY`. |
| `api_key_source`, `api_key_hint` | str \| null | **response only — never send them** | Where the key comes from (`"stored"` \| `"env"` \| `null`) and, for a stored key of 12+ characters, its last 4 characters. See (b3). |
| `price_input` | float | yes | USD per 1M input tokens. |
| `price_output` | float | yes | USD per 1M output tokens. |
| `price_cached_input` | float \| null | no | USD per 1M prompt tokens this endpoint served from its **own** prompt cache — see (b2). `null`/omitted = undeclared, which bills cached tokens at the full `price_input` rate. |
| `quality` | float \| null | no | Optional published quality score; falls back to the tier-default quality ladder if omitted. |
| `context_window` | int \| null | no | See [`context_window` and `context_window_source`](#b1-context_window-and-context_window_source) below. |
| `context_window_source` | str \| null | **response only — never send it** | Not part of `OnboardIn`; returned by `GET`/`POST`/`PUT /admin/infra/models` as `"operator"` \| `"probed"` \| `null`. See (b1). |
| `allowed_app_ids` | list[str] \| null | no | App-scoping — see (d). |
| `notes` | str \| null | no | Free text. |
| `enabled` | bool | no (default `true`) | Admin intent — see (c) for how this combines with the probe. |
| `region` | str \| null | no | Data-handling declaration — see (e2). Free customer vocabulary, stored lowercased. |
| `prompt_retention` | str \| null | no | Data-handling declaration — `none \| retained \| unknown`. |
| `accepts_raw_sensitive` | bool \| null | no | Data-handling declaration — JSON boolean here. |

Edit an onboarded model with `PUT /admin/infra/models/{slug}` (same body,
full replace — it re-probes on every edit). Remove one with
`DELETE /admin/infra/models/{slug}`. List all onboarded rows with
`GET /admin/infra/models`. All four endpoints require admin auth.

## (b1) `context_window` and `context_window_source`

`context_window` is the model's context length in tokens. It is captured
automatically, published on `GET /v1/models` as `max_model_len` (see
[`api-reference.md`](../reference/api-reference.md#get-v1models)), and is
**never enforced** by routeForge — an oversized request still fails upstream
(B36).

- **Captured from the endpoint itself, on onboard and on edit.** The same
  probe that checks your `slug` is served also reads that entry's
  `max_model_len` from the upstream `GET {endpoint}/models` response. This
  runs on **both** `POST /admin/infra/models` and `PUT
  /admin/infra/models/{slug}` — every edit re-probes and can pick up a
  changed value.
- **An operator-typed value always wins over the probe.** Send an explicit
  `context_window` in the body and it is stored as given; the probed number
  is discarded for that write.
- **Never guessed.** An endpoint that doesn't publish `max_model_len` (or
  publishes something unusable — missing, `null`, a non-numeric value, zero
  or negative) leaves `context_window` unknown (`null`). routeForge never
  invents a default, and an unusable `max_model_len` does not fail the probe
  itself — reachability and context capture are independent checks.
- **Zero or negative is rejected, not stored.** An explicit `context_window`
  of `0` or less in the request body is a `422` (`context_window must be >=
  1, or omit/null for unknown`) — an operator typo shouldn't silently persist
  as a nonsense row.
- **`context_window_source` records where the stored number came from** —
  `"operator"` (a human typed it), `"probed"` (routeForge's own probe
  supplied it), or `null` when `context_window` itself is `null`. It is
  **response-only**: `GET`/`POST`/`PUT /admin/infra/models` return it, but it
  is never part of the `OnboardIn` request body and sending it has no effect.
  Provenance is decided by the **value that ends up stored**, not by whether
  the field was present on the request — the console's edit form re-submits
  the whole row on every save, so a value that matches what the probe would
  have produced (this probe's result, or the existing row's already-`"probed"`
  value when this probe learned nothing new) is still recorded `"probed"`,
  even though the field was technically present in the PUT body. Only a
  value that differs from both is recorded `"operator"`. Typing the exact
  number the endpoint reports is honestly `"probed"`, not `"operator"` — the
  column says where the number came from, not who pressed save.
- **There is no re-probe loop.** The probe runs only on onboard and on edit —
  stated plainly: **the value is only as fresh as the last onboard or edit.**
  If the operator reconfigures the upstream server with a different
  `--max-model-len` (or equivalent), routeForge keeps the old number until
  someone re-saves this model through the admin API or console. There is no
  background job that notices the drift.

## (b2) `price_cached_input` — the cached-prompt-token rate

Providers that serve part of a prompt from a cache often bill those tokens at a
reduced rate. RouteForge captures the count the provider reports
(`usage.prompt_tokens_details.cached_tokens`), persists it on the usage row, and
prices it — but **only at a rate the operator declared**.

- **Undeclared (`null`, or the field omitted) ⇒ cached tokens bill at the full
  `price_input` rate.** RouteForge never invents a discount, so onboarding
  nothing here leaves every cost figure exactly as it was.
- **Declared ⇒** the cached portion of the prompt is billed at that rate and the
  rest at `price_input`. The count is clamped into `[0, prompt_tokens]`, because
  providers occasionally over-report it and an unclamped value would drive the
  cost below zero.
- The estimate the router budgets against **always assumes zero cached tokens.**
  The hit rate is unknowable before the call, and guessing one would bias
  routing toward whichever model the guess was generous about. Estimate high,
  record true: budget enforcement stays at least as strict as before.

**On-prem guidance: set `price_cached_input` equal to `price_input`.** A vLLM (or
SGLang, or TensorRT-LLM) prefix-cache hit saves *latency*, not GPU
amortization — the GPU-hours you are amortizing over your token price were spent
either way. Declaring a discount you did not receive understates your own cost
and makes the cost dashboard lie to you. The prefix cache does pay off, and
RouteForge does see it: as a lower measured latency for that endpoint, which
feeds the routing signal (`docs/reference/routing-observability.md` §3). That is
where the on-prem win shows up.

Two behaviours worth knowing:

- **A provider that does not report cached tokens yields `0`**, which is
  indistinguishable from a genuine cache miss. RouteForge does not guess.
- **On `PUT` (edit), omitting the field preserves the stored value**; only an
  explicit `null` clears it. This differs from `region` / `prompt_retention`,
  which full-replace. A price quietly reverting to "no discount" because a
  scripted display-name update was written against an older body shape would
  overstate spend with no operator action.

## (b3) Endpoint API keys — two ways, exactly one per model

An endpoint started with a key (vLLM `--api-key`, NIM, a gateway in front of
your servers) needs RouteForge to send `Authorization: Bearer <key>`. There are
two ways to give it, and a model uses **exactly one** (sending both is `422`):

| | `api_key` (stored) | `api_key_env` |
|---|---|---|
| What you send | the key itself | the **name** of an env var in the API container |
| Where the key lives | `onboarded_models.api_key_enc`, Fernet-encrypted with `ENCRYPTION_KEY` | your secret manager → the pod's environment |
| Needs a redeploy to add/rotate | **no** — usable as soon as the write returns | yes (new env entry, pod roll) |
| Use it when | you want onboarding to be one step in the console | secrets are managed outside RouteForge (Vault, sealed secrets) |

With neither, the provider-level `providers.onprem.api_key_env` in
`config/serving.yaml` is the fallback, exactly as before.

**`ENCRYPTION_KEY` is required for a stored key**, and it must decode to at
least 32 bytes (64 hex characters). Without one the write is refused — `422 set
ENCRYPTION_KEY to store endpoint keys, or use api_key_env` — and a shorter one
(e.g. `changeme`) is refused with a `422` naming the 32-byte requirement; nothing
is stored and nothing is probed. A stored key is never kept in clear (this is
stricter than the per-app Langfuse secret, which falls back to plaintext in dev
and accepts a short key). Surrounding whitespace is stripped; what remains must
be printable ASCII with no spaces or control characters (`422` otherwise —
including an empty key).

**Write-only.** No admin read returns the key or its ciphertext. `GET
/admin/infra/models` (and the `POST`/`PUT` acks) say only `api_key_source`:
`"stored"`, `"env"` or `null`; a stored key of at least 12 characters also gets
`api_key_hint`, its last 4 characters. The key is not in the config snapshot
(`GET /admin/config-snapshot`), traces, outcome rows, decision records or logs.
Upstream error text (a probe failure, a backend error that echoes the request,
a stream that fails before or after its first byte) has any live endpoint key —
stored, from a model's `api_key_env`, or the provider-level key — replaced with
`<redacted-key>`, including its repr/JSON-escaped forms, before it is logged,
stored or returned. A `422` for a malformed onboarding body does not echo the body.

**Switching and clearing.** One `PUT` switches modes: sending `api_key` clears
`api_key_env`, and sending `api_key_env` removes the stored key. `api_key: null`
(with no `api_key_env`) removes the stored key. **Omitting `api_key` on a `PUT`
keeps the stored key** — the console cannot resubmit a write-only value, so an
unrelated edit (a price, a display name) must not wipe it. (`api_key_env` itself
still full-replaces, as before.)

**A stored key never follows the endpoint to a new host.** A `PUT` that changes
the endpoint's scheme, host or port with `api_key` omitted is `422 changing the
endpoint requires re-entering api_key, or sending api_key: null to remove it` —
nothing is probed or saved. (A path-only change keeps the key.) Otherwise an
admin session could repoint a model at a host it controls and read the key off
the probe, and a model moved to another vendor would ship the old vendor's key.
A `POST` never inherits a stored key either: re-onboarding over a disabled row
(B19) starts with no stored key unless the body gives one.

**Rotation.** `PUT` the new `api_key`. The endpoint is re-probed with the new key
(`401`/`403` → `probe_status: "unauthorized"`, saved disabled — B61). The
replica that served the `PUT` uses the new key at once. **Every other replica
picks it up on its next registry reload, at most `routing.quality_refresh_s`
later (60 s by default)** — the same background loop that reloads quality
overrides also reloads onboarded models, so no restart is needed. Until then a
replica may still send the old key; rotate the key at the endpoint after that
window if the old one must keep working in between.

**If `ENCRYPTION_KEY` changes.** Nothing probes periodically, so the stored
`probe_status` and `enabled` do not change. Instead, each replica, on its next
registry reload (≤ `routing.quality_refresh_s`), finds the key no longer
decrypts, logs one WARNING naming the model's slug (never the value) and **takes
the model out of routing on that replica**; `/health` lists it as
`unauthorized`. This only narrows the pool — an `onprem_only` request with no
other healthy on-prem model is rejected, never sent to cloud. Until that reload,
a replica still routing to it sends a placeholder key; the endpoint answers
`401`, which does not trip the circuit breaker (a 4xx is the request's fault,
B36), so the request fails over to a peer or ends in `502`. `GET
/admin/infra/models` still shows the DB row (`enabled`, `api_key_source:
"stored"`, no hint). Re-enter the key with a `PUT`: it re-probes and the model
returns to routing.

Existing deployments need the column first:
`ALTER TABLE onboarded_models ADD COLUMN IF NOT EXISTS api_key_enc TEXT;`
(or run `scripts/migrate_app_policies.py`) — see
[`admin-setup.md`](admin-setup.md#upgrading-onboarded_models-for-stored-endpoint-keys-b68).

## (c) What the probe checks

On both `POST` (onboard) and `PUT` (edit), routeForge does
`GET {endpoint}/models` and asserts your `slug` appears among the returned
model ids. The persisted `enabled` is the requested `enabled`
**AND** the probe result:

- Reachable + `enabled: true` → saved enabled, `probe_status: "healthy"`.
- Reachable + `enabled: false` → saved disabled — an admin can explicitly
  take a healthy model out of rotation.
- Reachable but the credentials are rejected (`401`/`403`) → saved disabled,
  `probe_status: "unauthorized"`. The probe sends the model's stored key, else
  the value of its `api_key_env` — see (b3).
- Unreachable, or reachable but the slug isn't in `/models` → **always**
  saved disabled, `probe_status: "unreachable"`, `probe_error` set — an
  unreachable model can never end up enabled, regardless of what `enabled`
  was requested as.

## (d) App scoping

`allowed_app_ids` restricts which applications may route to this model.
Empty or `null` means every application can. This is enforced at request
time by the `ModelRegistry` (`is_allowed_for_app`), not just in the admin UI.

## (e) Privacy behavior

Every onboarded model is `provider: onprem` (forced server-side — never
taken from the request body). The effective on-prem pool is whatever
onboarded rows are currently `enabled`.

If no onboarded model is currently enabled, `ModelRegistry.has_healthy_onprem()`
is `false`. The policy context drops `onprem` from `allowed_providers` in
that case, so an `onprem_only` request has no eligible candidate and is
**rejected** — never silently routed to cloud. This is the same privacy
guarantee the old `inference_backend: none` reject gave; it's now derived <!-- purged-ok -->
from onboarding state instead of a static config value, and it holds even
if a previously-healthy model goes unreachable and is later disabled by an
edit or a failed re-probe.

## (e2) Data-handling declarations

An operator declares three data-handling attributes about an endpoint at onboarding.
**RouteForge records what an operator declared about an endpoint and enforces policy
against that declaration — it cannot verify a provider's contractual claim.** These are
statements about a contract the operator signed, stored so that an application's policy
can be enforced against them and so that an auditor can see what a decision rested on.
They are never a proof, and the console says so on the form.

| Attribute | Values | Meaning |
|---|---|---|
| `region` | any token, lowercased (`us`, `eu`, `de`, `onprem`, …) | Where the compute runs. Customer vocabulary — RouteForge ships no geography list and validates nothing here. |
| `prompt_retention` | `none` \| `retained` \| `unknown` | Whether the provider keeps prompts. |
| `accepts_raw_sensitive` | `true` \| `false` \| `unknown` | Whether this endpoint may receive **unredacted** sensitive content. |

A declared `false` excludes this endpoint for any request that would send it raw
sensitive content — which is an app running `model_input: raw` over content the gate
classified above `public`, **and also** an app running `model_input: raw` with
`pii_detection: off` (the `policy_mode: passthrough` shape). An app that classifies
nothing cannot show its content is public, so an endpoint that declared it will not take
unredacted sensitive content is not handed content nobody has classified. Full statement
of all three rules: `docs/reference/per-app-config.md`.

**Two shapes for the same attribute, and they differ on purpose.**

- **Onboarding API / DB / console** (`POST`/`PUT /admin/infra/models`):
  `accepts_raw_sensitive` is a **JSON boolean** (`true` / `false`), and *omitting* it —
  or sending `null` — means "undeclared". `region` and `prompt_retention` are strings, or
  `null` for undeclared.

  ```json
  {"slug": "qwen3-8b", "display_name": "Qwen3 8B", "tier": "cheap",
   "endpoint": "http://vllm.my-ns:8000/v1", "price_input": 0, "price_output": 0,
   "region": "us", "prompt_retention": "none", "accepts_raw_sensitive": true}
  ```

- **YAML** (`config/routing.yaml`'s `model_pool` entries, and provider defaults under
  `config/serving.yaml`'s `providers.<name>`): all three are **string tokens**, so
  `accepts_raw_sensitive` is `"true"` / `"false"` / `"unknown"`, not a YAML boolean.

  ```yaml
  # serving.yaml — the provider-wide default
  providers:
    azure: {enabled: true, region: us, prompt_retention: none, accepts_raw_sensitive: "false"}

  # routing.yaml — a per-model declaration, which wins over the provider default
  model_pool:
    - {name: gpt-4o-mini, provider: azure, tier: mid, region: us, prompt_retention: none}
  ```

**Resolution order**, per attribute: the per-model value, else the provider default from
`serving.yaml`, else `unknown`. `ModelRegistry.effective_pool()` resolves it once and
records the provenance (`model` / `provider_default` / `unknown`) on the decision
certificate.

**One asymmetry worth knowing before you edit an endpoint.** `PUT /admin/infra/models/{slug}`
is a full replace, so omitting `region` or `prompt_retention` clears them back to
undeclared — that direction is *stricter*, since an app requiring a region excludes an
undeclared endpoint. `accepts_raw_sensitive` is the exception: omitting it keeps the
stored value, because clearing it would *loosen* the endpoint (the raw-sensitive rule
fires only on a declared `false`). Send `"accepts_raw_sensitive": null` explicitly to
clear it.

**A malformed `prompt_retention` or `accepts_raw_sensitive` in YAML fails the boot**
(`POOL_*_UNKNOWN` / `PROVIDER_*_UNKNOWN`). An unset value never does. `region` is never
linted — it has no enum.

What an application does with these — `allowed_regions`, `require_no_retention`, the
three exclusion rules and their reason codes — is in
`docs/reference/per-app-config.md`.

## (f) Migration note (existing deployments)

`onboarded_models` is a new table, created via
`CREATE TABLE IF NOT EXISTS` as part of normal `create_all` startup — no
migration tool needed for a fresh or already-current database.

On a database from **before** this pivot: `create_all` will add
`onboarded_models` but will not touch or drop the old `infra_models` /
`infra_settings` tables it replaces. The operator must drop those two
orphaned tables manually — routeForge no longer reads or writes them.

## (g) The pool boots empty

There is no bundled demo model and no YAML on-prem entry any more. A fresh
deployment starts with an empty on-prem tier; `cheap`/`mid`/`frontier`
candidates from the cloud pool (`config/routing.yaml`'s `model_pool`) are
still available if `providers.azure`/`providers.bedrock` are enabled, but
on-prem candidates only exist once an operator onboards one. Onboarding
takes effect immediately — the `ModelRegistry` refreshes on every admin
write, no restart required. Other replicas reload it in the background every
`routing.quality_refresh_s` (60 s by default).

## Cloud providers are config, not onboarding

Everything above is about **on-prem** endpoints. A cloud path is declared in config, and
is off until an operator turns it on:

- `serving.yaml` `providers.<name>` — transport, the key's env-var name, and the
  provider's data-handling declarations;
- `routing.yaml` `model_pool` — which models, in which tier;
- `pricing.yaml` — a price for each pooled model (the config lint requires one).

`azure` and `bedrock` are shipped, both off. Two switches let a deployment change this
without carrying its own copy of the shipped files:

- **`SP_PROVIDER_<NAME>_ENABLED=true|false`** turns one provider on or off.
- **`SP_CONFIG_OVERLAY_DIR=<dir>`** names a directory whose `serving.yaml`, `routing.yaml`,
  `policy.yaml` or `pricing.yaml` (any subset) is merged over the shipped file: mappings
  merge key by key, `model_pool` merges by `name`, anything else is replaced. The
  cross-file lint runs on the merged result and the routing policy version is hashed from
  it. `deploy/dgx_lab/config-overlay/` is a worked example that adds an `openrouter`
  provider with two pool entries and their prices.

Turning a cloud provider on changes placement for **every application that is not
`onprem_only`**: those applications may now be routed off the customer's infrastructure.
Before enabling one, set `allowed_providers: [onprem]` on each application that must stay
where it is. `onprem_only` applications are unaffected by construction.

Declare a third-party provider's data handling cautiously. RouteForge enforces policy
against the declaration and cannot verify what the provider keeps; the lab's `openrouter`
entry declares `prompt_retention: retained` and `accepts_raw_sensitive: "false"`.
