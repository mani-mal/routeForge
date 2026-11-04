"""/admin/metrics/summary and /admin/metrics/models."""
import json

from tests.test_admin_api import _login
from tests.test_admin_usage_by_app import _chat, _seed

PROMPT = "zebra-unique-prompt-text-91827"


def test_summary_requires_admin(client):
    assert client.get("/admin/metrics/summary").status_code == 401
    assert client.get("/admin/metrics/models").status_code == 401


def test_summary_counts_served_requests(client):
    _, tok = _seed(client, "mx-a", "a")
    for _i in range(3):
        _chat(client, tok)
    _login(client)
    body = client.get("/admin/metrics/summary").json()
    assert body["current"]["completed"]["n"] == 3
    assert body["current"]["completed"]["insufficient"] is True
    assert body["current"]["spend"]["value"] > 0
    assert {"routing", "prompt_cache", "compression", "semantic_cache", "total"} <= set(body["current"]["savings"])
    assert {"window", "previous", "alerts", "baseline", "generated_at"} <= set(body)


def test_models_lists_served_model(client):
    _, tok = _seed(client, "mx-b", "b")
    _chat(client, tok); _chat(client, tok)
    _login(client)
    body = client.get("/admin/metrics/models").json()
    assert body["models"]
    row = body["models"][0]
    assert row["requests"] == 2 and row["placement"] in ("onprem", "cloud")


def test_hours_out_of_range_is_422(client):
    _login(client)
    for h in (0, 9000):
        assert client.get(f"/admin/metrics/summary?hours={h}").status_code == 422
        assert client.get(f"/admin/metrics/models?hours={h}").status_code == 422


def test_bad_placement_is_422(client):
    _login(client)
    assert client.get("/admin/metrics/summary?placement=moon").status_code == 422


def test_application_filter_scopes(client):
    a, ta = _seed(client, "mx-c", "c")
    b, tb = _seed(client, "mx-d", "d")
    _chat(client, ta); _chat(client, ta); _chat(client, tb)
    _login(client)
    assert client.get(f"/admin/metrics/summary?application_id={a}").json()["current"]["spend"]["n"] == 2
    assert client.get(f"/admin/metrics/summary?application_id={b}").json()["current"]["spend"]["n"] == 1


def test_response_is_content_free(client):
    _, tok = _seed(client, "mx-e", "e")
    _chat(client, tok, PROMPT)
    _login(client)
    for path in ("summary", "models"):
        assert PROMPT not in json.dumps(client.get(f"/admin/metrics/{path}").json())




# --- seeded-DB tests: rows shaped like the real writers -------------------------------
# build_failure_outcome stores chosen=None, attempts=[<last model tried>] for
# backend_failed / context_overflow, and chosen=None, attempts=[] for no_eligible.

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.apps import store
from app.apps.models import OutcomeFeedback, RoutingOutcome, UsageRecord
from app.main import create_app


def _uses(rid, model, provider, status="ok", cost=0.01, app="seed-app"):
    return UsageRecord(id=f"u-{rid}", application_id=app, request_id=rid, model=model,
                       provider=provider, tier="cheap", cost_usd=cost, status=status,
                       prompt_tokens=10, routing_saved_usd=0.0, compression_saved_usd=0.0,
                       prompt_cache_saved_usd=0.0, semantic_cache_saved_usd=0.0)


def _outc(rid, status, chosen=None, attempts=(), workload=None, app="seed-app"):
    return RoutingOutcome(request_id=rid, application_id=app, status=status, chosen=chosen,
                          attempts=list(attempts), workload=workload, latency_ms=5.0)


@pytest.fixture
def seeded(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/m.db")
    app = create_app()
    objs = []
    # on-prem model: 40 ok (workload w1 for the first 10) + 10 backend failures
    for i in range(40):
        objs += [_uses(f"on{i}", "xm-onprem", "onprem"),
                 _outc(f"on{i}", "ok", "xm-onprem", ["xm-onprem"],
                       workload="w1" if i < 10 else None)]
    for i in range(10):
        objs += [_uses(f"onf{i}", "xm-onprem", "onprem", status="error", cost=0.0),
                 _outc(f"onf{i}", "backend_failed", None, ["xm-onprem"])]
    # cloud model: 35 ok
    for i in range(35):
        objs += [_uses(f"cl{i}", "xm-cloud", "azure"),
                 _outc(f"cl{i}", "ok", "xm-cloud", ["xm-cloud"])]
    # policy rejections: no model, no usage row
    objs += [_outc(f"rej{i}", "no_eligible") for i in range(3)]
    # cancelled / budget 429 usage row: placeholder model, no outcome
    objs += [_uses("bud1", "-", "-", status="rejected", cost=0.5)]

    async def _go():
        async with app.state.db.session() as s:
            await store.create_application(s, app_id="seed-app", name="Seed")
            s.add_all(objs)
            await s.commit()
    asyncio.run(_go())
    c = TestClient(app)
    _login(c)
    return c


def test_per_model_sees_backend_failures_with_chosen_none(seeded):
    # C1 regression: failure outcomes carry chosen=None; the model is attempts[-1].
    row = next(r for r in seeded.get("/admin/metrics/models").json()["models"]
               if r["model"] == "xm-onprem")
    assert row["errors"] == 10
    assert row["completed"]["failures"] == 10
    assert row["completed"]["n"] == 50 and row["completed"]["value"] == pytest.approx(0.8)
    assert row["status"] != "healthy" or row["completed"]["value"] < 1


def test_placement_filter_keeps_attributed_failures(seeded):
    # C2 regression: placement=onprem must keep the backend failures.
    s = seeded.get("/admin/metrics/summary?placement=onprem").json()["current"]
    assert s["completed"]["n"] == 50 and s["completed"]["value"] == pytest.approx(0.8)
    assert s["completed"]["failures"] == 10
    # not attributable to a placement -> null, not a misleading 0
    assert s["completed"]["policy_rejections"] is None and s["completed"]["excluded"] is None
    cloud = seeded.get("/admin/metrics/summary?placement=cloud").json()["current"]
    assert cloud["completed"]["n"] == 35 and cloud["completed"]["failures"] == 0
    full = seeded.get("/admin/metrics/summary").json()["current"]
    assert full["completed"]["policy_rejections"] == 3


def test_placeholder_model_is_not_a_row_but_counts_in_spend(seeded):
    body = seeded.get("/admin/metrics/models").json()
    assert {r["model"] for r in body["models"]} == {"xm-onprem", "xm-cloud"}
    spend = seeded.get("/admin/metrics/summary").json()["current"]["spend"]
    assert spend["n"] == 40 + 10 + 35 + 1
    assert next(r for r in body["models"] if r["model"] == "xm-cloud")["placement"] == "cloud"
    assert next(r for r in body["models"] if r["model"] == "xm-onprem")["placement"] == "onprem"


def test_workload_filter_and_unfiltered_workload_list(seeded):
    cur = seeded.get("/admin/metrics/summary?workload=w1").json()["current"]
    assert cur["spend"]["n"] == 10 and cur["completed"]["n"] == 10
    assert cur["workloads"] == ["w1"]
    # the dropdown list is computed before filtering, so with 2 workloads it keeps both
    # (here only w1 exists; add nothing) -> list is the unfiltered one
    assert seeded.get("/admin/metrics/summary").json()["current"]["workloads"] == ["w1"]


def test_window_of_only_policy_rejections_is_not_empty(monkeypatch, tmp_path):
    # I5 regression (server half): `empty` is false when only rejections exist.
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/e.db")
    app = create_app()

    async def _go():
        async with app.state.db.session() as s:
            await store.create_application(s, app_id="seed-app", name="Seed")
            s.add_all([_outc(f"r{i}", "no_eligible") for i in range(4)])
            await s.commit()
    asyncio.run(_go())
    c = TestClient(app)
    _login(c)
    cur = c.get("/admin/metrics/summary").json()["current"]
    assert cur["spend"]["n"] == 0 and cur["empty"] is False
    assert cur["completed"]["policy_rejections"] == 4
    assert c.get("/admin/metrics/summary?hours=1&application_id=none").json()["current"]["empty"] is True


def test_usage_row_without_outcome_through_route(seeded):
    body = seeded.get("/admin/metrics/summary?workload=w1").json()
    assert body["current"]["spend"]["n"] == 10  # the outcome-less budget row is dropped
