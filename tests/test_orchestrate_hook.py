"""External-training orchestration hook tests (F4 Task 2): tools/orchestrate/*.

Covers the 7 invariants from the task brief:
1. offline isolation (rule 4): serving graph never imports tools.orchestrate
2. no auto-onboard / no in-process promotion (rule 4, load-bearing): a
   succeeded job is SURFACED (printed instructions) but onboard_model /
   store.upsert_onboarded are never called and no model enters the registry
3. hosts no compute: the client only POSTs a spec + GETs status -- no loop,
   no streaming
4. trigger submits the documented TrainingJobSpec contract
5. env-only secrets: URL+token from env-var NAMES, friendly error when
   missing, token never persisted to the job-log
6. job-log durability: upsert/load round-trip, state transitions, path guards
7. residency: dataset_ref is passed as a REFERENCE only, never resolved/read

Uses an injected fake httpx-shaped client (records requests, returns canned
responses) -- no real network. All job-log paths are tmp_path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.orchestrate import jobs as jobs_module
from tools.orchestrate.client import (
    OrchestratorError, TrainingJobSpec, TrainingJobStatus, TrainingOrchestratorClient,
    resolve_credentials,
)
from tools.orchestrate.jobs import (
    JobsPathError, TrainingJobRecord, load_jobs, safe_jobs_path, upsert_job,
)
from tools.orchestrate.status import main as status_main
from tools.orchestrate.status import onboarding_instructions
from tools.orchestrate.trigger import main as trigger_main

REPO_ROOT = Path(__file__).resolve().parents[1]

FAKE_TOKEN = "super-secret-orchestrator-token"


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict):
        self.status_code = status_code
        self._json = json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._json


class FakeAsyncClient:
    """Records every call it receives; never touches the network."""

    def __init__(self, post_response: FakeResponse | None = None,
                 get_responses: dict[str, FakeResponse] | None = None):
        self.post_response = post_response
        self.get_responses = get_responses or {}
        self.requests: list[dict] = []

    async def post(self, url, *, headers=None, json=None):
        self.requests.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return self.post_response

    async def get(self, url, *, headers=None):
        self.requests.append({"method": "GET", "url": url, "headers": headers})
        ref = url.rsplit("/", 1)[-1]
        return self.get_responses[ref]

    async def aclose(self):  # pragma: no cover - never called for an injected client
        raise AssertionError("aclose() must never be called on an injected client")


def _set_env(monkeypatch, url="https://fake-orchestrator.example", token=FAKE_TOKEN):
    monkeypatch.setenv("TEST_ORCH_URL", url)
    monkeypatch.setenv("TEST_ORCH_TOKEN", token)


# --- 1. offline isolation -----------------------------------------------------

def test_serving_graph_does_not_import_tools_orchestrate():
    """Nothing under app/ imports tools.orchestrate -- importing the whole
    server module graph must never pull it in (rule 4 / offline isolation)."""
    env = os.environ.copy()
    env.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    result = subprocess.run(
        [sys.executable, "-c",
         "import app.main, sys; "
         "assert 'tools.orchestrate' not in sys.modules, "
         "'tools.orchestrate leaked into the serving import graph'"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


# --- 2. no auto-onboard / no in-process promotion (load-bearing) -------------

def test_succeeded_job_is_surfaced_never_auto_onboarded(tmp_path, monkeypatch, capsys):
    _set_env(monkeypatch)
    jobs_file = tmp_path / "training-jobs.json"

    fake_trigger_client = FakeAsyncClient(
        post_response=FakeResponse(200, {"job_id": "ext-job-1"}))
    rc = trigger_main(
        ["--base-model", "meta-llama/base-ref", "--method", "distill",
         "--dataset-ref", "s3://residency-local/exports/ds-42",
         "--jobs-file", str(jobs_file), "--config-dir", str(tmp_path / "config"),
         "--endpoint-env", "TEST_ORCH_URL", "--token-env", "TEST_ORCH_TOKEN"],
        client=fake_trigger_client)
    assert rc == 0
    capsys.readouterr()

    fake_status_client = FakeAsyncClient(get_responses={
        "ext-job-1": FakeResponse(200, {
            "state": "succeeded",
            "result_endpoint": "https://onprem.example/v1",
            "result_slug": "distilled-mid-v1",
        }),
    })

    with patch("app.api.admin_routes.onboard_model") as onboard_spy, \
         patch("app.apps.store.upsert_onboarded") as upsert_spy:
        rc = status_main(
            ["--jobs-file", str(jobs_file), "--config-dir", str(tmp_path / "config"),
             "--endpoint-env", "TEST_ORCH_URL", "--token-env", "TEST_ORCH_TOKEN"],
            client=fake_status_client)
        assert rc == 0
        assert onboard_spy.call_count == 0
        assert upsert_spy.call_count == 0

    out = capsys.readouterr().out
    assert "ext-job-1: succeeded" in out
    assert "Onboard the trained model" in out
    assert "RouteForge does NOT do this automatically" in out
    assert "POST /admin/infra/models" in out
    assert "distilled-mid-v1" in out

    # The job-log itself records the surfaced result, but nothing about an
    # onboarded row -- onboarding stays a separate, operator-initiated path.
    records = load_jobs(jobs_file, config_dir=str(tmp_path / "config"))
    assert len(records) == 1
    assert records[0].state == "succeeded"
    assert records[0].result_slug == "distilled-mid-v1"


def test_onboarding_instructions_never_call_an_onboarding_function():
    """Static trace: `onboarding_instructions` only formats text -- assert it
    has no reference to app.api.admin_routes or app.apps.store at all."""
    import inspect

    src = inspect.getsource(onboarding_instructions)
    assert "onboard_model" not in src
    assert "upsert_onboarded" not in src
    assert "import" not in src  # no local imports of an onboarding module either


# --- 3. hosts no compute -------------------------------------------------------

def test_client_only_posts_a_spec_and_gets_status_no_loop_no_stream():
    import asyncio

    fake = FakeAsyncClient(
        post_response=FakeResponse(200, {"job_id": "ext-1"}),
        get_responses={"ext-1": FakeResponse(200, {"state": "running"})})
    client = TrainingOrchestratorClient("https://orch.example", "tok", client=fake)

    spec = TrainingJobSpec(base_model_ref="ref", method="distill", dataset_ref="ds-ref")
    ref = asyncio.run(client.submit_job(spec))
    status = asyncio.run(client.get_status(ref))

    assert status.state == "running"
    # Exactly one POST, one GET -- no polling loop, no streaming, no retries.
    assert len(fake.requests) == 2
    assert fake.requests[0]["method"] == "POST"
    assert fake.requests[1]["method"] == "GET"

    # No training-compute vocabulary in the client's actual code (imports and
    # def lines) -- prose in docstrings/comments is exempt so this stays a
    # code check, not a word-ban on the module's own explanation of itself.
    import inspect

    from tools.orchestrate import client as client_module
    code_lines = [
        line for line in inspect.getsource(client_module).splitlines()
        if (stripped := line.strip())
        and not stripped.startswith("#")
        and not stripped.startswith('"')
        and not stripped.startswith("'")
    ]
    code_only = "\n".join(code_lines).lower()
    for banned in ("torch", "train_step", "forward(", "backward(", ".stream("):
        assert banned not in code_only


# --- 4. trigger submits the documented spec ------------------------------------

def test_trigger_submits_documented_spec_contract(tmp_path, monkeypatch):
    _set_env(monkeypatch)
    jobs_file = tmp_path / "training-jobs.json"
    fake = FakeAsyncClient(post_response=FakeResponse(200, {"job_id": "ext-9"}))

    rc = trigger_main(
        ["--base-model", "org/base-model-ref", "--method", "finetune",
         "--dataset-ref", "ref://residency-local/ds-7",
         "--hyperparam", "epochs=3", "--hyperparam", "lr=1e-5",
         "--notes", "quarterly distill",
         "--jobs-file", str(jobs_file), "--config-dir", str(tmp_path / "config"),
         "--endpoint-env", "TEST_ORCH_URL", "--token-env", "TEST_ORCH_TOKEN"],
        client=fake)
    assert rc == 0

    assert len(fake.requests) == 1
    posted = fake.requests[0]
    assert posted["url"] == "https://fake-orchestrator.example/jobs"
    body = posted["json"]
    assert body == {
        "base_model_ref": "org/base-model-ref",
        "method": "finetune",
        "dataset_ref": "ref://residency-local/ds-7",
        "hyperparams": {"epochs": "3", "lr": "1e-5"},
        "notes": "quarterly distill",
    }
    assert posted["headers"] == {"Authorization": f"Bearer {FAKE_TOKEN}"}


# --- 5. env-only secrets --------------------------------------------------------

def test_resolve_credentials_missing_endpoint_env_raises_friendly_error(monkeypatch):
    monkeypatch.delenv("TEST_ORCH_URL", raising=False)
    monkeypatch.delenv("TEST_ORCH_TOKEN", raising=False)
    with pytest.raises(OrchestratorError, match=r"\$TEST_ORCH_URL"):
        resolve_credentials("TEST_ORCH_URL", "TEST_ORCH_TOKEN")


def test_resolve_credentials_missing_token_env_raises_friendly_error(monkeypatch):
    monkeypatch.setenv("TEST_ORCH_URL", "https://orch.example")
    monkeypatch.delenv("TEST_ORCH_TOKEN", raising=False)
    with pytest.raises(OrchestratorError, match=r"\$TEST_ORCH_TOKEN"):
        resolve_credentials("TEST_ORCH_URL", "TEST_ORCH_TOKEN")


def test_trigger_cli_missing_env_fails_friendly_exit_1(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TEST_ORCH_URL", raising=False)
    monkeypatch.delenv("TEST_ORCH_TOKEN", raising=False)
    rc = trigger_main(
        ["--base-model", "ref", "--method", "distill", "--dataset-ref", "ds",
         "--jobs-file", str(tmp_path / "jobs.json"), "--config-dir", str(tmp_path / "config"),
         "--endpoint-env", "TEST_ORCH_URL", "--token-env", "TEST_ORCH_TOKEN"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "TEST_ORCH_URL" in err
    assert not (tmp_path / "jobs.json").exists()


def test_token_never_appears_in_the_job_log_file(tmp_path, monkeypatch):
    _set_env(monkeypatch)
    jobs_file = tmp_path / "training-jobs.json"
    fake = FakeAsyncClient(post_response=FakeResponse(200, {"job_id": "ext-tok"}))

    rc = trigger_main(
        ["--base-model", "ref", "--method", "distill", "--dataset-ref", "ds-ref",
         "--jobs-file", str(jobs_file), "--config-dir", str(tmp_path / "config"),
         "--endpoint-env", "TEST_ORCH_URL", "--token-env", "TEST_ORCH_TOKEN"],
        client=fake)
    assert rc == 0
    assert jobs_file.exists()
    contents = jobs_file.read_text()
    assert FAKE_TOKEN not in contents


# --- 6. job-log durability -------------------------------------------------------

def test_job_log_round_trips_and_transitions_state(tmp_path):
    jobs_file = tmp_path / "jobs.json"
    config_dir = tmp_path / "config"

    assert load_jobs(jobs_file, config_dir=config_dir) == []

    record = TrainingJobRecord(
        job_id="ext-1", external_ref="ext-1", state="submitted",
        base_model_ref="ref", method="distill", dataset_ref="ds-ref")
    upsert_job(jobs_file, record, config_dir=config_dir)

    loaded = load_jobs(jobs_file, config_dir=config_dir)
    assert len(loaded) == 1
    assert loaded[0].state == "submitted"

    updated = record.model_copy(update={
        "state": "succeeded", "result_endpoint": "https://onprem.example/v1",
        "result_slug": "slug-1"})
    upsert_job(jobs_file, updated, config_dir=config_dir)

    loaded_again = load_jobs(jobs_file, config_dir=config_dir)
    assert len(loaded_again) == 1  # upsert, not append
    assert loaded_again[0].state == "succeeded"
    assert loaded_again[0].result_endpoint == "https://onprem.example/v1"
    assert loaded_again[0].result_slug == "slug-1"


def test_job_log_write_is_atomic_and_survives_a_mid_write_crash(tmp_path, monkeypatch):
    """upsert_job must never truncate-then-write: simulate a crash between
    the temp-file write and the `os.replace` swap (the exact window
    `Path.write_text` doesn't have, because it truncates the real target
    first) and assert the existing valid job-log is left completely intact
    -- never a truncated/invalid partial write."""
    jobs_file = tmp_path / "jobs.json"
    config_dir = tmp_path / "config"

    first = TrainingJobRecord(
        job_id="ext-1", external_ref="ext-1", state="submitted",
        base_model_ref="ref", method="distill", dataset_ref="ds-ref")
    upsert_job(jobs_file, first, config_dir=config_dir)
    good_contents = jobs_file.read_text()
    assert json.loads(good_contents)  # sanity: valid JSON before the crash

    def boom(*args, **kwargs):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(jobs_module.os, "replace", boom)

    second = TrainingJobRecord(
        job_id="ext-2", external_ref="ext-2", state="submitted",
        base_model_ref="ref", method="distill", dataset_ref="ds-ref")
    with pytest.raises(OSError):
        upsert_job(jobs_file, second, config_dir=config_dir)

    # The target must still hold the last GOOD write, byte-for-byte -- not
    # truncated, not partially overwritten -- and no leftover temp files.
    assert jobs_file.read_text() == good_contents
    assert json.loads(jobs_file.read_text()) == json.loads(good_contents)
    leftover_tmp = list(tmp_path.glob("*.tmp"))
    assert leftover_tmp == [], f"orphaned temp file(s) left behind: {leftover_tmp}"


def test_job_log_path_guard_rejects_traversal():
    with pytest.raises(JobsPathError):
        safe_jobs_path("../jobs.json", config_dir="config")


def test_job_log_path_guard_rejects_writing_under_config(tmp_path):
    with pytest.raises(JobsPathError):
        safe_jobs_path(tmp_path / "config" / "sneaky.json", config_dir=tmp_path / "config")


# --- 7. residency: dataset_ref is a reference only, never resolved/read --------

def test_dataset_ref_is_passed_as_a_bare_reference_never_content(tmp_path, monkeypatch):
    """The submit payload carries only the ref string -- assert the client
    module never opens/reads a file and never imports/calls into Langfuse."""
    _set_env(monkeypatch)
    jobs_file = tmp_path / "jobs.json"
    fake = FakeAsyncClient(post_response=FakeResponse(200, {"job_id": "ext-res"}))

    nonexistent_ref = str(tmp_path / "this-file-does-not-exist.parquet")
    rc = trigger_main(
        ["--base-model", "ref", "--method", "distill",
         "--dataset-ref", nonexistent_ref,
         "--jobs-file", str(jobs_file), "--config-dir", str(tmp_path / "config"),
         "--endpoint-env", "TEST_ORCH_URL", "--token-env", "TEST_ORCH_TOKEN"],
        client=fake)
    # Never attempts to open/read the ref -- if it did, this would fail
    # (the path doesn't exist) instead of succeeding.
    assert rc == 0
    assert fake.requests[0]["json"]["dataset_ref"] == nonexistent_ref

    # No import of a Langfuse client anywhere in the trigger/client modules --
    # an actual `import` line, not just the word appearing in a docstring.
    import inspect

    from tools.orchestrate import client as client_module
    from tools.orchestrate import trigger as trigger_module
    for mod in (client_module, trigger_module):
        for line in inspect.getsource(mod).splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                assert "langfuse" not in stripped.lower()
