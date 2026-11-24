from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from loguru import logger
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from core.app import App
from notifer.notifer import Notifer


async def test_app_only_sends_cookie_to_weibo(monkeypatch):
    requests = []
    cookie = "session=synthetic"
    trace_config = aiohttp.TraceConfig()

    @trace_config.on_request_start.append
    async def capture_request(session, context, request):
        requests.append((request.url.host, request.headers.get("Cookie")))
        raise aiohttp.ClientConnectionError("intercept before network")

    create_session = aiohttp.ClientSession
    monkeypatch.setattr(
        "core.app.aiohttp.ClientSession",
        lambda **kwargs: create_session(trace_configs=[trace_config], **kwargs),
    )
    monkeypatch.setattr("core.app.logger.add", Mock())
    monkeypatch.setattr("core.app.get_repository", AsyncMock())
    monkeypatch.setattr("core.app.load_config", lambda: {
        "cookie": cookie,
        "notification": {"sendkey": "test-key"},
    })

    app = App()
    await app.start()
    try:
        await app.monitor.get_weibo_list("123")
        with pytest.raises(aiohttp.ClientConnectionError):
            await app.notifer.ms_send("Test")
    finally:
        await app.stop()

    assert requests == [("www.weibo.com", cookie), ("sctapi.ftqq.com", None)]


async def test_notification_errors_omit_credentials_from_logs_and_results():
    sendkey = "fake-serverchan-secret"
    token = "fake-telegram-secret"
    url = URL(f"https://sctapi.ftqq.com/{sendkey}.send")
    request = aiohttp.RequestInfo(url, "POST", CIMultiDictProxy(CIMultiDict()), url)
    response_error = aiohttp.ClientResponseError(request, (), status=502)
    session = Mock()
    session.post.side_effect = response_error
    notifier = Notifer(session, {"sendkey": sendkey, "tgbottoken": token, "chatid": "1"})
    bot = Mock()
    bot.send_message = AsyncMock(side_effect=RuntimeError(f"https://api.telegram.org/bot{token}"))
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)))
    try:
        with patch("notifer.notifer.telegram.Bot", return_value=bot):
            results = await notifier.send_message("Message", "Telegram message", "Title")
    finally:
        logger.remove(sink)

    assert results == {
        "telegram": (False, "RuntimeError"),
        "serverchan": (False, "ClientResponseError (HTTP 502)"),
    }
    rendered_logs = "".join(logs)
    assert "HTTP 502" in rendered_logs
    assert sendkey not in rendered_logs
    assert token not in rendered_logs
