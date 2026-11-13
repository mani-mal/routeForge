"""B108: the client-visible `adaptive_routing: enforce` stamp is honest --
it appears only when an advisor exists AND the app's effective mode is enforce."""
from types import SimpleNamespace

from app.pipeline import Pipeline


def _stamp(advisor, mode):
    return Pipeline._adaptive_stamp(SimpleNamespace(_advisor=advisor),
                                    SimpleNamespace(adaptive_routing=mode))


def test_enforce_with_an_advisor_is_stamped():
    assert _stamp(object(), "enforce") == {"adaptive_routing": "enforce"}


def test_no_stamp_when_the_advisor_failed_to_construct():
    # Boot logged the construction failure and serves without the advisor:
    # nothing is being steered, so the client must not be told `enforce`.
    assert _stamp(None, "enforce") == {}


def test_off_and_shadow_never_stamped():
    assert _stamp(object(), "shadow") == {}
    assert _stamp(object(), "off") == {}
