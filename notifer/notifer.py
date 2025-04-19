import aiohttp
from loguru import logger
import telegram
import asyncio


class Notifer:
    def __init__(self, notification_config: dict):
        self.notification_config = notification_config

    async def ms_send(self, desp: str = '', title: str = "weibo") -> None:
        # server酱推送
        async with aiohttp.ClientSession() as session:
            try:
                response = await session.post(
                    url=f"https://{self.notification_config['num']}.push.ft07.com/send/{self.notification_config['sendkey']}.send",
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
            await bot.send_message(text=message, chat_id=self.notification_config['chatid'], parse_mode="MarkdownV2",
                                   disable_web_page_preview=True)
            logger.info("telegram推送成功！")
        except Exception as e:
            logger.exception(f"telegram推送失败：{e}")
            raise

    async def send_message(self, message: str, telegram_message: str, title: str) -> None:
        """
        同时使用telegram和server酱推送消息
        message: 要发送给Server酱的消息
        telegram_message: 要发送给Telegram的消息
        title: Server酱的消息标题
        notification_config: 通知配置字典
        """
        logger.info(message)
        await asyncio.gather(
            self.telegram_send(telegram_message),
            self.ms_send(message, title)
        )
