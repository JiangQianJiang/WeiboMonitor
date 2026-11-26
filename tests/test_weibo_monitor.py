import asyncio
from unittest.mock import AsyncMock

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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["filtered", "partial", "duplicate", "missing_cursor", "limit"])
async def test_following_snapshot_requires_complete_unfiltered_pagination(failure):
    first = {
        "ok": 1, "users": [{"id": 10}], "total_number": 2, "next_cursor": 20,
        "has_filtered_attentions": False,
    }
    second = {
        "ok": 1, "users": [{"id": 20}], "total_number": 2, "next_cursor": 0,
        "has_filtered_attentions": False,
    }
    page_limit = 2
    if failure == "filtered":
        second["has_filtered_attentions"] = True
    elif failure == "partial":
        second["users"] = []
    elif failure == "duplicate":
        second["users"] = first["users"]
    elif failure == "missing_cursor":
        second.pop("next_cursor")
    else:
        page_limit = 1
    monitor = WeiboMonitor(None)
    monitor._get = AsyncMock(side_effect=[first, second])

    assert await monitor.get_following("123", page_limit) is None


@pytest.mark.asyncio
async def test_following_snapshot_accepts_the_verified_shape_and_terminal_cursor():
    monitor = WeiboMonitor(None)
    monitor._get = AsyncMock(side_effect=[
        {"ok": 1, "users": [{"id": 10}], "total_number": 2, "next_cursor": 20, "has_filtered_attentions": False},
        {"ok": 1, "users": [{"idstr": "20", "screen_name": "Followed"}], "total_number": 2,
         "next_cursor": 0, "has_filtered_attentions": False},
    ])

    assert await monitor.get_following("123", 2) == {
        "10": {"id": "10", "screen_name": ""}, "20": {"id": "20", "screen_name": "Followed"},
    }
    assert [entry.args[1]["page"] for entry in monitor._get.await_args_list] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "payload", "visibility"), [
    (200, {"ok": 0, "errno": "20101"}, "not_found"),
    (200, {"ok": 0, "errno": "20112"}, "restricted"),
    (200, {"ok": -100}, "unknown"),
    (302, {"ok": 0, "errno": "20101"}, "unknown"),
])
async def test_visibility_checks_omit_desktop_cookie(status, payload, visibility):
    session = FakeSession(FakeResponse(payload, status=status))
    client = WeiboMonitor(session, cookie="session=synthetic")

    assert await client.get_visibility("123") == visibility
    (url, options), = session.requests
    assert url == "https://m.weibo.cn/statuses/show?id=123"
    assert "Cookie" not in options["headers"]
    assert options["allow_redirects"] is False


async def test_incomplete_mobile_pin_page_cannot_confirm_unpinned():
    payload = {
        "ok": 1,
        "data": {"cards": [
            {"card_type": 9, "mblog": {"mblogtype": 2, "user": {"id": 123}}},
            {"card_type": 9, "mblog": {"id": 2, "user": {"id": 123}}},
        ]},
    }
    client = WeiboMonitor(FakeSession(FakeResponse(payload)))

    assert await client.get_mobile_pin("123") == (False, None)
