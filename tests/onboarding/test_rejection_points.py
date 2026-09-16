"""HTTP-level coverage for rejection points that exist in code but had no test
driving them through /v1/chat/completions: daily budget exhausted, max_rpm,
required-verification failure, and a revoked token."""
from app.apps import store
from tests.onboarding.harness import conformance_client, onboard

_HI = {"messages": [{"role": "user", "content": "hi"}]}


def test_daily_budget_exhausted_is_429_and_named():
    with conformance_client() as c:
        app = onboard(c, "rp-daily", max_spend_usd_per_day=0.5)

        async def spend():
            async with c.app.state.db.session() as session:
                await store.record_usage(
                    session, application_id="rp-daily", request_id="seed", model="local-8b",
                    provider="onprem", tier="cheap", prompt_tokens=1, completion_tokens=1,
                    total_tokens=2, cost_usd=1.0, baseline_usd=1.0, latency_ms=1.0,
                    sensitivity="public", onprem_only=False, status="ok")
                await session.commit()

        c.portal.call(spend)
        r = app.chat(_HI)
    assert r.status_code == 429
    assert "daily budget exceeded" in r.json()["detail"]


def test_rate_limit_is_429_after_max_rpm():
    with conformance_client() as c:
        app = onboard(c, "rp-rpm", max_rpm=1)
        assert app.chat(_HI).status_code == 200
        r = app.chat(_HI)
    assert r.status_code == 429 and "rate limit" in r.json()["detail"]


def test_required_verification_failure_is_422_not_the_raw_answer(monkeypatch):
    from app.gateway.backends import litellm_backend
    from tests._litellm_fake import make_recording_acompletion, make_response

    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="UNVERIFIABLE")))
    with conformance_client() as c:
        app = onboard(c, "rp-verify")
        # A US SSN (regex detector) -> onprem_only + require_verification
        # (config/policy.yaml sensitivity_rules: US_SSN -> require_verification: true).
        r = app.chat({"messages": [{"role": "user", "content": "my ssn is 482-73-1946"}]})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("response failed verification")
    assert "UNVERIFIABLE" not in r.text.replace(r.json()["detail"], "")


def test_revoked_token_is_401():
    with conformance_client() as c:
        app = onboard(c, "rp-revoke")
        # No standalone GET /admin/applications/{id}/tokens route exists; tokens
        # are embedded in the application detail payload (see admin_routes._app_dict).
        # onboard() creates the app (which auto-issues a "default" token) and then
        # issues a second "conformance" token, which is the one app.chat() actually
        # uses -- so pick by name rather than assuming a single-element list.
        detail = c.get("/admin/applications/rp-revoke").json()
        tok_id = next(t["id"] for t in detail["tokens"] if t["name"] == "conformance")
        assert c.post(f"/admin/tokens/{tok_id}/revoke", json={}).status_code == 200
        r = app.chat(_HI)
    assert r.status_code == 401
