"""Canonical policy vocabularies — the single source for the sensitivity,
risk-class, and residency values used by the boot config-lint AND the
write-time per-app privacy-rule validation. Never spell these anywhere else."""
from __future__ import annotations

SENSITIVITY = frozenset({"public", "internal", "confidential", "restricted"})
RISK_CLASS = frozenset({"low", "medium", "high"})
RESIDENCY = frozenset({"any", "private_cloud_ok", "onprem_only"})

# Per-app override vocabularies (AppPolicy.pii_detection / AppPolicy.stream_mode).
PII_MODES = ("off", "regex", "presidio")
STREAM_MODES = ("off", "verified_then_stream", "stream_unverified", "incremental_verify")
# Protection posture (AppPolicy.policy_mode) — the axis `residency` used to
# conflate. `residency` says WHERE compute may run; this says how much the gate
# does to the data. "enforced" = today's privacy-first behaviour; "passthrough"
# = router + meter + tracer only (no detection, no redaction either direction).
# Placement is untouched by either value: onprem_only still pins routing.
POLICY_MODES = ("enforced", "passthrough")
