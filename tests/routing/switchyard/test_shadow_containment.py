"""B104 containment property: in shadow mode, random PII-shaped content (SSNs,
emails, names) and a unique canary token, fed through message content AND
client tool results, never reach any trace attribute or tag, any outcome row,
the DecisionRecord `advisor` field, or any captured log line.

Uses the REAL Switchyard advisor (it is the component that reads the content),
the real route, the real exporter behind `RedactingSpanExporter`, and the
real outcome writer. Seeded, so a failure reproduces.
"""
from __future__ import annotations

import json
import logging
import random
import string

import pytest

from tests import _litellm_fake
from tests.test_admin_api import _login
from tests.routing.switchyard._shadow_harness import (
    DEMO, client, columns, install_fake, outcomes,
)

pytest.importorskip("switchyard")

_FIRST = ["Alice", "Bob", "Carmen", "Dmitri", "Esther", "Farouk", "Grace", "Hiro"]
_LAST = ["Nguyen", "Okafor", "Petrov", "Quinn", "Rossi", "Sato", "Tanaka", "Ueda"]
_TOOLS = [{"type": "function", "function": {
    "name": "crm_lookup", "description": "Look up a customer",
    "parameters": {"type": "object", "properties": {"q": {"type": "string"}},
                   "required": ["q"]}}}]


def _ssn(rng: random.Random) -> str:
    return f"{rng.randint(100, 665)}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}"


def _email(rng: random.Random) -> str:
    user = "".join(rng.choices(string.ascii_lowercase, k=rng.randint(4, 10)))
    return f"{user}@{rng.choice(['example.com', 'corp.test', 'mail.invalid'])}"


def _payload(rng: random.Random, canary: str) -> tuple[dict, list[str]]:
    name = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
    ssn, email = _ssn(rng), _email(rng)
    secrets = [canary, ssn, email]
    text = (f"{canary} Customer {name} (SSN {ssn}, {email}) asked about "
            f"invoice {rng.randint(1000, 9999)}. {canary}")
    if rng.random() < 0.5:
        return {"messages": [{"role": "user", "content": text}]}, secrets
    tool_result = json.dumps({"name": name, "ssn": ssn, "email": email,
                              "note": f"{canary} vip"})
    return {"messages": [
        {"role": "user", "content": f"Look up {canary}"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "crm_lookup", "arguments": json.dumps({"q": canary})}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": tool_result},
        {"role": "user", "content": text},
    ], "tools": _TOOLS}, secrets


def test_canary_never_reaches_switchyard_telemetry(monkeypatch, caplog):
    rng = random.Random(0xB104)
    install_fake(monkeypatch)
    _litellm_fake.CALLS.clear()
    runs = []
    with caplog.at_level(logging.DEBUG):
        with client(monkeypatch, "shadow") as c:
            for i in range(30):
                canary = f"CANARY{rng.getrandbits(64):016x}"
                req, secrets = _payload(rng, canary)
                r = c.post("/v1/chat/completions", headers=DEMO, json=req)
                assert r.status_code == 200, r.text
                runs.append((secrets, r.json()))
            _login(c)
            snaps = [c.get(f"/api/requests/{body['id']}").json() for _, body in runs]
            spans = c.mem.get_finished_spans()
            rows = [columns(o) for o in outcomes(c)]

    assert len(rows) == len(runs) == 30
    span_blob = repr([(s.name, dict(s.attributes)) for s in spans])
    rows_blob = repr(rows)
    log_blob = "\n".join(r.getMessage() for r in caplog.records)
    # The advisor record lives on the operator snapshot (never the client body).
    assert all("advisor" not in rec for _, body in runs for rec in body["trace"])
    advisor_blob = repr([[rec.get("advisor") for rec in snap["trace"]] for snap in snaps])
    # The content really flowed: each canary reached the model (and so the
    # advisor, which is handed the same post-gate messages).
    wire_blob = repr(_litellm_fake.CALLS)
    assert all(secrets[0] in wire_blob for secrets, _ in runs)
    # The advisor really ran on every request.
    assert advisor_blob.count("'mode': 'shadow'") == 30
    assert span_blob.count("'sp.switchyard.mode': 'shadow'") == 30
    for secrets, _ in runs:
        canary = secrets[0]
        assert canary not in span_blob, "canary in a trace attribute or tag"
        assert canary not in rows_blob, "canary in an outcome row"
        assert canary not in advisor_blob, "canary in the DecisionRecord advisor field"
        assert canary not in log_blob, "canary in a log line"
        for value in secrets[1:]:
            assert value not in advisor_blob
            assert value not in rows_blob
