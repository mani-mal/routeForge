# Switchyard integration (B100: dependency contract)

NVIDIA NeMo Switchyard (PyPI `nemo-switchyard`, import name `switchyard`) is an
**optional, unmodified, exact-pinned upstream dependency**. RouteForge will use it as a
routing *advisor* (see
[`../research/RouteForge-Switchyard-Feature-Comparison-and-Integration-Recommendation.md`](../research/RouteForge-Switchyard-Feature-Comparison-and-Integration-Recommendation.md)
and backlog B100-B112).

> **Status.** As of B100 the integration does **nothing at runtime**. B100 ships only the
> optional extra, the version contract, the config block, the lint codes and the import
> guard. The `shadow` and `enforce` modes are wired by B105 and B108; the contract suite
> is B110.

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
