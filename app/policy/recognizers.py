"""Customer-defined regex recognizers: validation (name, compile, ReDoS screen)
and a runtime matcher with a per-match timeout. Kept separate from the built-in
Presidio path so it works under both pii_detection: presidio and regex."""
from __future__ import annotations

import re as _re
import regex  # the `regex` module: supports per-match timeout=

from app.models import Entity

NAME_RE = _re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
MAX_PATTERN_LEN = 512
_MATCH_TIMEOUT_S = 0.1

# Presidio's common built-in entity types — reserved so a custom name can't
# shadow one and cause confusing double-detection.
PRESIDIO_BUILTINS = frozenset({
    "US_SSN", "CREDIT_CARD", "EMAIL_ADDRESS", "PHONE_NUMBER", "US_BANK_NUMBER",
    "PERSON", "LOCATION", "DATE_TIME", "IP_ADDRESS", "URL", "MEDICAL_LICENSE",
    "US_DRIVER_LICENSE", "US_PASSPORT", "IBAN_CODE", "NRP", "US_ITIN",
})

# Adversarial inputs used to smoke out catastrophic backtracking at write time.
#
# NOTE: the brief's original probes used a repeat count of 40 (e.g. "a" * 40).
# Verified empirically against the installed `regex` 2026.7.19: that engine
# resolves `(a+)+$` against a 40-char adversarial string in well under 1ms —
# its internal optimizations handle short catastrophic-backtracking cases far
# better than a naive backtracker. Probing at n=100..300 still resolved in
# <0.06s (still under our 0.1s timeout, i.e. would NOT have been rejected).
# Only at n>=400 did `(a+)+$` reliably exceed the 0.1s timeout on this
# machine/version. To stay robust across faster hardware and future `regex`
# versions with even better backtracking mitigations, the probe repeat count
# was strengthened to 2000 (5x the empirically-observed threshold). Verified
# this does not slow down validation of benign patterns: a benign pattern
# (e.g. `\bEMP-\d{6}\b`) resolves against all three 2000-char probes in
# ~10-20 microseconds each, since it simply fails to match the probe
# structure — the timeout budget is only ever spent on genuinely
# catastrophic patterns, which is exactly what we want to reject.
_REDOS_PROBES = ["a" * 2000 + "!", "EMP-" + "1" * 2000 + "x", "(" * 2000]


class RecognizerTimeout(Exception):
    pass


def validate_recognizer(name: str, pattern: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError(f"invalid recognizer name {name!r} (want ^[A-Z][A-Z0-9_]{{1,31}}$)")
    if name in PRESIDIO_BUILTINS:
        raise ValueError(f"{name!r} collides with a built-in entity type")
    if len(pattern) > MAX_PATTERN_LEN:
        raise ValueError("pattern too long")
    try:
        compiled = regex.compile(pattern)
    except regex.error as e:
        raise ValueError(f"regex does not compile: {e}") from e
    # ReDoS screen: run against adversarial inputs under a tight timeout.
    for probe in _REDOS_PROBES:
        try:
            compiled.search(probe, timeout=_MATCH_TIMEOUT_S)
        except TimeoutError as e:
            raise ValueError("pattern too slow (possible ReDoS)") from e


class CustomRecognizer:
    def __init__(self, name: str, pattern: str, score: float = 0.85) -> None:
        self.name = name
        self.score = score
        self._rx = regex.compile(pattern)

    def find(self, text: str) -> list[Entity]:
        out: list[Entity] = []
        try:
            for m in self._rx.finditer(text, timeout=_MATCH_TIMEOUT_S):
                out.append(Entity(type=self.name, start=m.start(), end=m.end(),
                                  value=m.group()))
        except TimeoutError as e:
            raise RecognizerTimeout(self.name) from e
        return out
