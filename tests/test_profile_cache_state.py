"""Task 9: an in-memory ProfileCache lives on app.state, created alongside the
other single-node in-memory stores (RateLimiter, SessionStore, ...) in
app/main.py's create_app(). routes.py reads/populates it per request; the
admin PUT (Task 10) clears it on writes."""


def test_app_has_profile_cache(monkeypatch):
    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    # mock is gone (see tests/conftest.py) -- vllm + the health-probe skip
    # flag keeps create_app() offline the same way conftest's process-wide
    # defaults do.
    monkeypatch.setenv("SP_INFERENCE_BACKEND", "vllm")
    monkeypatch.setenv("SP_SKIP_BACKEND_HEALTHCHECK", "1")
    from app.main import create_app
    from app.apps.privacy import ProfileCache
    app = create_app()
    assert isinstance(app.state.profile_cache, ProfileCache)
