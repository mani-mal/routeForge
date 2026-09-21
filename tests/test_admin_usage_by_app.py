"""Durable cross-app usage rollup: store query + admin endpoint (F1 Phase C / B15)."""
from tests.test_admin_api import _login


def _seed(client, app_id, name):
    _login(client)
    r = client.post("/admin/applications", json={"id": app_id, "name": name})
    body = r.json()
    return body["application"]["id"], body["token"]


def _chat(client, token, text="hello"):
    return client.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"messages": [{"role": "user", "content": text}]})


def test_by_application_rollup_separates_apps(client):
    a_id, a_tok = _seed(client, "rollup-alpha", "alpha")
    b_id, b_tok = _seed(client, "rollup-beta", "beta")
    _chat(client, a_tok); _chat(client, a_tok); _chat(client, b_tok)
    _login(client)
    body = client.get("/admin/usage/by-application").json()
    by_id = {row["application_id"]: row for row in body["apps"]}
    assert by_id[a_id]["count"] == 2
    assert by_id[b_id]["count"] == 1
    assert by_id[a_id]["total_usd"] >= 0.0
    assert "tier_distribution" in by_id[a_id]


def test_by_application_reconciles_with_usage_summary(client):
    a_id, a_tok = _seed(client, "rollup-gamma", "gamma")
    _chat(client, a_tok); _chat(client, a_tok)
    _login(client)
    per_app = {r["application_id"]: r for r in client.get("/admin/usage/by-application").json()["apps"]}
    scoped = client.get(f"/admin/usage?application_id={a_id}").json()
    assert per_app[a_id]["count"] == scoped["count"]
    assert abs(per_app[a_id]["total_usd"] - scoped["total_usd"]) < 1e-9


def test_by_application_reconciles_with_rejected_rows(monkeypatch, tmp_path):
    """A rejected UsageRecord with a NONZERO cost/token footprint must not
    leak into count/total_usd/distributions -- usage_by_application must
    filter to status=="ok" for everything except rejected_count, exactly
    like usage_summary (app/apps/store.py:211-246). Uses a file-backed DB
    (not the in-memory StaticPool the `client` fixture defaults to) so a
    direct store.record_usage() seed on one asyncio.run() loop and the
    TestClient's requests on another loop see the same data -- same pattern
    as tests/test_egress_custom_redaction.py's end-to-end test."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.apps import store
    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/rf.db")
    app = create_app()
    app_id, raw_token = "rollup-delta", "rollup-delta-token"

    async def _seed():
        async with app.state.db.session() as s:
            await store.create_application(s, app_id=app_id, name="Delta")
            await store.add_token(s, application_id=app_id, raw_token=raw_token)
            await store.record_usage(
                s, application_id=app_id, request_id="req-ok-1", model="m",
                provider="onprem", tier="cheap", prompt_tokens=10,
                completion_tokens=5, total_tokens=15, cost_usd=0.01,
                baseline_usd=0.02, latency_ms=5.0, sensitivity="public",
                onprem_only=True, status="ok")
            # Deliberately nonzero cost/tokens: if the ok-only filter is
            # missing, this leaks into total_usd/total_tokens/count and the
            # reconciliation assertions below fail.
            await store.record_usage(
                s, application_id=app_id, request_id="req-rej-1", model="m",
                provider="onprem", tier="cheap", prompt_tokens=1,
                completion_tokens=0, total_tokens=1, cost_usd=99.0,
                baseline_usd=99.0, latency_ms=1.0, sensitivity="public",
                onprem_only=True, status="rejected")
            await s.commit()

    asyncio.run(_seed())

    client = TestClient(app)
    _login(client)
    per_app = {r["application_id"]: r
              for r in client.get("/admin/usage/by-application").json()["apps"]}
    scoped = client.get(f"/admin/usage?application_id={app_id}").json()

    row = per_app[app_id]
    assert row["rejected_count"] == 1
    assert row["count"] == scoped["count"] == 1
    assert abs(row["total_usd"] - scoped["total_usd"]) < 1e-9
    assert row["total_usd"] < 99.0


def test_by_application_and_summary_report_error_count(monkeypatch, tmp_path):
    """Backend-failure rows (status == "error", spec §4 / B38) are counted in
    `error_count` on BOTH admin surfaces and in nothing else: `count`, spend
    and tokens stay the `ok` rows' alone, even when an error row carries a
    nonzero footprint. Same file-backed-DB seeding as the rejected-rows test
    above."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.apps import store
    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/rf.db")
    app = create_app()
    app_id = "rollup-epsilon"

    def _usage(request_id, status, cost, tokens):
        return dict(application_id=app_id, request_id=request_id, model="m",
                    provider="onprem", tier="cheap", prompt_tokens=tokens,
                    completion_tokens=0, total_tokens=tokens, cost_usd=cost,
                    baseline_usd=cost, latency_ms=5.0, sensitivity="public",
                    onprem_only=True, status=status)

    async def _seed():
        async with app.state.db.session() as s:
            await store.create_application(s, app_id=app_id, name="Epsilon")
            await store.record_usage(s, **_usage("req-ok-1", "ok", 0.01, 15))
            await store.record_usage(s, **_usage("req-err-1", "error", 50.0, 500))
            await store.record_usage(s, **_usage("req-err-2", "error", 50.0, 500))
            await store.record_usage(s, **_usage("req-can-1", "cancelled", 0.0, 0))
            await s.commit()

    asyncio.run(_seed())

    client = TestClient(app)
    _login(client)
    per_app = {r["application_id"]: r
               for r in client.get("/admin/usage/by-application").json()["apps"]}
    scoped = client.get(f"/admin/usage?application_id={app_id}").json()

    row = per_app[app_id]
    assert row["error_count"] == scoped["error_count"] == 2
    assert row["count"] == scoped["count"] == 1
    assert row["rejected_count"] == scoped["rejected_count"] == 0
    assert abs(row["total_usd"] - 0.01) < 1e-9
    assert abs(scoped["total_usd"] - 0.01) < 1e-9
    assert row["total_tokens"] == scoped["total_tokens"] == 15
