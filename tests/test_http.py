from datetime import UTC, datetime

import httpx
import pytest

from goldmaking.tsm import DownloadError, FeedError, FeedSpec, TSMClient, retry_seconds


def test_retry_backoff_and_recovery(settings):
    responses = [
        httpx.Response(503),
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, content=b"csv"),
    ]
    waits = []
    with httpx.Client(transport=httpx.MockTransport(lambda _: responses.pop(0))) as client:
        result = TSMClient(settings, client, waits.append).fetch(
            FeedSpec(scope="region", transport="http")
        )
    assert result.content == b"csv"
    assert len(waits) == 2 and waits[1] >= 3


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404])
def test_permanent_errors_not_retried(status, settings):
    waits = []
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as client:
        with pytest.raises(DownloadError) as error:
            TSMClient(settings, client, waits.append).fetch(
                FeedSpec(scope="region", transport="http")
            )
    assert error.value.status == status
    assert waits == []


def test_long_retry_after_deferred(settings):
    waits = []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": "7200"})
        )
    ) as client:
        with pytest.raises(DownloadError) as error:
            TSMClient(settings, client, waits.append).fetch(
                FeedSpec(scope="region", transport="http")
            )
    assert error.value.retry_after == 7200 and waits == []
    assert retry_seconds("Thu, 01 Jan 2026 00:01:00 GMT", datetime(2026, 1, 1, tzinfo=UTC)) == 60


def test_network_timeout_bounded(settings):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DownloadError):
            TSMClient(settings, client, lambda _: None).fetch(
                FeedSpec(scope="region", transport="http")
            )
    assert count == settings.http_attempts


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "99999999"}])
def test_download_size_bounded(headers, settings):
    settings = settings.model_copy(update={"max_download_bytes": 1024})
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"x" * 1025, headers=headers)
        )
    ) as client:
        with pytest.raises(FeedError, match="volumineux"):
            TSMClient(settings, client).fetch(FeedSpec(scope="region", transport="http"))
