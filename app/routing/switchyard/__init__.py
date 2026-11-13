"""RouteForge's single adapter package for NVIDIA NeMo Switchyard.

Separation rule (B100). Switchyard (PyPI ``nemo-switchyard``, import name
``switchyard``) is an optional, unmodified, exact-pinned upstream dependency:

* RouteForge does not vendor, fork or monkey-patch it.
* A needed behaviour change is proposed upstream; until it ships, RouteForge
  adapts inside this package.
* This package is the ONLY place in the repo allowed to ``import switchyard``
  (or ``switchyard_rust``). ``tests/routing/switchyard/test_import_guard.py``
  fails the build if any other module does. An upstream release therefore
  changes this package and its contract tests, nothing else.

B100 added the version contract below. B103 added the adapter
(`advisor.py`, `convert.py`, `evidence.py`): `SwitchyardAdvisor` imports
Switchyard lazily, only when constructed, so this package imports cleanly
without the extra. Nothing calls it at request time yet; the shadow and
enforce modes arrive with B105/B108 and the contract suite with B110.

See docs/reference/switchyard-integration.md,
docs/backlog/bugs.md (B100, B110) and
docs/research/RouteForge-Switchyard-Feature-Comparison-and-Integration-Recommendation.md.
"""

import re
from importlib import metadata

DISTRIBUTION = "nemo-switchyard"

# Human-readable supported range. Keep in step with the exact pin in
# pyproject.toml's `switchyard` extra and `_LOWER`/`_UPPER` below.
SUPPORTED = ">=0.4.0a1,<0.5"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
# Pre-release phases sort below the final release (rank 3).
_PHASE_RANK = {"a": 0, "b": 1, "rc": 2}
_Key = tuple[int, int, int, int, int]

_LOWER: _Key = (0, 4, 0, 0, 1)  # 0.4.0a1, inclusive
_UPPER: _Key = (0, 5, 0, 0, 0)  # 0.5.0a0, exclusive (excludes 0.5 pre-releases)


def _key(version: str) -> _Key | None:
    m = _VERSION_RE.match(version.strip())
    if m is None:
        return None
    major, minor, patch, phase, num = m.groups()
    if phase is None:
        return (int(major), int(minor), int(patch), 3, 0)
    return (int(major), int(minor), int(patch), _PHASE_RANK[phase], int(num))


def installed_version() -> str | None:
    """Installed ``nemo-switchyard`` version, or None. Never imports it."""
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None


def is_supported(version: str) -> bool:
    """True if ``version`` is inside SUPPORTED; unparseable versions are not."""
    key = _key(version)
    return key is not None and _LOWER <= key < _UPPER


# Re-exported last: advisor.py imports `installed_version` from this module.
from .advisor import (  # noqa: E402
    ROLE_MODELS,
    AdvisorResult,
    SwitchyardAdvisor,
    SwitchyardProposal,
    SwitchyardUnavailable,
)

__all__ = [
    "DISTRIBUTION",
    "ROLE_MODELS",
    "SUPPORTED",
    "AdvisorResult",
    "SwitchyardAdvisor",
    "SwitchyardProposal",
    "SwitchyardUnavailable",
    "installed_version",
    "is_supported",
]
