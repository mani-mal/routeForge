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
