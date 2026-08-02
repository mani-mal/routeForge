from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps.models import InfraModel, InfraSettings

_log = logging.getLogger("serving.infra")


@dataclass(frozen=True)
class InfraModelRow:
    model_name: str
    engine: str
    enabled: bool
    replicas: int | None = None
    min_replicas: int | None = None
    max_replicas: int | None = None
    tensor_parallel: int | None = None
    gpu_type: str | None = None
    endpoint: str | None = None
    runai_scheduled: bool = False
    notes: str | None = None


@dataclass(frozen=True)
class InfraClusterRow:
    runai_licensed: bool = False
    runai_project: str | None = None
    default_engine: str = "vllm"


@dataclass(frozen=True)
class InfraSnapshot:
    cluster: InfraClusterRow
    models: dict[str, InfraModelRow]


class InfraRoster:
    """Process-global, lazily-loaded snapshot of the DB-declared on-prem fleet.
    Injected into Router (sync reads on the request path); refreshed on every
    admin write. On a DB error it keeps last-known-good; a cold cache is
    PERMISSIVE (all on-prem enabled) so a DB blip degrades ops visibility, never
    residency — the gate enforces onprem_only -> never-cloud independently.
    See infra-management-config-design.md §6."""

    def __init__(self, settings) -> None:
        self._s = settings
        self._snap: InfraSnapshot | None = None

    async def ensure_loaded(self, db) -> None:
        if self._snap is None:
            await self.refresh(db)

    async def refresh(self, db) -> None:
        try:
            async with db.session() as session:
                self._snap = await self._load(session)
        except Exception:
            # keep last-known-good; cold cache stays None -> permissive current()
            _log.exception("infra roster refresh failed; keeping last-known-good")

    @staticmethod
    async def _load(session: AsyncSession) -> InfraSnapshot:
        cs = await session.get(InfraSettings, "cluster")
        cluster = InfraClusterRow(
            runai_licensed=bool(cs.runai_licensed) if cs else False,
            runai_project=cs.runai_project if cs else None,
            default_engine=cs.default_engine if cs else "vllm")
        rows = (await session.execute(select(InfraModel))).scalars().all()
        models = {
            r.model_name: InfraModelRow(
                model_name=r.model_name, engine=r.engine, enabled=bool(r.enabled),
                replicas=r.replicas, min_replicas=r.min_replicas,
                max_replicas=r.max_replicas, tensor_parallel=r.tensor_parallel,
                gpu_type=r.gpu_type, endpoint=r.endpoint,
                runai_scheduled=bool(r.runai_scheduled), notes=r.notes)
            for r in rows}
        return InfraSnapshot(cluster=cluster, models=models)

    def current(self) -> InfraSnapshot | None:
        return self._snap

    def is_enabled(self, model_name: str) -> bool:
        """Fail toward serving: unknown model or cold cache -> enabled."""
        snap = self._snap
        if snap is None:
            return True
        row = snap.models.get(model_name)
        return True if row is None else row.enabled

    def engine_for(self, model_name: str) -> str | None:
        """Serving engine for an on-prem model. Under global `vllm` it is 'vllm'
        by construction; under `dynamo` it is the per-model engine (or the
        cluster default). Only meaningful for on-prem choices; callers pass an
        on-prem model_name."""
        ib = self._s.serving.inference_backend
        if ib == "vllm":
            return "vllm"
        if ib == "none":
            return None
        # dynamo: heterogeneous workers
        snap = self._snap
        if snap is not None:
            row = snap.models.get(model_name)
            if row is not None and row.engine:
                return row.engine
            return snap.cluster.default_engine
        return None
