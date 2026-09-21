"""Endpoint files for FeasibleRouteBench (spec §4.2).

An endpoint file is YAML: a top-level `endpoints:` list, each entry either
`simulated` or `live` (never both, never neither -- a run is all one mode).
**Load-time validation fails the whole run, never mid-run** -- same discipline
as `app/eval/cases.py::load_suite`.

`harness_settings` builds the harness's own `Settings` (spec §3: never the
deployment's) with every provider an endpoint file names switched on, and the
two cache levers switched off so a sweep measures endpoints, not caches.
`build_registry` builds a scratch, in-memory `ModelRegistry` from the loaded
specs -- the harness never reads or writes a deployment database.

Offline harness code only -- never imported by the serving path.
"""
from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Collection
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError, model_validator

from app.apps.db import Database
from app.apps.registry import ModelRegistry
from app.apps.store import upsert_onboarded
from app.config.schema import Settings


class EndpointFileError(ValueError):
    """An endpoint-file load-time error (spec §4.2), or a scratch-registry
    build failure (see `build_registry`). Names the offending entry's slug
    (or `<file>` when the error isn't tied to one entry), so a load failure
    is actionable without re-reading the file."""

    def __init__(self, slug: str | None, message: str) -> None:
        self.slug = slug
        label = slug if slug else "<file>"
        super().__init__(f"{label}: {message}")


class LatencyProfile(BaseModel):
    mean: float
    jitter: float = 0.0


class SimProfile(BaseModel):
    quality: dict[str, float]
    latency_ms: LatencyProfile

    @model_validator(mode="after")
    def _check_quality(self) -> "SimProfile":
        if "default" not in self.quality:
            raise ValueError("simulated.quality must include a 'default' entry")
        for workload, value in self.quality.items():
            if not (0.0 <= value <= 1.0):
                raise ValueError(
                    f"simulated.quality[{workload!r}] = {value!r} is out of [0,1]")
        return self


class LiveTarget(BaseModel):
    endpoint: str
    api_key_env: str | None = None


class PriceSpec(BaseModel):
    input: float
    output: float
    cached_input: float | None = None


class EndpointSpec(BaseModel):
    slug: str
    provider: str
    tier: str
    price: PriceSpec
    quality: float | None = None
    region: str | None = None
    prompt_retention: str | None = None
    accepts_raw_sensitive: bool | None = None
    simulated: SimProfile | None = None
    live: LiveTarget | None = None


# `api_key_env` is a variable NAME, never a value that looks like a secret.
# Global Constraints' B32 lesson: reject a value containing `sk-`, containing
# whitespace, or that is >=32 chars drawn only from [A-Za-z0-9_-] AND contains
# a lowercase letter (a real env var name is conventionally all-uppercase).
_ENV_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")


def _check_not_key_looking(slug: str, value: str) -> None:
    if "sk-" in value:
        raise EndpointFileError(
            slug, f"api_key_env {value!r} looks like a key value, not a variable name")
    if any(ch.isspace() for ch in value):
        raise EndpointFileError(
            slug, f"api_key_env {value!r} must not contain whitespace")
    if (len(value) >= 32 and _ENV_TOKEN_RE.fullmatch(value)
            and any(ch.islower() for ch in value)):
        raise EndpointFileError(
            slug, f"api_key_env {value!r} looks like a key value, not a variable name")


def load_endpoints(
    path: str, *, live: bool, tiers: Collection[str],
) -> tuple[list[EndpointSpec], str]:
    """Load a YAML endpoint file (spec §4.2).

    `tiers` is the deployment's canonical tier list (`settings.routing.tiers`)
    -- callers pass it in rather than this module hard-coding
    `cheap`/`mid`/`frontier`, since tiers are config, not a harness constant.

    Returns `(specs, digest)` where `digest` is the sha256 hex digest of the
    file's raw bytes.

    Raises `EndpointFileError` for every §4.2 load error: a malformed
    document or entry; both or neither of `simulated`/`live` on one entry; a
    `live` entry when `live=False`; a `simulated` entry when `live=True` (a
    run is all one mode); a `tier` not in `tiers`; a duplicate `slug`; or an
    `api_key_env` that looks like a key rather than a variable name.
    """
    raw_bytes = Path(path).read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()

    try:
        doc = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        raise EndpointFileError(None, f"invalid YAML: {exc}") from exc

    entries = doc.get("endpoints") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        raise EndpointFileError(None, "missing or invalid top-level 'endpoints' list")

    specs: list[EndpointSpec] = []
    seen_slugs: set[str] = set()

    for index, obj in enumerate(entries):
        if not isinstance(obj, dict):
            raise EndpointFileError(f"<entry {index}>", "endpoint entry must be a mapping")

        raw_slug = obj.get("slug")
        label = raw_slug if isinstance(raw_slug, str) else f"<entry {index}>"

        try:
            spec = EndpointSpec(**obj)
        except ValidationError as exc:
            raise EndpointFileError(label, str(exc)) from exc

        if spec.slug in seen_slugs:
            raise EndpointFileError(spec.slug, "duplicate slug")
        seen_slugs.add(spec.slug)

        if (spec.simulated is None) == (spec.live is None):
            raise EndpointFileError(
                spec.slug, "exactly one of 'simulated' or 'live' is required")
        if spec.live is not None and not live:
            raise EndpointFileError(
                spec.slug,
                "'live' entry present but the run is not --live "
                "(a run is all one mode)")
        if spec.simulated is not None and live:
            raise EndpointFileError(
                spec.slug,
                "'simulated' entry present in a --live run (a run is all one mode)")
        if spec.tier not in tiers:
            raise EndpointFileError(
                spec.slug,
                f"tier {spec.tier!r} is not one of the canonical tiers {sorted(tiers)!r}")
        if spec.live is not None and spec.live.api_key_env is not None:
            _check_not_key_looking(spec.slug, spec.live.api_key_env)

        specs.append(spec)

    return specs, digest


def harness_settings(base: Settings, specs: list[EndpointSpec]) -> Settings:
    """Build the harness's own `Settings` (spec §3/§4.2): a deep copy of
    `base` -- `base` is never mutated -- with every provider named in `specs`
    enabled, semantic cache + memory switched off so the sweep measures
    endpoints, not caches, and the static YAML `routing.model_pool` emptied so
    the endpoint file alone defines which endpoints exist.

    A provider named in `specs` must already be declared in
    `base.serving.providers` (e.g. `onprem`/`azure`/`bedrock` in a stock
    deployment): the harness turns on a deployment's own declared providers,
    it does not invent new ones out of an endpoint file. An undeclared
    provider raises `EndpointFileError` naming the providers that do exist.
    """
    settings = copy.deepcopy(base)
    known = sorted(settings.serving.providers)
    for spec in specs:
        cfg = settings.serving.providers.get(spec.provider)
        if cfg is None:
            raise EndpointFileError(
                spec.slug,
                f"provider {spec.provider!r} is not declared in "
                f"serving.providers (known providers: {known})")
        cfg.enabled = True
    settings.serving.semantic_cache.enabled = False
    settings.serving.memory.enabled = False
    # The endpoint file is the WHOLE endpoint universe of a run. The
    # deployment's static YAML pool (e.g. `azure-gpt-mid`) would otherwise
    # rejoin the effective pool the moment its provider is enabled above --
    # swept with no simulated profile, or, under `--live`, called for real
    # although the operator never listed it.
    settings.routing.model_pool = []
    return settings


async def build_registry(
    settings: Settings, specs: list[EndpointSpec],
) -> tuple[ModelRegistry, Database]:
    """Build the harness's scratch registry (spec §3): a fresh in-memory
    SQLite `Database` -- never a deployment's database -- one
    `upsert_onboarded` row per endpoint spec (`enabled=True,
    probe_status="healthy"`), and a `ModelRegistry` refreshed from it.
    """
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()

    async with db.session() as session:
        for spec in specs:
            endpoint = spec.live.endpoint if spec.live is not None else f"sim://{spec.slug}"
            api_key_env = spec.live.api_key_env if spec.live is not None else None
            await upsert_onboarded(
                session, spec.slug,
                display_name=spec.slug,
                provider=spec.provider,
                tier=spec.tier,
                quality=spec.quality,
                price_input=spec.price.input,
                price_output=spec.price.output,
                price_cached_input=spec.price.cached_input,
                endpoint=endpoint,
                api_key_env=api_key_env,
                enabled=True,
                probe_status="healthy",
                region=spec.region,
                prompt_retention=spec.prompt_retention,
                accepts_raw_sensitive=spec.accepts_raw_sensitive,
            )
        await session.commit()

    registry = ModelRegistry(settings)
    await registry.refresh(db)

    # `ModelRegistry.refresh` swallows any exception and keeps last-known-good
    # (app/apps/registry.py) -- exactly right for a live deployment, but a
    # scratch registry that silently fails to load must not produce an
    # empty (or partial) benchmark without saying so. Assert every row we
    # just wrote actually made it into the effective pool.
    loaded = {c.name for c in registry.effective_pool()}
    missing = [spec.slug for spec in specs if spec.slug not in loaded]
    if missing:
        raise EndpointFileError(
            missing[0],
            f"scratch registry refresh did not load: {missing}")

    return registry, db
