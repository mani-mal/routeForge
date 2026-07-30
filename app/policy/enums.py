"""Canonical policy vocabularies — the single source for the sensitivity,
risk-class, and residency values used by the boot config-lint AND the
write-time per-app privacy-rule validation. Never spell these anywhere else."""
from __future__ import annotations

SENSITIVITY = frozenset({"public", "internal", "confidential", "restricted"})
RISK_CLASS = frozenset({"low", "medium", "high"})
RESIDENCY = frozenset({"any", "private_cloud_ok", "onprem_only"})
