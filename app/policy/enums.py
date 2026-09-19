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

# --- P1b endpoint data-handling declarations (design doc §2/§7) --------------
# `prompt_retention` and `accepts_raw_sensitive` are fixed enums (unlike
# `region`, which is customer vocabulary and deliberately has none — see
# app/config/lint.py). Values are matched lowercase-stripped, mirroring the
# read-time normalization `ModelRegistry._resolve_data_handling`/
# `app.policy.feasible._declared` already apply, so a value the lint accepts
# is a value those readers will actually resolve to a real declaration.
PROMPT_RETENTION_VALUES = frozenset({"none", "retained", "unknown"})
# The YAML declaration is a STRING token ("true" | "false" | "unknown"); the
# onboarding API/DB column is a JSON boolean. Both are legal inputs here.
RAW_SENSITIVE_VALUES = frozenset({"true", "false", "unknown"})
