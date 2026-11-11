"""B80: the effective quality floor for one request.

Pure: no I/O, no settings mutation. `build_context` calls it once and puts the
answer on the policy context; the router reads only those two ctx fields and
never re-derives the floor.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.config.schema import Settings

if TYPE_CHECKING:
    from app.apps.policy import AppPolicyView


def resolve_floor(settings: Settings, app_policy: "AppPolicyView | None",
                  workload: str | None) -> tuple[float | None, str]:
    """`(floor, effective_mode)` for `workload`.

    Mode: the app's own, else the deployment's. Floor: raise-only -- the max of
    the deployment value and the app value present for that workload; `None`
    when there is no workload or neither side names one.
    """
    dep = settings.routing.quality_floors
    mode = (app_policy.quality_floor_mode
            if app_policy is not None and app_policy.quality_floor_mode is not None
            else dep.mode)
    if workload is None:
        return None, mode
    values = []
    if workload in dep.workloads:
        values.append(float(dep.workloads[workload]))
    if app_policy is not None and app_policy.quality_floors:
        values.extend(f for w, f in app_policy.quality_floors if w == workload)
    return (max(values) if values else None), mode
