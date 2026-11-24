import re
from typing import Dict, Optional, Tuple
import aiohttp
from loguru import logger
import telegram
import asyncio


def escape_markdown_v2(text: str) -> str:
    """转义 Telegram MarkdownV2 特殊字符"""
    special_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(special_chars)}])', r'\\\1', text)


class ServerChanResponseError(RuntimeError):
    """A response error with no request URL or provider message."""

    def __init__(self, *, status: int | None = None, code: int | None = None):
        self.status = status
        self.code = code
        if status is not None:
            message = f"Server酱 HTTP {status}"
        elif code is not None:
            message = f"Server酱业务错误 (code={code})"
        else:
            message = "Server酱响应格式无效"
        super().__init__(message)


class Notifer:
    def __init__(self, session: aiohttp.ClientSession, notification_config: dict):
        self.session = session
        self.notification_config = notification_config

    async def ms_send(self, desp: str = '', title: str = "weibo") -> None:
        sendkey = self.notification_config['sendkey']
        if sendkey.startswith('sctp'):
            match = re.match(r'sctp(\d+)t', sendkey)
            if not match:
                raise ValueError("Invalid Server酱 SendKey format")
            url = f"https://{match.group(1)}.push.ft07.com/send/{sendkey}.send"
        else:
            url = f"https://sctapi.ftqq.com/{sendkey}.send"

        async with self.session.post(
            url=url,
            json={"title": title, "desp": desp},
            headers={"Content-Type": "application/json;charset=utf-8"},
            timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=False,
        ) as response:
            if not 200 <= response.status < 300:
                raise ServerChanResponseError(status=response.status)
            try:
                payload = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                raise ServerChanResponseError() from None

            code = payload.get("code") if isinstance(payload, dict) else None
            if type(code) is not int:
                raise ServerChanResponseError()
            if code != 0:
                raise ServerChanResponseError(code=code)

        logger.info("Server酱推送成功！")

    async def telegram_send(self, message: str) -> None:
        bot = telegram.Bot(self.notification_config['tgbottoken'])
        await bot.send_message(text=message, chat_id=self.notification_config['chatid'], parse_mode="MarkdownV2",
                               disable_web_page_preview=True)
        logger.info("telegram推送成功！")

    async def send_message(self, message: str, telegram_message: str, title: str) -> Dict[str, Tuple[bool, Optional[str]]]:
        """
        Push message to telegram and/or serverchan based on config switches.
        Returns {channel: (success, safe_error_summary)} for logging and persistence.

        message: Server酱 message
        telegram_message: Telegram message
        title: Server酱 title
        """
        logger.info(message)

        channels: Dict[str, "coroutine"] = {}
        if self.notification_config.get('enable_telegram', True):
            channels['telegram'] = self.telegram_send(telegram_message)
        if self.notification_config.get('enable_serverchan', True):
            channels['serverchan'] = self.ms_send(message, title)

        if not channels:
            logger.warning("All notification channels are disabled, skipping push")
            return {}

        results_list = await asyncio.gather(*channels.values(), return_exceptions=True)

        results: Dict[str, Tuple[bool, Optional[str]]] = {}
        for (channel, _), result in zip(channels.items(), results_list):
            if isinstance(result, BaseException):
                # Provider exception strings may contain credentials in request URLs.
                error = type(result).__name__
                if type(result) is ServerChanResponseError:
                    error = str(result)
                elif isinstance(result, aiohttp.ClientResponseError):
                    error = f"{error} (HTTP {result.status})"
                logger.error(f"{channel} push failed: {error}")
                results[channel] = (False, error)
            else:
                results[channel] = (True, None)

        return results
