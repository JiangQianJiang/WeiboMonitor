import asyncio

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from core.config import load_config, LOG_DIR
from monitor.weibo import WeiboMonitor
from notifer.notifer import Notifer
from state.store import load_state, save_state, get_latest_id, set_latest_id


class App:
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self.config: dict = {}
        self.state: dict = {}
        self.session: aiohttp.ClientSession | None = None
        self.monitor: WeiboMonitor | None = None
        self.notifer: Notifer | None = None

    async def start(self):
        logger.add(
            LOG_DIR / "runtime_{time}.log",
            rotation="1 day", retention="3 days",
            compression="zip", level="INFO",
        )
        self.config = load_config()
        self.state = load_state()
        self.session = aiohttp.ClientSession(headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0',
            'Cookie': self.config.get("cookie", ""),
        })
        self.monitor = WeiboMonitor(self.session)
        self.notifer = Notifer(self.session, self.config["notification"])
        logger.info("App 启动完成")

    async def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if self.session and not self.session.closed:
            await self.session.close()
        logger.info("App 已停止")

    async def _check_updates(self):
        logger.debug("开始检查微博更新")
        tasks = [
            self._check_single(account)
            for account in self.config["accounts"]
        ]
        await asyncio.gather(*tasks)

    async def _check_single(self, account: dict):
        weiboid = account["weiboid"]
        try:
            info = await self.monitor.get_latest_weibo(weiboid)
            if not info:
                logger.warning(f"{weiboid}未获取到微博内容")
                return

            old_id = get_latest_id(self.state, weiboid)
            if old_id == info["id"]:
                logger.debug(f"{info['screen_name']}无新微博更新")
                return

            set_latest_id(self.state, weiboid, info["id"])
            save_state(self.state)

            message = (
                f"【{info['screen_name']}】发表微博：\n\n"
                f"{info['text']}\n\n"
                f"{info['region_name']} | {info['source']}\n\n"
                f"https://weibo.com/{weiboid}/{info['id']}\n\n"
            )
            telegram_message = self.config["notification"]["telegram_template"].format(
                screen_name=info["screen_name"],
                text=info["text"],
                region_name=info["region_name"],
                source=info["source"],
                url=f"https://weibo.com/{weiboid}/{info['id']}",
            )
            logger.info(f"{info['screen_name']}检测到新微博，已推送")
            await self.notifer.send_message(message, telegram_message, f"{info['screen_name']}发微博啦")
        except Exception:
            logger.exception(f"检查微博更新失败: {weiboid}")

    async def _run(self):
        await self.start()
        self.scheduler.add_job(self._check_updates, "interval", seconds=8)
        self.scheduler.start()
        logger.info("调度器启动，检查间隔8秒")
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    def run_forever(self):
        try:
            asyncio.run(self._run())
        except (KeyboardInterrupt, SystemExit):
            logger.info("服务已终止")

    async def run_once(self):
        await self.start()
        try:
            await self._check_updates()
        finally:
            await self.stop()
