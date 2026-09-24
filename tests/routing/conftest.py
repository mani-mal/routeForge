import pytest

from app.routing.quality import QualityScore


@pytest.fixture
def quality_resolver_factory():
    """A minimal QualityResolver stand-in: a dict of model -> score, defaulting
    to the historical tier_default floor for anything unnamed."""
    class _Fake:
        def __init__(self, scores: dict[str, float]) -> None:
            self._s = scores

        def resolve(self, name: str, task_class: str | None = None) -> QualityScore:
            if name in self._s:
                return QualityScore(score=self._s[name], source="published")
            return QualityScore(score=0.5, source="tier_default")

    return _Fake
