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
