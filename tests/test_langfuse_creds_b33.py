"""B33: per-app Langfuse credentials -- read-back, partial update, verify on
save (from the API process), and removal. No test touches the network: the
outbound client is replaced with an httpx MockTransport."""
import logging

import httpx
import pytest

from app.api import admin_routes

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}
SECRET = "sk-lf-0123456789abcdef"
FULL = {"host": "http://lf:3000", "public_key": "pk-lf-1", "secret_key": SECRET,
        "project_id": "proj1"}


@pytest.fixture
def lf(client, monkeypatch):
    """Login, create an app, and route verification through a scripted handler."""
    assert client.post("/admin/login", json=_ADMIN).status_code == 200
    client.post("/admin/applications", json={"id": "b33", "name": "B33"})
    state = {"handler": lambda req: httpx.Response(200, json={"data": [{"id": "proj1"}]}),
             "calls": []}

    def handler(req):
        state["calls"].append(req)
        return state["handler"](req)

    monkeypatch.setattr(admin_routes, "_langfuse_http_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client.state = state
    return client


URL = "/admin/applications/b33/langfuse"


def test_save_verifies_and_get_shows_non_secret_fields(lf, caplog):
    caplog.set_level(logging.DEBUG)
    r = lf.put(URL, json=FULL)
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is True
    req = lf.state["calls"][0]
    assert str(req.url) == "http://lf:3000/api/public/projects"
    assert req.headers["authorization"].startswith("Basic ")
    g = lf.get(URL)
    assert g.status_code == 200
    assert g.json() == {"host": "http://lf:3000", "host_source": "app", "public_key": "pk-lf-1",
                        "project_id": "proj1", "secret_set": True,
                        "secret_last4": SECRET[-4:]}
    assert SECRET not in r.text and SECRET not in g.text
    assert SECRET not in caplog.text


def test_short_secret_gives_no_last4(lf):
    lf.put(URL, json={**FULL, "secret_key": "sk-short"})
    assert lf.get(URL).json()["secret_last4"] is None


def test_get_unset_and_unknown(lf):
    assert lf.get(URL).json()["secret_set"] is False
    assert lf.get("/admin/applications/nope/langfuse").status_code == 404


def test_partial_put_keeps_other_fields_and_secret(lf):
    lf.put(URL, json=FULL)
    lf.state["handler"] = lambda req: httpx.Response(200, json={"data": [{"id": "proj2"}]})
    r = lf.put(URL, json={"project_id": "proj2"})
    assert r.status_code == 200, r.text
    g = lf.get(URL).json()
    assert g["project_id"] == "proj2" and g["host"] == "http://lf:3000"
    assert g["public_key"] == "pk-lf-1" and g["secret_last4"] == SECRET[-4:]
    # the stored secret (not a blank) was what got verified
    import base64
    tok = lf.state["calls"][-1].headers["authorization"].split()[1]
    assert base64.b64decode(tok).decode() == f"pk-lf-1:{SECRET}"


def test_partial_put_without_stored_values_is_422(lf):
    r = lf.put(URL, json={"host": "http://lf:3000"})
    assert r.status_code == 422
    assert lf.state["calls"] == []


def test_whitespace_value_is_422(lf):
    lf.put(URL, json=FULL)
    for f in ("host", "public_key", "secret_key", "project_id"):
        assert lf.put(URL, json={f: "   "}).status_code == 422


@pytest.mark.parametrize("handler,needle", [
    (lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused")),
     "host http://lf:3000 is not reachable from the RouteForge API process"),
    (lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), "not reachable"),
    (lambda req: httpx.Response(401), "Langfuse rejected these keys"),
    (lambda req: httpx.Response(403), "Langfuse rejected these keys"),
    (lambda req: httpx.Response(200, json={"data": [{"id": "other"}]}),
     "these keys belong to project other, not proj1"),
    (lambda req: httpx.Response(500), "500"),
])
def test_verification_failures_422_and_store_nothing(lf, caplog, handler, needle):
    caplog.set_level(logging.DEBUG)
    lf.state["handler"] = handler
    r = lf.put(URL, json=FULL)
    assert r.status_code == 422
    assert needle in r.json()["detail"]
    assert SECRET not in r.text and SECRET not in caplog.text
    assert lf.get(URL).json()["secret_set"] is False


def test_verify_false_stores_without_checking(lf):
    lf.state["handler"] = lambda req: httpx.Response(500)
    r = lf.put(URL + "?verify=false", json=FULL)
    assert r.status_code == 200
    assert r.json()["verified"] is False
    assert lf.state["calls"] == []
    assert lf.get(URL).json()["secret_set"] is True


def test_delete_removes_credentials(lf):
    lf.put(URL, json=FULL)
    r = lf.delete(URL)
    assert r.status_code == 200, r.text
    assert r.json()["has_langfuse_keys"] is False
    assert lf.get(URL).json() == {"host": None, "host_source": "deployment", "public_key": None,
                                  "project_id": None, "secret_set": False, "secret_last4": None}
    assert lf.delete("/admin/applications/nope/langfuse").status_code == 404


def test_delete_refused_while_capture_io_full(lf):
    lf.put(URL, json=FULL)
    assert lf.put("/admin/applications/b33/policy",
                  json={"capture_io": "full"}).status_code == 200
    r = lf.delete(URL)
    assert r.status_code == 422
    assert "full" in r.json()["detail"]
    assert lf.get(URL).json()["secret_set"] is True
    lf.put("/admin/applications/b33/policy", json={"capture_io": "redacted"})
    assert lf.delete(URL).status_code == 200


def test_changed_host_or_public_key_without_secret_is_422_and_no_call(lf):
    lf.put(URL, json=FULL)
    n = len(lf.state["calls"])
    for body in ({"host": "http://attacker:9"}, {"host": "https://lf:3000"},
                 {"public_key": "pk-other"}):
        r = lf.put(URL, json=body)
        assert r.status_code == 422, body
        assert "re-entering the secret key" in r.json()["detail"]
    assert len(lf.state["calls"]) == n
    # same host spelled differently is the same origin; re-entering the secret is fine
    assert lf.put(URL, json={"host": "HTTP://LF:3000/"}).status_code == 200
    assert lf.put(URL, json={**FULL, "host": "http://other:3000"}).status_code == 200


def test_host_validation(lf):
    for h in ("http://u:p@lf:3000", "http://lf:notaport", "ftp://lf", "http://"):
        assert lf.put(URL, json={**FULL, "host": h}).status_code == 422, h
    assert lf.state["calls"] == []


def test_fields_are_stripped(lf):
    lf.put(URL, json={**FULL, "host": " http://lf:3000 ", "project_id": " proj1 "})
    g = lf.get(URL).json()
    assert g["host"] == "http://lf:3000" and g["project_id"] == "proj1"


def test_mismatch_message_caps_ids(lf):
    lf.state["handler"] = lambda req: httpx.Response(
        200, json={"data": [{"id": f"p{i}" + "x" * 100} for i in range(9)]})
    d = lf.put(URL, json=FULL).json()["detail"]
    assert "p4" in d and "p5" not in d and len(d) < 500


def test_redirect_is_not_followed(lf):
    lf.state["handler"] = lambda req: httpx.Response(302, headers={"location": "http://evil/"})
    assert lf.put(URL, json=FULL).status_code == 422
    assert len(lf.state["calls"]) == 1


def test_oversized_body_rejected(lf):
    lf.state["handler"] = lambda req: httpx.Response(200, content=b"x" * 70000)
    assert lf.put(URL, json=FULL).status_code == 422


def test_deployment_host_fallback_allows_partial_edit(lf, monkeypatch):
    # auto-provisioned shape: langfuse_host NULL, host comes from LANGFUSE_HOST
    monkeypatch.setenv("LANGFUSE_HOST", "http://lf:3000")
    lf.put(URL, json=FULL)
    import asyncio
    from app.apps import store
    async def _null_host():
        async with lf.app.state.db.session() as s:
            await store.update_application(s, "b33", langfuse_host=None)
            await s.commit()
    asyncio.run(_null_host())
    g = lf.get(URL).json()
    assert g["host"] == "http://lf:3000" and g["host_source"] == "deployment"
    assert lf.put(URL, json={"project_id": "proj1"}).status_code == 200
    # a different host still needs the secret re-entered
    assert lf.put(URL, json={"host": "http://attacker:9"}).status_code == 422
