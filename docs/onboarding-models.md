# Onboarding an on-prem model

routeForge is a pure router. It points at externally-deployed
OpenAI-compatible endpoints and deploys, scales, or load-balances nothing.
There is no bundled demo model and no `inference_backend` config axis — the
on-prem model pool boots **empty**, and every on-prem candidate the router
can pick is one an operator has explicitly onboarded through the admin API
or UI.

## (a) Deploy the model — outside routeForge

Stand up your own OpenAI-compatible inference server. How you serve it is
entirely out of scope for routeForge — any of these work:

- vLLM (`vllm serve <model> --served-model-name <slug>`)
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
| `api_key_env` | str \| null | no | Name of the env var holding the bearer token for this endpoint, if any — never the key value itself. |
| `price_input` | float | yes | USD per 1M input tokens. |
| `price_output` | float | yes | USD per 1M output tokens. |
| `quality` | float \| null | no | Optional published quality score; falls back to the tier-default quality ladder if omitted. |
| `context_window` | int \| null | no | |
| `allowed_app_ids` | list[str] \| null | no | App-scoping — see (d). |
| `notes` | str \| null | no | Free text. |
| `enabled` | bool | no (default `true`) | Admin intent — see (c) for how this combines with the probe. |

Edit an onboarded model with `PUT /admin/infra/models/{slug}` (same body,
full replace — it re-probes on every edit). Remove one with
`DELETE /admin/infra/models/{slug}`. List all onboarded rows with
`GET /admin/infra/models`. All four endpoints require admin auth.

## (c) What the probe checks

On both `POST` (onboard) and `PUT` (edit), routeForge does
`GET {endpoint}/models` and asserts your `slug` appears among the returned
model ids. The persisted `enabled` is the requested `enabled`
**AND** the probe result:

- Reachable + `enabled: true` → saved enabled, `probe_status: "healthy"`.
- Reachable + `enabled: false` → saved disabled — an admin can explicitly
  take a healthy model out of rotation.
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
guarantee the old `inference_backend: none` reject gave; it's now derived
from onboarding state instead of a static config value, and it holds even
if a previously-healthy model goes unreachable and is later disabled by an
edit or a failed re-probe.

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
write, no restart required.
