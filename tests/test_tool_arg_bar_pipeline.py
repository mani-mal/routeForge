"""B69 end to end: the tool-argument bar through the real app, both
directions, for an enforced app (`client_output: redact`).

The detector is a Presidio-shaped stub (tests/test_tool_arg_bar.py) so the
scores are deterministic without spaCy: 0.3 `US_DRIVER_LICENSE` on `n1`,
0.85 `PERSON` on "John Smith". A real-Presidio regression test sits at the
bottom and skips when `presidio_analyzer` is not installed."""
from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.main as main_mod
import app.obs.export as export_mod
import app.obs.otel as otel
from app.config.lint import ConfigLintError, enforce, lint_settings
from app.config.loader import load_settings
from app.gateway.backends import litellm_backend
from app.main import create_app
from tests._litellm_fake import CALLS, make_recording_acompletion, make_response
from tests.test_tool_arg_bar import StubDetector

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}
_TOOLS = [{"type": "function", "function": {
    "name": "bash", "description": "run a shell command",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}]
CMD = "find src -type f -print0 | xargs -0 -n1 printf '%q\\n'"
CMD_ARGS = json.dumps({"command": CMD})
PERSON_ARGS = json.dumps({"user": "John Smith"})


@pytest.fixture
def stub_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(main_mod, "make_detector", lambda mode: StubDetector())
    return TestClient(create_app())


def _app(client, app_id: str, **policy) -> dict:
    assert client.post("/admin/login", json=_ADMIN).status_code == 200
    created = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert created.status_code == 200, created.text
    if policy:
        put = client.put(f"/admin/applications/{app_id}/policy", json=policy)
        assert put.status_code == 200, put.text
    return {"Authorization": f"Bearer {created.json()['token']}"}


def _model_tool_calls(monkeypatch, *arguments: str) -> None:
    tcs = [{"id": f"c{i}", "type": "function",
            "function": {"name": "bash", "arguments": a}} for i, a in enumerate(arguments)]
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content=None, tool_calls=tcs)))


def _ask(client, headers):
    r = client.post("/v1/chat/completions", headers=headers, json={
        "messages": [{"role": "user", "content": "list the files"}], "tools": _TOOLS})
    assert r.status_code == 200, r.text
    return r.json()


def _returned_args(body) -> list[str]:
    return [tc["function"]["arguments"] for tc in body["choices"][0]["message"]["tool_calls"]]


def test_egress_weak_match_passes_strong_match_redacted(stub_client, monkeypatch):
    headers = _app(stub_client, "bar-egress")
    _model_tool_calls(monkeypatch, CMD_ARGS, PERSON_ARGS)
    body = _ask(stub_client, headers)
    cmd, person = _returned_args(body)
    assert cmd == CMD_ARGS                                   # byte-identical
    assert json.loads(person) == {"user": "REDACTED_PERSON_1"}
    p = body["policy"]
    assert p["client_output_mode"] == "redact"
    assert p["tool_args_min_score"] == 0.5                   # deployment default
    assert (p["tool_arg_redactions"], p["tool_arg_skipped"]) == (1, 1)
    assert p["tool_arg_skipped_types"] == ["US_DRIVER_LICENSE"]


def test_ingress_replayed_tool_call_weak_match_reaches_model_unchanged(stub_client, monkeypatch):
    headers = _app(stub_client, "bar-ingress")
    CALLS.clear()
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="done")))
    r = stub_client.post("/v1/chat/completions", headers=headers, json={
        "messages": [
            {"role": "user", "content": "list the files"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "bash", "arguments": CMD_ARGS}},
                {"id": "c2", "type": "function",
                 "function": {"name": "bash", "arguments": PERSON_ARGS}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
            {"role": "tool", "tool_call_id": "c2", "content": "ok"},
        ],
        "tools": _TOOLS,
    })
    assert r.status_code == 200, r.text
    sent = CALLS[-1]["messages"]
    assistant = next(m for m in sent if m.get("role") == "assistant")
    args = [tc["function"]["arguments"] for tc in assistant["tool_calls"]]
    assert args[0] == CMD_ARGS
    assert json.loads(args[1]) == {"user": "REDACTED_PERSON_1"}
    assert "John Smith" not in json.dumps(sent)
    p = r.json()["policy"]
    # Detection unchanged: the skipped type is still detected and classified.
    assert "US_DRIVER_LICENSE" in p["detected_entities"]
    assert (p["tool_arg_redactions"], p["tool_arg_skipped"]) == (1, 1)
    assert p["tool_arg_skipped_types"] == ["US_DRIVER_LICENSE"]


def test_message_content_weak_match_is_still_redacted(stub_client, monkeypatch):
    headers = _app(stub_client, "bar-content")
    CALLS.clear()
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="done")))
    r = stub_client.post("/v1/chat/completions", headers=headers, json={
        "messages": [{"role": "user", "content": "explain xargs -0 -n1 printf"}],
        "tools": _TOOLS})
    assert r.status_code == 200, r.text
    sent = json.dumps(CALLS[-1]["messages"])
    assert "-n1" not in sent and "<US_DRIVER_LICENSE_1>" in sent
    assert r.json()["policy"]["tool_arg_skipped"] == 0


def test_per_app_override_both_directions_and_version(stub_client, monkeypatch):
    headers = _app(stub_client, "bar-app")
    _model_tool_calls(monkeypatch, CMD_ARGS, PERSON_ARGS)
    inherit = _ask(stub_client, headers)
    assert inherit["policy"]["tool_args_min_score"] == 0.5
    v_inherit = inherit["policy"]["app_policy_version"]

    put = stub_client.put("/admin/applications/bar-app/policy", json={"tool_args_min_score": 0.2})
    assert put.status_code == 200, put.text
    assert put.json()["policy"]["tool_args_min_score"] == 0.2
    assert put.json()["effective"]["tool_args_min_score"] == 0.2
    low = _ask(stub_client, headers)
    cmd, _ = _returned_args(low)
    assert "-n1" not in cmd and "REDACTED_US_DRIVER_LICENSE_1" in cmd
    assert low["policy"]["tool_args_min_score"] == 0.2
    assert low["policy"]["tool_arg_skipped"] == 0
    v_low = low["policy"]["app_policy_version"]

    assert stub_client.put("/admin/applications/bar-app/policy",
                           json={"tool_args_min_score": 0.9}).status_code == 200
    high = _ask(stub_client, headers)
    cmd, person = _returned_args(high)
    assert (cmd, person) == (CMD_ARGS, PERSON_ARGS)
    assert high["policy"]["tool_arg_skipped_types"] == ["PERSON", "US_DRIVER_LICENSE"]
    v_high = high["policy"]["app_policy_version"]
    assert len({v_inherit, v_low, v_high}) == 3

    reset = stub_client.put("/admin/applications/bar-app/policy",
                            json={"reset": ["tool_args_min_score"]})
    assert reset.status_code == 200, reset.text
    assert reset.json()["policy"]["tool_args_min_score"] is None
    assert reset.json()["effective"]["tool_args_min_score"] == 0.5
    again = _ask(stub_client, headers)
    assert again["policy"]["tool_args_min_score"] == 0.5
    assert again["policy"]["app_policy_version"] == v_inherit


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_out_of_range_override_is_422(stub_client, bad):
    _app(stub_client, "bar-bad")
    r = stub_client.put("/admin/applications/bar-bad/policy", json={"tool_args_min_score": bad})
    assert r.status_code == 422
    assert "tool_args_min_score" in r.text


def test_effective_bar_includes_pii_score_threshold(stub_client):
    _app(stub_client, "bar-thr", pii_score_threshold=0.7)
    got = stub_client.get("/admin/applications/bar-thr")
    assert got.status_code == 200, got.text
    assert got.json()["effective"]["tool_args_min_score"] == 0.7


def test_outcome_row_carries_counts_and_no_values(stub_client, monkeypatch):
    headers = _app(stub_client, "bar-outcome")
    _model_tool_calls(monkeypatch, CMD_ARGS, PERSON_ARGS)
    _ask(stub_client, headers)
    r = stub_client.get("/admin/outcomes/export", params={"application_id": "bar-outcome"})
    assert r.status_code == 200, r.text
    rows = [json.loads(line)["outcome"] for line in r.text.splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert (row["tool_arg_redactions"], row["tool_arg_skipped"]) == (1, 1)
    assert row["tool_arg_skipped_types"] == ["US_DRIVER_LICENSE"]
    assert "John Smith" not in r.text and "xargs" not in r.text


# --- trace ------------------------------------------------------------------

@pytest.fixture
def mem_provider(monkeypatch):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    return mem


def test_trace_carries_bar_and_counts(mem_provider):
    export_mod.export_trace(
        request_id="req-b69", tenant_id="acme", model="local-8b",
        cost={"total_usd": 0.0, "baseline_usd": 0.0},
        policy={"app_policy_version": "abc", "tool_args_min_score": 0.5,
                "tool_arg_redactions": 1, "tool_arg_skipped": 2,
                "tool_arg_skipped_types": ["DATE_TIME", "US_DRIVER_LICENSE"]},
        trace=[])
    otel._provider.force_flush()
    root = next(s for s in mem_provider.get_finished_spans() if s.name == "serving.request")
    assert root.attributes["sp.tool_args_min_score"] == 0.5
    assert root.attributes["sp.tool_arg_redactions"] == 1
    assert root.attributes["sp.tool_arg_skipped"] == 2
    assert root.attributes["sp.tool_arg_skipped_types"] == "DATE_TIME,US_DRIVER_LICENSE"


# --- config -------------------------------------------------------------------

def test_missing_config_key_defaults_to_half(tmp_path):
    import shutil

    import yaml
    for name in ("serving.yaml", "routing.yaml", "policy.yaml", "pricing.yaml"):
        shutil.copy(f"config/{name}", tmp_path / name)
    raw = yaml.safe_load((tmp_path / "serving.yaml").read_text())
    raw["policy"].pop("tool_args_min_score", None)
    (tmp_path / "serving.yaml").write_text(yaml.safe_dump(raw))
    assert load_settings(str(tmp_path)).serving.tool_args_min_score == 0.5


@pytest.mark.parametrize("bad", [-0.01, 1.5])
def test_out_of_range_config_is_a_named_boot_error(bad):
    s = copy.deepcopy(load_settings())
    s.serving.tool_args_min_score = bad
    assert any("TOOL_ARGS_MIN_SCORE_OUT_OF_RANGE" in e for e in lint_settings(s))
    with pytest.raises((SystemExit, ConfigLintError)):
        enforce(s)


# --- the backlog's regression, on real Presidio -------------------------------

@pytest.mark.parametrize("command", ["xargs -0 -n1 printf '%q\\n'", "tail -n20 log",
                                     "head -c1500 f"])
def test_presidio_shell_flags_survive_an_enforced_app(monkeypatch, command):
    pytest.importorskip("presidio_analyzer")
    client = TestClient(create_app())
    headers = _app(client, "bar-presidio", pii_detection="presidio")
    args = json.dumps({"command": command})
    _model_tool_calls(monkeypatch, args)
    body = _ask(client, headers)
    assert _returned_args(body) == [args]
    assert body["policy"]["detection_mode"] == "presidio"
    assert body["policy"]["client_output_mode"] == "redact"
