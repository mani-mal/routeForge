"""Per-app privacy profile: the resolved recognizers + classification rules the
gate uses for one application. Empty rules => fall back to the global policy.yaml
sensitivity_rules. See docs/superpowers/specs/2026-07-30-per-app-privacy-rules-design.md."""
from __future__ import annotations

from dataclasses import dataclass

from app.config.schema import Settings
from app.policy.recognizers import CustomRecognizer


@dataclass(frozen=True)
class SensitivityRuleView:
    entities: frozenset[str]
    sensitivity: str
    risk_class: str
    residency: str
    require_verification: bool


@dataclass(frozen=True)
class AppPrivacyProfile:
    recognizers: tuple[CustomRecognizer, ...]
    rules: tuple[SensitivityRuleView, ...]


_PUBLIC_CATCH_ALL = SensitivityRuleView(
    entities=frozenset(), sensitivity="public", risk_class="low",
    residency="any", require_verification=False)


def effective_rules(profile: AppPrivacyProfile, settings: Settings) -> list[SensitivityRuleView]:
    """The app's own rules if it has any, else the global sensitivity_rules —
    always terminated by a public/low/any catch-all so classification never
    falls off the end of the list."""
    if profile.rules:
        rules = list(profile.rules)
    else:
        rules = [SensitivityRuleView(
            entities=frozenset(r.entities), sensitivity=r.sensitivity,
            risk_class=r.risk_class, residency=r.residency,
            require_verification=r.require_verification)
            for r in settings.policy.sensitivity_rules]
    if not rules or rules[-1].entities != frozenset():
        rules.append(_PUBLIC_CATCH_ALL)
    return rules


class ProfileCache:
    """Single-node in-memory cache, cleared on admin writes (like RateLimiter)."""

    def __init__(self) -> None:
        self._d: dict[str, AppPrivacyProfile] = {}

    def get(self, app_id: str) -> AppPrivacyProfile | None:
        return self._d.get(app_id)

    def put(self, app_id: str, profile: AppPrivacyProfile) -> None:
        self._d[app_id] = profile

    def clear(self, app_id: str) -> None:
        self._d.pop(app_id, None)
