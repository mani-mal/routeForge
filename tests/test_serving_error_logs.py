"""Contract: an operator can find why a request failed upstream from the
RouteForge log alone. Endpoints are scrubbed; no prompt content is logged."""
import logging


def test_backend_failure_is_logged_with_request_model_and_scrubbed_error(client, monkeypatch, caplog):
    from app.gateway.backends import litellm_backend

    async def refuse(**k):
        raise RuntimeError("connection refused by 10.0.0.5:8000 secret-prompt-text")

    monkeypatch.setattr(litellm_backend, "acompletion", refuse)
    caplog.set_level(logging.WARNING)
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 502
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "serving.gateway"]
    assert lines, "no gateway failure log line"
    assert any("req-" in l and "RuntimeError" in l and "<endpoint>" in l for l in lines)
    assert not any("10.0.0.5" in l for l in lines)
    assert any(rec.name == "serving.api" and "502" in rec.getMessage() for rec in caplog.records)


def test_unhandled_error_is_logged_with_traceback(client, monkeypatch, caplog):
    async def explode(*a, **k):
        raise KeyError("unexpected")

    monkeypatch.setattr(client.app.state.pipeline, "run", explode)
    caplog.set_level(logging.ERROR)
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 500
    errs = [rec for rec in caplog.records if rec.name == "serving.api" and rec.exc_info]
    assert errs and "req-" in errs[0].getMessage()
