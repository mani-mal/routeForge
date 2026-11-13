# Switchyard integration (B100: dependency contract)

NVIDIA NeMo Switchyard (PyPI `nemo-switchyard`, import name `switchyard`) is an
**optional, unmodified, exact-pinned upstream dependency**. RouteForge will use it as a
routing *advisor* (see
[`../research/RouteForge-Switchyard-Feature-Comparison-and-Integration-Recommendation.md`](../research/RouteForge-Switchyard-Feature-Comparison-and-Integration-Recommendation.md)
and backlog B100-B112).

> **Status.** B100 shipped the optional extra, the version contract, the config block, the
> lint codes and the import guard; B103 the adapter; **B104/B105 (2026-11-12) the shadow
> mode**: with `mode: shadow` the advisor runs on every routed request and its proposal is
> **recorded, never acted on** (see [Shadow mode](#shadow-mode-b104b105) below). `enforce`
> is B108; until it lands, `mode: enforce` is recorded truthfully as `enforce` but behaves
> exactly like `shadow`. The contract suite is B110.

## Ownership boundary

| RouteForge owns | Switchyard may do |
|---|---|
| Auth, the policy gate, the feasible set (residency, data handling), budgets, execution, failover, audit | Advise on a choice **among candidates RouteForge has already made feasible** |

Switchyard never sees a request before the policy gate, never widens the candidate set,
and never lowers the risk floor (CLAUDE.md rules 1, 3, 8).

## Separation rule

- Upstream stays unmodified: no vendoring, no fork, no monkey-patching.
- A needed behaviour change is proposed upstream; until it ships, RouteForge adapts inside
  the adapter package.
- `app/routing/switchyard/` is the **only** package that may import `switchyard` (or
  `switchyard_rust`). `tests/routing/switchyard/test_import_guard.py` scans `app/`,
  `scripts/` and `tests/` and fails otherwise. Importing the adapter itself
  (`app.routing.switchyard`) is fine anywhere.
- Consequence: an upstream release changes one adapter package and its contract tests.

## Installing

```bash
uv sync --extra switchyard                    # from a checkout
pip install 'serving-plane[switchyard]'       # the project name in pyproject.toml
```

The extra pins `nemo-switchyard==0.4.0a1` exactly and is never a core dependency. A
default install does not pull it in; RouteForge boots and passes its tests without it.

## Configuration

An optional `switchyard:` block in `config/routing.yaml` (step-6 policy, so routing owns
it; in any other file the lint fails with `CONCERN_IN_WRONG_FILE`). An absent block means
`mode: off`. `config/routing.yaml` carries a commented-out example.

| Field | Type / default | Meaning |
|---|---|---|
| `mode` | `off` \| `shadow` \| `enforce`, default `off` | Quote `"off"` in YAML (bare `off` is a boolean; it is coerced to `off` anyway). |
| `algorithm` | str, default `stage_router` | Only `stage_router` is accepted. |
| `picker` | `efficient_first` \| `capable_first`, default `efficient_first` | Tie preference between efficient and capable. |
| `confidence_threshold` | float 0..1, default `0.5` | Minimum advisor confidence to act on. |
| `timeout_ms` | int > 0, default `20` | Advisor call budget. |
| `classifier` | mapping or null, default null | Not allowed until B111. |
| `capable_tier` | `mid` \| `frontier`, default `frontier` | Tier a `capable` proposal maps to (`efficient` always maps to `cheap`). The router's risk floor and best-allowed clamp still apply afterwards (design §3). |

## Shadow mode (B104/B105)

> **Release note (B104/B105, 2026-11-12).** This release adds six `switchyard_*` columns to
> `routing_outcomes`. Run the six ALTERs in
> [`routing-outcomes.md`](./routing-outcomes.md) (or `scripts/migrate_app_policies.py`)
> **before rolling this image, in every mode, including `off`**: the ORM names the columns
> in every outcome write, so on an unmigrated table every outcome write fails. Serving
> continues (fail-open, logged as `outcome record failed`) and the B62 boot check warns of
> the missing columns, but no outcome rows are recorded until the ALTERs run.

Design: [`../architecture/switchyard-advisor-design.md`](../architecture/switchyard-advisor-design.md)
§4-§9.

**What runs.** With `routing.switchyard.mode != off`, `create_app()` builds **one**
`SwitchyardAdvisor` at startup and hands it to the `Pipeline`. With `off` nothing is built,
the upstream package is never imported, and the request path is byte-for-byte unchanged.
If construction fails, the error is logged at ERROR and serving continues without the
advisor (advice fails open; policy never does).

On every request that reaches routing, on both the ordinary and the streaming path, the
pipeline calls `advise(comp.messages)` **once**, after compression and before
`gateway.run` / `gateway.run_stream`. Those messages are post-gate, `model_input`-shaped and
post-compression: exactly what the model will see (rules 1 and 7). The proposal is reused
across failover and escalation, never recomputed. It does **not** run on a semantic-cache
hit, a gate rejection or a rejected model pin, since those never route.

**What it changes: nothing.** The proposal never touches the tier target, `complexity`,
`min_tier` or anything passed to the gateway or router. The served model, response bodies,
metering and cost are identical to `off`. Only the trace, the outcome row and the served
DecisionRecord's `advisor` field differ. `tests/routing/switchyard/test_shadow_identity.py`
pins this for `shadow` and `enforce`, on non-stream and stream traffic including an
`onprem_only` high-risk request. `enforce` does not pass `advisor_tier` to `Router.select`
until B108.

**Failure.** `advise()` never raises; a timeout (`timeout_ms`, default 20), an exception, an
unconvertible message, an invalid role or a requested routing-time call becomes a fallback
with a cause. If the advisor or the recording code raises anyway, the pipeline logs at ERROR
(the exception type only, never the messages) and the request continues unchanged.

### What is recorded, and where to see it

All values come from the allowlisted `AdvisorResult` record (ids, enums, numbers, bools).
`agreed` is true when the proposal's tier equals the tier actually **served** (the served
router hop's `chosen_tier`), false when routing served another tier (for example a
high-risk request floored above an `efficient` proposal), and null on a fallback.

| Where | What |
|---|---|
| **Trace (root span)** | `sp.switchyard.mode`, `.role`, `.tier`, `.confidence`, `.agreed`, `.fallback_cause`, `.latency_ms`, `.version`, `.algorithm`. Null values are omitted. Emitted regardless of `capture_io`, like `sp.candidates`. A fallback still carries `version` and `algorithm`. |
| **Langfuse trace tags** | `langfuse.trace.tags` (the attribute the pinned SDK, langfuse 4.14.1, defines as `LangfuseOtelSpanAttributes.TRACE_TAGS`): `switchyard`, `switchyard:shadow` or `switchyard:enforce`, and `switchyard:agreed`, `switchyard:overridden` or `switchyard:fallback`. A fixed vocabulary (`SWITCHYARD_TAGS` in `app/obs/export.py`); the exporter refuses any other tag. Filter advisor traffic in the Langfuse UI by these tags. Traces go to the same project as always: the app's own, or the shared one. |
| **DecisionRecord** | `advisor` on the **served** router hop, on the operator surfaces only (the console's request snapshot, the live decision stream); it is removed from the client-bound response `trace` at the end of `_finalize`, so the client body is identical to `off`. The record: `AdvisorResult.to_record()` (role, tier, confidence, allowlisted evidence, version, algorithm, fallback cause, latency) plus `mode` and `agreed`. Absent on every other hop, and on every record when the advisor did not run. |
| **`routing_outcomes`** | `switchyard_mode`, `switchyard_role`, `switchyard_tier`, `switchyard_confidence`, `switchyard_agreed`, `switchyard_fallback_cause` (nullable; deployed DBs need the ALTERs in [`routing-outcomes.md`](./routing-outcomes.md)). A failed request that reached routing keeps the proposal with `agreed` null. |

Never recorded: the messages, any free-text evidence, client headers. The containment
property test (`tests/routing/switchyard/test_shadow_containment.py`) feeds random
PII-shaped content and a canary token through message content and tool results and finds
none of it in any trace attribute or tag, outcome row, DecisionRecord `advisor` field or
log line.

### Not yet built (B105 follow-ups)

- The console decision inspector does not yet render the `advisor` field ("advisor proposed
  X (role, confidence)"); it is in the data the inspector already loads.
- The policy-simulator / `routing_outcomes` report of agreement rate and the counterfactual
  cost of following the advisor, per app and per workload. The columns above are what it
  will read.
- Advisor overhead p50/p99 measured on the DGX `rf-pytest` pod.

## Lint codes

All fire only when `mode != off`. With `mode: off` (or no block) nothing about Switchyard
is checked.

- `SWITCHYARD_NOT_INSTALLED`: the `nemo-switchyard` distribution is not installed.
- `SWITCHYARD_VERSION_UNSUPPORTED`: installed version is outside
  `>=0.4.0a1,<0.5` (`SUPPORTED` in `app/routing/switchyard/__init__.py`).
- `SWITCHYARD_ALGORITHM_UNKNOWN`: `algorithm` is not `stage_router`, or a `classifier` is
  set (only classifier-less `stage_router` is allowed until B111).

Installation and version are read from package metadata; the lint never imports
`switchyard`.

## Packaging (B107)

**Decision: the package is in the default image extras** (`EXTRAS="observability,postgres,switchyard"`
in the `Dockerfile`, `deploy/compose/docker-compose.yml`, `deploy/spark/docker-compose.yml`
and the Helm default). It is inert while `routing.switchyard.mode` is `off`, so a default
deployment behaves exactly as before. Build with `--build-arg EXTRAS="observability,postgres"`
to leave it out (turning the mode on then fails boot with `SWITCHYARD_NOT_INSTALLED`).

Measured on Python 3.12, linux x86_64, wheel installed into an empty venv:

| Measure | Value |
|---|---|
| Wheel size, x86_64 / aarch64 | 9.71 MB / 9.84 MB (one wheel per arch, `cp310-abi3`, no dependencies) |
| `site-packages` growth | about 28 MB (`switchyard_rust` native library 28 MB, `switchyard` 24 KB) |
| `import switchyard.libsy` | about 54 ms (3 runs: 54.4, 54.3, 53.6 ms) |

The growth is under the 30 MB threshold set for B107, which is why it ships by default
rather than as a variant. `uv.lock` already resolves both manylinux wheels, so the image
build needs nothing extra on either arch.

### Enabling it

Default is off. `mode: shadow` is set through the existing `SP_CONFIG_OVERLAY_DIR`
mechanism (a `routing.yaml` merged over the shipped one; the lint runs on the merged result):

- **Compose:** `docker compose -f docker-compose.yml -f docker-compose.switchyard.yml up -d`
  from `deploy/compose/`. The overlay lives in `deploy/compose/config-overlay-switchyard/`.
- **Helm:** `--set servingPlane.switchyard.mode=shadow`. The chart renders
  `<release>-routing-overlay`, mounts it at `/etc/routeforge/config-overlay` and sets
  `SP_CONFIG_OVERLAY_DIR`. The chart offers `off | shadow` only; `enforce` arrives with B108.
  If you already set `SP_CONFIG_OVERLAY_DIR` through `extraEnv`, put the `switchyard:` block
  in your own `routing.yaml` overlay instead.

### Air-gapped install

On a connected machine, download the exact wheel for each architecture. The wheel has no
dependencies, so `--no-deps` is sufficient:

```bash
for arch in x86_64 aarch64; do
  pip download --no-deps --only-binary=:all: \
    --platform manylinux2014_$arch --python-version 3.12 \
    -d wheelhouse/$arch nemo-switchyard==0.4.0a1
done
# nemo_switchyard-0.4.0a1-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl   9.71 MB
# nemo_switchyard-0.4.0a1-cp310-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl 9.84 MB
```

(`--only-binary=:all:` is required with `--platform`; the `manylinux2014` tag matches the
`manylinux_2_17` wheel.) Verify them against the hashes in `uv.lock`, copy both directories
into the internal mirror or onto the transfer media, then install offline on the target host
or while building the image on an internal builder:

```bash
pip install --no-index --find-links /mnt/mirror/wheelhouse/$(uname -m) nemo-switchyard==0.4.0a1
pip show nemo-switchyard            # Version: 0.4.0a1
```

Then confirm with the boot lint: set `switchyard.mode: "shadow"` and start the API (or run
`load_settings()` plus `lint_settings()`); it must not report `SWITCHYARD_NOT_INSTALLED` or
`SWITCHYARD_VERSION_UNSUPPORTED`. For an image build with no internet, point `uv` at the mirror
(`UV_FIND_LINKS` or `UV_INDEX_URL`) or pre-build the image on a connected host and load it.

### No network at import or run time

Static read: the installed Python layer is 512 lines in `switchyard` and `switchyard_rust`
and imports nothing from `socket`, `urllib`, `http`, `requests`, `aiohttp` or `subprocess`.
All routing logic is in the native library; model calls are made by the caller (RouteForge),
not by Switchyard, through the `Step.CallModel` / `Step.CallDecision` hand-off.

Dynamic check (2026-11-12, x86_64, 0.4.0a1): inside `unshare -rn` (no network interfaces; a
control `connect()` to 1.1.1.1:443 fails with `Network is unreachable`), `import switchyard.libsy`,
`algorithms.stage_router(picker="efficient_first", confidence_threshold=0.5)`, and one
`run_stream` with the request `{"messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]}`
and models `{"efficient":["efficient"],"capable":["capable"],"any":["efficient","capable"]}`
iterated to `Step.Done` all succeed. A classifier is not configured (B111 is not shipped),
which is the only configuration that could involve a model call.

## Supported versions

| RouteForge | nemo-switchyard | Status |
|---|---|---|
| `main` | `0.4.0a1` (exact pin) | Provisional until a 0.4.x final is released |

## Upgrade playbook

1. Read upstream's CHANGELOG for breaking entries.
2. Bump the exact pin in `pyproject.toml` and `SUPPORTED` (with `_LOWER`/`_UPPER`) in
   `app/routing/switchyard/__init__.py`; run `uv lock`.
3. Run `uv run pytest tests/routing/switchyard/`.
4. Fix only the adapter package. If a fix would need changes elsewhere, the boundary has
   leaked; fix that instead.

## Attribution

Switchyard is licensed Apache-2.0 and ships a NOTICE file. It is installed from PyPI, not
redistributed in this repository. See the root `NOTICE` for the attribution entry.
