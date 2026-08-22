"""Persistent memory substrate (Feature 5, Task 1) -- CCR-class residency
posture (CLAUDE.md rule 6): local-only, TTL-bounded, tenant-partitioned,
sanitized-only, never exported. See
docs/architecture/persistent-memory-residency-spec.md for the binding
guarantees (R1-R7).

This package builds the substrate only: `DeterministicMemoryStore`, the
`MemoryStore` seam, `MemoryCfg`, and the config lint guards. It is NOT wired
into the serving pipeline (Task 2 wires read/write in) and is default OFF
(`config/serving.yaml` memory.enabled: false) -- importing this package has no
effect on request handling.
"""
