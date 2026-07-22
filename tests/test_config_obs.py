import os
import pytest
from app.config.loader import load_settings
from app.config.lint import enforce, ConfigLintError

def test_observability_loaded_with_defaults(monkeypatch):
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    s = load_settings()
    lf = s.serving.observability.langfuse
    assert lf.enabled is True
    assert lf.redact_on_export is True
    assert lf.public_key == "pk-test"
    assert lf.secret_key == "sk-test"
    # host has a ${LANGFUSE_HOST:-...} default in yaml; unset env -> the default
    assert lf.host == "http://langfuse:3000"

def test_host_env_expansion(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
    s = load_settings()
    assert s.serving.observability.langfuse.host == "http://localhost:3000"

def test_lint_rejects_redact_off(tmp_path, monkeypatch):
    # redact_on_export must be true when langfuse is enabled
    s = load_settings()
    object.__setattr__(s.serving.observability.langfuse, "redact_on_export", False)
    with pytest.raises(ConfigLintError, match="redact_on_export"):
        enforce(s)
