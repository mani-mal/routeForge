"""B114: Presidio's email validation must never trigger a suffix-list fetch."""
from __future__ import annotations

import sys

import pytest

from app.policy.detectors import use_offline_tldextract


def test_noop_when_tldextract_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "tldextract", None)  # import -> ImportError
    assert use_offline_tldextract() is False


def test_extractor_has_no_remote_urls_and_no_cache():
    tldextract = pytest.importorskip("tldextract")
    impl = sys.modules["tldextract.tldextract"]
    original = impl.TLD_EXTRACTOR
    try:
        assert use_offline_tldextract() is True
        ex = impl.TLD_EXTRACTOR
        assert tuple(ex.suffix_list_urls) == ()
        assert ex._cache.enabled is False
        # extract() resolves TLD_EXTRACTOR at call time, so it uses the offline
        # extractor; a lookup must not touch the network.
        import requests

        def boom(*a, **k):
            raise AssertionError("network fetch attempted")

        orig = requests.Session.request
        requests.Session.request = boom
        try:
            assert tldextract.extract("user@example.co.uk").suffix == "co.uk"
        finally:
            requests.Session.request = orig
    finally:
        impl.TLD_EXTRACTOR = original
