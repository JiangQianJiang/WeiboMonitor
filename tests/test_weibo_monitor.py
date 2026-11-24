import asyncio

import aiohttp
import pytest

from monitor.weibo import WeiboMonitor


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        return None

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def api_payload(items):
    return {"data": {"list": items}}


@pytest.mark.asyncio
async def test_get_weibo_list_returns_complete_normalized_list():
    session = FakeSession(FakeResponse(api_payload([
        {
            "id": 200,
            "text_raw": "newest",
            "created_at": "time-2",
            "user": {"screen_name": "User"},
            "source": "Web",
            "region_name": "Beijing",
        },
        {
            "id": "100",
            "text_raw": "older",
            "created_at": "time-1",
            "user": {"screen_name": "User"},
            "source": "iPhone",
            "region_name": "Shanghai",
        },
    ])))

    result = await WeiboMonitor(session).get_weibo_list("uid")

    assert result == [
        {
            "id": "200",
            "text": "newest",
            "time": "time-2",
            "screen_name": "User",
            "source": "Web",
            "region_name": "Beijing",
        },
        {
            "id": "100",
            "text": "older",
            "time": "time-1",
            "screen_name": "User",
            "source": "iPhone",
            "region_name": "Shanghai",
        },
    ]
    assert session.requests[0][1]["timeout"] == 5


@pytest.mark.asyncio
async def test_get_weibo_list_returns_empty_list_for_empty_api_list():
    session = FakeSession(FakeResponse(api_payload([])))

    result = await WeiboMonitor(session).get_weibo_list("uid")

    assert result == []


@pytest.mark.asyncio
async def test_get_weibo_list_does_not_follow_redirects_with_cookie():
    session = FakeSession(FakeResponse(api_payload([{"id": 1, "text_raw": "ignored"}]), status=302))
    monitor = WeiboMonitor(session, cookie="session=synthetic")

    assert await monitor.get_weibo_list("uid") == []
    [(url, options)] = session.requests
    assert url.startswith("https://www.weibo.com/")
    assert options["headers"]["Cookie"] == "session=synthetic"
    assert options["allow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [aiohttp.ClientError("network"), asyncio.TimeoutError()])
async def test_get_weibo_list_returns_empty_list_for_request_failure(error):
    session = FakeSession(error=error)

    result = await WeiboMonitor(session).get_weibo_list("uid")

    assert result == []


@pytest.mark.asyncio
async def test_get_weibo_list_skips_malformed_sibling():
    session = FakeSession(FakeResponse(api_payload([
        {
            "id": 200,
            "text_raw": "valid",
            "created_at": "time-2",
            "user": {"screen_name": "User"},
            "source": "Web",
            "region_name": "Beijing",
        },
        {
            "id": 100,
            "created_at": "time-1",
            "user": {"screen_name": "User"},
            "source": "iPhone",
            "region_name": "Shanghai",
        },
    ])))

    result = await WeiboMonitor(session).get_weibo_list("uid")

    assert [item["id"] for item in result] == ["200"]
