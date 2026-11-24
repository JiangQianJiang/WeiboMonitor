from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.app import App
from notifer.notifer import Notifer, ServerChanResponseError
from state import repository
from state.repository import StateRepository


@pytest.fixture
def serverchan_response():
    response = SimpleNamespace(status=200, json=AsyncMock(return_value={"code": 0}))
    session = MagicMock()
    session.post.return_value.__aenter__.return_value = response
    return session, response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sendkey", "expected_url"),
    [
        ("SCTtest", "https://sctapi.ftqq.com/SCTtest.send"),
        ("sctp123ttest", "https://123.push.ft07.com/send/sctp123ttest.send"),
    ],
)
async def test_serverchan_accepts_success_for_both_endpoints(serverchan_response, sendkey, expected_url):
    session, response = serverchan_response
    notifier = Notifer(session, {"sendkey": sendkey})

    await notifier.ms_send("test body", "test title")

    request = session.post.call_args.kwargs
    assert request["url"] == expected_url
    assert request["json"] == {"title": "test title", "desp": "test body"}
    assert request["timeout"].total > 0
    assert request["allow_redirects"] is False
    response.json.assert_awaited_once()
    session.post.return_value.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 429])
async def test_serverchan_rejects_non_2xx_and_closes_response(serverchan_response, status):
    session, response = serverchan_response
    response.status = status
    notifier = Notifer(session, {"sendkey": "SCTtest"})

    with pytest.raises(ServerChanResponseError, match=f"HTTP {status}"):
        await notifier.ms_send()

    response.json.assert_not_awaited()
    session.post.return_value.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {}, {"code": False}, {"code": "0"}])
async def test_serverchan_rejects_malformed_response(serverchan_response, payload):
    session, response = serverchan_response
    response.json.return_value = payload
    notifier = Notifer(session, {"sendkey": "SCTtest"})

    with pytest.raises(ServerChanResponseError, match="响应格式无效"):
        await notifier.ms_send()

    session.post.return_value.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_serverchan_rejects_invalid_json(serverchan_response):
    session, response = serverchan_response
    response.json.side_effect = ValueError("untrusted response details")
    notifier = Notifer(session, {"sendkey": "SCTtest"})

    with pytest.raises(ServerChanResponseError, match="响应格式无效") as exc_info:
        await notifier.ms_send()

    assert "untrusted response details" not in str(exc_info.value)
    session.post.return_value.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_serverchan_business_rejection_keeps_cursor_until_success(serverchan_response, tmp_path, monkeypatch):
    session, response = serverchan_response
    response.json.side_effect = [
        {"code": 40001, "message": "rejected SCTtest"},
        {"code": 0},
    ]
    notification = {"sendkey": "SCTtest", "enable_telegram": False, "telegram_template": "{text}"}
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "test.db")
    state = StateRepository()
    await state.initialize()
    await state.set_latest_id("123", "old", "Test User")

    app = App()
    app.repository = state
    app.config = {"notification": notification}
    app.notifer = Notifer(session, notification)
    app.monitor = SimpleNamespace(get_weibo_list=AsyncMock(return_value=[
        {"id": "new", "text": "New post", "screen_name": "Test User", "source": "", "region_name": ""},
        {"id": "old"},
    ]))

    await app._check_single({"weiboid": "123"})
    assert state.get_latest_id("123") == "old"
    assert await state.get_weibo_history("123") == []
    assert await state.get_push_stats() == {"serverchan_failed": 1}
    async with state._connect() as db:
        async with db.execute("SELECT error_message FROM push_log") as cursor:
            error, = await cursor.fetchone()
    assert error == "Server酱业务错误 (code=40001)"

    await app._check_single({"weiboid": "123"})
    assert state.get_latest_id("123") == "new"
    assert await state.get_push_stats() == {"serverchan_failed": 1, "serverchan_success": 1}

    reloaded_state = StateRepository()
    await reloaded_state.initialize()
    assert reloaded_state.get_latest_id("123") == "new"
