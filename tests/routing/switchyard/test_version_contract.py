import pytest

from app.routing import switchyard as sw


@pytest.mark.parametrize("v", ["0.4.0a1", "0.4.0a2", "0.4.0rc2", "0.4.0", "0.4.7"])
def test_supported(v):
    assert sw.is_supported(v)


@pytest.mark.parametrize("v", ["0.3.0", "0.4.0a0", "0.5.0", "0.5.0a1", "1.0.0", "junk", ""])
def test_unsupported(v):
    assert not sw.is_supported(v)


def test_installed_version_is_str_or_none():
    assert sw.installed_version() is None or isinstance(sw.installed_version(), str)
