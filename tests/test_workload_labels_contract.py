"""Dotted `<pack>.<phase>` workload labels, through the real HTTP boundary.

Contract: a client may label a call with `x-routeforge-workload`. A label
matching `^[a-z0-9][a-z0-9._-]{0,63}$` after lower-casing -- dots included, so
a workflow-phase label like `issue-resolver.implement` -- is stored as sent
(lower-cased) with `workload_source: "client"`. Anything else is dropped to
`workload: null, workload_source: "invalid"`; the request is still served 200,
never rejected. Asserted on the outcome row and on the admin export line.
"""
from __future__ import annotations

import json

import pytest

from tests.test_admin_api import _login
from tests.test_outcome_hooks import DEMO, HELLO, _client, _only

VALID = [
    ("issue-resolver.implement", "issue-resolver.implement"),
    ("readiness.assess", "readiness.assess"),
    ("ci-repair.validate", "ci-repair.validate"),
    ("Issue-Resolver.Implement", "issue-resolver.implement"),
    ("a" * 64, "a" * 64),
]

INVALID = [
    "a" * 65,
    "has space",
    ".leading-dot",
    "-leading-dash",
]


def _export_lines(c) -> list[dict]:
    _login(c)
    out = c.get("/admin/outcomes/export")
    assert out.status_code == 200, out.text
    return [json.loads(x) for x in out.text.strip().splitlines()]


@pytest.mark.parametrize("sent,stored", VALID)
def test_valid_label_is_stored_lowercased(sent, stored):
    with _client() as c:
        r = c.post("/v1/chat/completions", headers={**DEMO, "x-routeforge-workload": sent},
                   json=HELLO)
        assert r.status_code == 200, r.text
        row = _only(c)
        assert (row.workload, row.workload_source) == (stored, "client")
        (line,) = _export_lines(c)
        assert line["outcome"]["workload"] == stored
        assert line["outcome"]["workload_source"] == "client"


@pytest.mark.parametrize("sent", INVALID)
def test_invalid_label_is_dropped_and_the_request_is_served(sent):
    with _client() as c:
        r = c.post("/v1/chat/completions", headers={**DEMO, "x-routeforge-workload": sent},
                   json=HELLO)
        assert r.status_code == 200, (sent, r.text)
        row = _only(c)
        assert (row.workload, row.workload_source) == (None, "invalid")
        (line,) = _export_lines(c)
        assert line["outcome"]["workload"] is None
        assert line["outcome"]["workload_source"] == "invalid"


def test_dotted_label_via_metadata_is_stored_too():
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO,
                   json={**HELLO, "metadata": {"workload": "Readiness.Assess"}})
        assert r.status_code == 200, r.text
        row = _only(c)
        assert (row.workload, row.workload_source) == ("readiness.assess", "client")
