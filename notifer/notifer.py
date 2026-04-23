import re
import aiohttp
from loguru import logger
import telegram
import asyncio


def escape_markdown_v2(text: str) -> str:
    """转义 Telegram MarkdownV2 特殊字符"""
    special_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(special_chars)}])', r'\\\1', text)


class Notifer:
    def __init__(self, session: aiohttp.ClientSession, notification_config: dict):
        self.session = session
        self.notification_config = notification_config

    async def ms_send(self, desp: str = '', title: str = "weibo") -> None:
        # server酱推送
        try:
            sendkey = self.notification_config['sendkey']
            # 判断 sendkey 格式并构造 URL
            if sendkey.startswith('sctp'):
                # 新版 sendkey 格式: sctp{num}t...
                match = re.match(r'sctp(\d+)t', sendkey)
                if not match:
                    raise ValueError(f'Invalid sendkey format: {sendkey}')
                num = match.group(1)
                url = f"https://{num}.push.ft07.com/send/{sendkey}.send"
            else:
                # 旧版 sendkey 格式
                url = f"https://sctapi.ftqq.com/{sendkey}.send"

            response = await self.session.post(
                url=url,
                json={"title": title, "desp": desp},
                headers={"Content-Type": "application/json;charset=utf-8"})
            response.raise_for_status()
            logger.info("Server酱推送成功！")
        except Exception as e:
            logger.exception(f"Server酱推送失败: {e}")
            raise

    async def telegram_send(self, message: str) -> None:
        try:
            bot = telegram.Bot(self.notification_config['tgbottoken'])
            escaped_message = escape_markdown_v2(message)
            await bot.send_message(text=escaped_message, chat_id=self.notification_config['chatid'], parse_mode="MarkdownV2",
                                   disable_web_page_preview=True)
            logger.info("telegram推送成功！")
        except Exception as e:
            logger.exception(f"telegram推送失败：{e}")
            raise

    async def send_message(self, message: str, telegram_message: str, title: str) -> None:
        """
        根据配置开关推送消息到telegram和/或server酱
        message: 要发送给Server酱的消息
        telegram_message: 要发送给Telegram的消息
        title: Server酱的消息标题
        """
        logger.info(message)

        tasks = []
        if self.notification_config.get('enable_telegram', True):
            tasks.append(self.telegram_send(telegram_message))
        else:
            logger.debug("Telegram 推送已禁用")

        if self.notification_config.get('enable_serverchan', True):
            tasks.append(self.ms_send(message, title))
        else:
            logger.debug("Server酱 推送已禁用")

        if tasks:
            await asyncio.gather(*tasks)
        else:
            logger.warning("所有通知渠道均已禁用，跳过推送")
