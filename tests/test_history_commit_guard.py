"""F1 regression: record_history succeeding but the enclosing session.commit()
raising must NOT publish SSE events for a request that rolled back and never
made it into the durable request_history table.

Before the fix, `summary` (the outer variable gating the bus-publish block in
app/api/routes.py) was assigned straight from `history_store.record_history(...)`
before `await session.commit()`. If commit() then raised, the whole block's
except swallowed it (usage + history both roll back) but `summary` was already
non-None, so the bus still published "request"/"step" events for a request
that is not actually in the DB. The fix defers the outer-variable assignment
until after a successful commit.
"""
import app.apps.history_store as history_store


def test_commit_failure_after_record_history_skips_bus_publish(client, monkeypatch):
    published = []

    async def _spy_publish(event):
        published.append(event)

    monkeypatch.setattr(client.app.state.bus, "publish", _spy_publish)

    real_record_history = history_store.record_history

    async def _record_history_then_break_commit(session, *args, **kwargs):
        # Let the real (redaction-safe) history row get built and staged on
        # the session exactly like production, then sabotage this session's
        # commit -- simulating a DB hiccup between the successful history
        # write and the commit that would make it durable.
        result = await real_record_history(session, *args, **kwargs)

        async def _raise_commit():
            raise RuntimeError("simulated commit failure")

        session.commit = _raise_commit
        return result

    monkeypatch.setattr(
        history_store, "record_history", _record_history_then_break_commit)

    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer demo-key"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    # Guarded: usage/history recording must never break serving, even when
    # its own commit blows up.
    assert r.status_code == 200
    request_id = r.json()["id"]

    # The whole point of the fix: no SSE "request"/"step" events fired for a
    # request whose durable row never committed.
    assert published == []

    # Corroborate the row really did roll back (belt-and-suspenders on the
    # "not in the durable table" half of the finding) -- need a fresh,
    # unpatched session/commit to log in and read it back.
    login = client.post("/admin/login",
                         json={"email": "admin@routeforge.local",
                               "password": "changeme"})
    assert login.status_code == 200
    listing = client.get("/api/requests")
    assert listing.status_code == 200
    assert all(row["id"] != request_id for row in listing.json())
