"""Direct tests for the onboarding probe's HTTP parsing (B50).

`probe_endpoint` is monkeypatched away in every other test file, so this is
the only place its real behaviour is pinned.
"""
import httpx
import pytest

from app.apps.probe import ProbeResult, parse_max_model_len, probe_endpoint


@pytest.mark.parametrize("value,expected", [
    (131072, 131072),
    (262144.0, 262144),
    ("131072", 131072),
    (None, None),
    (0, None),
    (-1, None),
    ("very long", None),
    (True, None),          # a bool is not a context window
    (1.5, None),           # a non-integral float is not a token count
])
def test_parse_max_model_len(value, expected) -> None:
    assert parse_max_model_len({"id": "m", "max_model_len": value}) == expected


def test_parse_missing_key_is_none() -> None:
    assert parse_max_model_len({"id": "m"}) is None


@pytest.fixture
def mock_upstream(monkeypatch):
    """Point `probe_endpoint`'s inline AsyncClient at a canned response."""
    def _install(payload: object, status: int = 200) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(status, json=payload))
        real = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: real(transport=transport, **kw))
    return _install


@pytest.mark.anyio
async def test_probe_captures_max_model_len(mock_upstream) -> None:
    payload = {"data": [{"id": "other", "max_model_len": 4096},
                        {"id": "qwen", "max_model_len": 262144}]}
    mock_upstream(payload)
    res = await probe_endpoint("http://example.com/v1", "qwen", None)
    assert isinstance(res, ProbeResult)
    assert res.ok is True
    assert res.max_model_len == 262144


@pytest.mark.anyio
async def test_probe_without_max_model_len_still_succeeds(mock_upstream) -> None:
    """Reachability and context capture are independent."""
    payload = {"data": [{"id": "qwen"}]}
    mock_upstream(payload)
    res = await probe_endpoint("http://example.com/v1", "qwen", None)
    assert res.ok is True and res.max_model_len is None


@pytest.mark.anyio
@pytest.mark.parametrize("bad", [None, 0, -1, "very long", {"nested": 1}])
async def test_unusable_max_model_len_does_not_fail_the_probe(mock_upstream, bad) -> None:
    """Parsing the limit can never turn a good probe into a failed one.

    The slug IS served, so reachability is established; only the context
    limit is unusable, and unknown is a legitimate answer (design §4.1).
    """
    payload = {"data": [{"id": "qwen", "max_model_len": bad}]}
    mock_upstream(payload)
    res = await probe_endpoint("http://example.com/v1", "qwen", None)
    assert res.ok is True
    assert res.max_model_len is None


@pytest.mark.anyio
async def test_a_payload_that_hides_the_slug_is_still_a_failure(mock_upstream) -> None:
    """Regression guard, NOT a new behaviour: a body RouteForge cannot read
    the served ids out of means it cannot confirm the model is served, which
    is the condition this probe exists to detect. It must keep failing."""
    payload = {"data": "not-a-list"}
    mock_upstream(payload)
    res = await probe_endpoint("http://example.com/v1", "qwen", None)
    assert res.ok is False
    assert res.max_model_len is None


@pytest.mark.anyio
async def test_probe_failure_reports_not_ok(mock_upstream) -> None:
    mock_upstream({"data": []}, status=500)
    res = await probe_endpoint("http://example.com/v1", "qwen", None)
    assert res.ok is False and res.max_model_len is None


@pytest.mark.anyio
async def test_probe_sends_authorization_header_when_env_is_set(monkeypatch) -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"data": [{"id": "qwen"}]})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real(transport=transport, **kw))
    monkeypatch.setenv("MY_FAKE_API_KEY_ENV", "fake-token-for-tests")

    res = await probe_endpoint("http://example.com/v1", "qwen", "MY_FAKE_API_KEY_ENV")
    assert res.ok is True
    assert seen_headers.get("authorization") == "Bearer fake-token-for-tests"


@pytest.mark.anyio
async def test_probe_sends_no_authorization_header_when_env_is_unset(monkeypatch) -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"data": [{"id": "qwen"}]})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real(transport=transport, **kw))
    monkeypatch.delenv("MY_FAKE_API_KEY_ENV", raising=False)

    res = await probe_endpoint("http://example.com/v1", "qwen", "MY_FAKE_API_KEY_ENV")
    assert res.ok is True
    assert "authorization" not in seen_headers
