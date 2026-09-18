# Creating a sample application (and pointing it at an onboarded model)

This is the **admin** side of the flow: stand up a test application, scope a model to
it, issue a token, and send traffic so you can watch routeForge route to your
newly-onboarded model. For the *application developer* side (calling the API with a
token), see [`integrating-an-app.md`](./integrating-an-app.md).

> Two-sided scoping. A model becomes a candidate for a request only if **both** sides
> permit it: the **model's** `allowed_app_ids` (set on the Infrastructure panel) must
> include the app, **and** the **app's policy** (`allowed_tiers` / `allowed_providers` /
> `allowed_models`) must allow that tier/provider/model. If either side excludes it, the
> router drops the candidate with a visible reason.

## 0. Prerequisites

- You're logged into the admin console (`admin@routeforge.local` on the lab).
- At least one model is onboarded and **healthy** (Infrastructure → Onboard). On the DGX
  lab that's a cheap on-prem model available to all apps, plus `nemotron-nano-8b-v1`
  (mid, scoped to `routeforge_sample`) — check Infrastructure → Models for the current
  slugs on your deployment.
- Base URLs on the DGX lab: UI `http://10.89.197.201:30800`, API
  `http://10.89.197.201:30808`.

## 1. Create the application

**UI:** Admin → Applications → **Create application**. Give it an **id** (a stable slug,
e.g. `sample-app`) and a **name**. A token is issued and shown **once** — copy it now.
(Later tokens are issued and revoked in the app's **Settings** tab, **step 1 Access**;
each is likewise revealed only once.)

**API:**

```bash
curl -sX POST http://10.89.197.201:30808/admin/applications \
  -H "Content-Type: application/json" --cookie "rf_admin_session=<session>" \
  -d '{"id":"sample-app","name":"Sample App","default_residency":"any"}'
# → {"application": {...}, "token": "rf_live_…"}   ← the token appears ONCE
```

`default_residency` is `any` (cloud allowed) or `onprem_only` (never leaves on-prem).
The privacy gate can still *raise* residency per-request (e.g. it detects an SSN), but
never lowers it below the app default.

## 2. Set the application's policy

This is the app-side half of scoping. Leave a field **null/empty to allow everything**;
set it to restrict. To let `sample-app` reach a **mid-tier on-prem** model like the
Nemotron, its policy must allow the `mid` tier and the `onprem` provider.

**UI:** Admin → Applications → *sample-app* → **Settings**. Tiers, providers, residency
and models are **step 2 Placement**; the budget and rate caps are **step 5 Delivery and
limits**. Both steps share one **Save policy** button — pressing it in either saves both.
(Unsure what a new app should be set to? See
[`choosing-app-settings.md`](./choosing-app-settings.md).)

**API:**

```bash
curl -sX PUT http://10.89.197.201:30808/admin/applications/sample-app/policy \
  -H "Content-Type: application/json" --cookie "rf_admin_session=<session>" \
  -d '{
        "allowed_tiers": ["cheap","mid","frontier"],
        "allowed_providers": ["onprem"],
        "residency": "onprem_only",
        "max_spend_usd_per_day": 5.0,
        "max_rpm": 60
      }'
```

- `allowed_tiers` — which of `cheap | mid | frontier` this app may use.
- `allowed_providers` — `onprem`, `azure`, `bedrock`. On the lab only `onprem` is on.
- `allowed_models` — optional allow-list of specific slugs; null = any model the tiers/
  providers permit.
- `residency` — per-app residency (`any` | `private_cloud_ok` | `onprem_only`), same
  meaning as step 1. This is the **placement** axis and the only one: it says where
  compute may run, and nothing about how much is done to the data.
- `max_spend_usd_per_day` / `max_rpm` — budget and rate caps (`429` when exceeded).

The policy body above sets no **protection** fields, so the app inherits the deployment's
defaults — normally `policy_mode: enforced`, which resolves to `model_input: redact` and
`client_output: redact`. That is the right starting point. The protection axes
(`model_input`, `client_output`, `capture_io`, `pii_detection`, `content_integrity`,
`stream_mode`), the precedence order and every validation error code are in
[`per-app-config.md`](../reference/per-app-config.md#the-axes--the-authoritative-model);
[`choosing-app-settings.md`](./choosing-app-settings.md) is the decision guide if you are
picking them for a real customer.

> **Effective placement is two-sided, and can be raised per request.** Both of these must
> hold for a model to be a candidate: the **model's** `allowed_app_ids` must permit the
> application (§3 below), *and* the **application's** `allowed_providers` /
> `allowed_tiers` / `allowed_models` must permit the model. On top of that, the request's
> effective residency may be **raised** above the app's own floor by a declared
> `sensitivity_rules` entry when the gate detects an entity (an SSN forces
> `onprem_only`), gated per app by `escalate_residency_on_detection`. It is raise-only:
> neither a protection setting nor a telemetry setting can ever lower the application's
> residency floor, and an empty eligible set is a clean rejection, never a silent
> widening to cloud.

## 3. Scope the model to the application (model side)

If you onboarded the model with **applications = (none selected)** it's usable by *all*
apps and you can skip this. If you scoped it to specific apps (e.g. Nemotron →
`routeforge_sample`), add your new app so it becomes a candidate.

Change the app scope either way — both hit the same `PUT` and both re-probe the endpoint:

- **UI (shipped `ui:0.17.0`, B21):** Infrastructure → the model's row → **edit**. The form
  is seeded from the stored row, so you only change `allowed_app_ids` and save. The slug is
  fixed by the path and can't be renamed by an edit (that's a delete + re-onboard).
- **API:** `PUT /admin/infra/models/{slug}` with the model's full field set and the new
  `allowed_app_ids`.
  ```bash
  curl -sX PUT http://10.89.197.201:30808/admin/infra/models/nvidia/llama-3.1-nemotron-nano-8b-v1 \
    -H "Content-Type: application/json" --cookie "rf_admin_session=<session>" \
    -d '{
          "slug":"nvidia/llama-3.1-nemotron-nano-8b-v1",
          "display_name":"nemotron-nano-8b-v1",
          "tier":"mid",
          "endpoint":"http://10.89.197.201:30890/v1",
          "price_input":0.2, "price_output":0.2,
          "allowed_app_ids":["routeforge_sample","sample-app"]
        }'
  ```

## 4. Send a test request

Point any OpenAI client at the API base and pass the app token as the API key:

```bash
curl -sX POST http://10.89.197.201:30808/v1/chat/completions \
  -H "Authorization: Bearer rf_live_…" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Explain a red-black tree."}]}'
```

`model: "auto"` lets the router choose. Send a *specific slug* to force a model your app
is allowed to use.

## 5. Watch it route

Open the console (`http://10.89.197.201:30800`) → live decision stream / request
drilldown. Per request you'll see the tier picked, the model chosen, every rejected
candidate with its reason (`excluded · residency`, `not permitted for
application`, `above target tier`, `lower score`…), cost, and latency.

**Why a mid-tier model may not get traffic:** the router picks a **target tier from the
request's complexity** (plus any risk floor). A trivial prompt targets `cheap`, so a
`mid` model is dropped as *above target tier* and the cheap model serves. To exercise
the Nemotron, send a request whose complexity lands in the mid band (a
longer reasoning prompt), or temporarily set its tier to `cheap`.
