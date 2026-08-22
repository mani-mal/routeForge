"""Substrate tests for persistent memory (Feature 5, Task 1). Mirrors the CCR
test pattern (tests/test_ccr_store.py): a fake monotonic clock drives TTL.
See docs/architecture/persistent-memory-residency-spec.md for the guarantees
these invariants enforce (R1-R7)."""
import pytest

from app.memory.store import DeterministicMemoryStore
from app.models import MemoryRecord


def test_tenant_isolation_structural():
    """R2: a recall for tenant B can NEVER return tenant A's memory."""
    s = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    s.write("tenant A sanitized summary", tenant_id="tenant-a")
    assert s.recall(tenant_id="tenant-b", max_items=5) == []
    assert s.recall(tenant_id="tenant-a", max_items=5) == ["tenant A sanitized summary"]


def test_partition_key_includes_tenant_id():
    """R2, structural: the partition key is derived from tenant_id, not a
    filter applied after a broader scan."""
    s = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    s.write("x", tenant_id="tenant-a", application_id="app1", session_id="sess1")
    assert s.size == 1
    assert all(key.startswith("tenant-a\x1f") for key in s._data)  # noqa: SLF001


def test_tenant_id_required():
    s = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    with pytest.raises(ValueError):
        s.write("x", tenant_id="")
    with pytest.raises(ValueError):
        s.recall(tenant_id="", max_items=5)


def test_ttl_swept_on_write():
    t = [0.0]
    s = DeterministicMemoryStore(ttl_seconds=10, clock=lambda: t[0])
    s.write("old", tenant_id="t1")
    assert s.size == 1
    t[0] = 11.0
    s.write("new", tenant_id="t1")  # sweep-on-write drops the expired "old" entry
    assert s.size == 1
    assert s.recall(tenant_id="t1", max_items=5) == ["new"]


def test_ttl_evicted_on_read():
    t = [0.0]
    s = DeterministicMemoryStore(ttl_seconds=10, clock=lambda: t[0])
    s.write("gone", tenant_id="t1")
    t[0] = 11.0
    assert s.recall(tenant_id="t1", max_items=5) == []  # evict-on-read
    assert s.size == 0


def test_retrievals_to_cloud_pinned_zero_on_normal_recall():
    """R3: the deterministic store is always local, so retrievals_to_cloud
    must stay 0 for a normal (to_cloud=False) recall."""
    s = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    s.write("x", tenant_id="t1")
    s.recall(tenant_id="t1", max_items=5, to_cloud=False)
    s.recall(tenant_id="t1", max_items=5, to_cloud=False)
    assert s.retrievals == 2
    assert s.retrievals_to_cloud == 0


def test_to_cloud_counter_still_tracked_for_honesty():
    # The caller must never pass to_cloud=True for onprem_only (R3) -- this
    # only proves the counter exists and works, mirroring CCRStore's parity.
    s = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    s.write("x", tenant_id="t1")
    s.recall(tenant_id="t1", max_items=5, to_cloud=True)
    assert s.retrievals == 1
    assert s.retrievals_to_cloud == 1


def test_recall_respects_max_recall_and_recency():
    t = [0.0]
    s = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: t[0])
    for i in range(5):
        t[0] = float(i)
        s.write(f"summary-{i}", tenant_id="t1")
    out = s.recall(tenant_id="t1", max_items=3)
    assert out == ["summary-4", "summary-3", "summary-2"]  # most-recent-first, capped


def test_recall_scoped_by_application_and_session():
    s = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    s.write("app1-sess1", tenant_id="t1", application_id="app1", session_id="sess1")
    s.write("app1-sess2", tenant_id="t1", application_id="app1", session_id="sess2")
    assert s.recall(tenant_id="t1", application_id="app1",
                     session_id="sess1", max_items=5) == ["app1-sess1"]
    assert s.recall(tenant_id="t1", application_id="app1",
                     session_id="sess2", max_items=5) == ["app1-sess2"]


def test_memory_record_is_counts_only():
    """R4: MemoryRecord must never carry a content field."""
    fields = set(MemoryRecord.model_fields)
    forbidden = {"text", "content", "summary", "memory", "recalled_text",
                 "recalled_items", "entries"}
    assert not (fields & forbidden)
    rec = MemoryRecord(recalled_count=2, tokens_recalled=40)
    assert rec.recall_stayed_local is True
    assert rec.retrievals_to_cloud == 0
