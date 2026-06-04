import asyncio

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from core.config import load_config, LOG_DIR
from monitor.weibo import WeiboMonitor
from notifer.notifer import Notifer, escape_markdown_v2
from state.store import get_repository


class App:
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self.config: dict = {}
        self.repository = None
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
        self.repository = await get_repository()
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
            info_list = await self.monitor.get_weibo_list(weiboid)
            if not info_list:
                logger.warning(f"{weiboid}未获取到微博内容")
                return

            old_id = self.repository.get_latest_id(weiboid)

            # Cold start: silently record the newest weibo ID, don't push
            if not old_id:
                latest = info_list[0]
                await self.repository.set_latest_id(weiboid, latest["id"], latest["screen_name"])
                logger.info(f"{latest['screen_name']}首次记录，不推送，latest_id={latest['id']}")
                return

            # Boundary-prefix: API returns newest-first, find old_id position
            idx = next((i for i, w in enumerate(info_list) if w["id"] == old_id), None)

            if idx is None:
                # old_id not on first page — push all and warn
                logger.warning(f"{weiboid}: old_id {old_id} not found on first page, pushing all {len(info_list)} items")
                new_weibos = list(reversed(info_list))
            elif idx == 0:
                # First item is old_id, no new weibos
                logger.debug(f"{info_list[0]['screen_name']}无新微博更新")
                return
            else:
                # Take items before old_id, reverse for old→new order
                new_weibos = list(reversed(info_list[:idx]))

            for info in new_weibos:
                message = (
                    f"【{info['screen_name']}】发表微博：\n\n"
                    f"{info['text']}\n\n"
                    f"{info['region_name']} | {info['source']}\n\n"
                    f"https://weibo.com/{weiboid}/{info['id']}\n\n"
                )
                # Escape user data fields BEFORE template substitution to preserve template markup
                telegram_message = self.config["notification"]["telegram_template"].format(
                    screen_name=escape_markdown_v2(info["screen_name"]),
                    text=escape_markdown_v2(info["text"]),
                    region_name=escape_markdown_v2(info["region_name"]),
                    source=escape_markdown_v2(info["source"]),
                    url=escape_markdown_v2(f"https://weibo.com/{weiboid}/{info['id']}"),
                )
                logger.info(f"{info['screen_name']}检测到新微博，准备推送")

                push_results = await self.notifer.send_message(
                    message, telegram_message, f"{info['screen_name']}发微博啦"
                )

                info["weiboid"] = weiboid

                # Check if at least one channel succeeded
                any_success = any(success for success, _ in push_results.values()) if push_results else False

                if any_success:
                    # At least one channel succeeded - ensure parent record first
                    await self.repository.set_latest_id(weiboid, info["id"], info["screen_name"])
                    await self.repository.save_weibo_history(info)
                    # Log all push results after parent exists
                    for channel, (success, error) in push_results.items():
                        status = "success" if success else "failed"
                        await self.repository.log_push(weiboid, info["id"], channel, status, error)
                    logger.info(f"{info['screen_name']}推送成功，状态已更新")
                else:
                    # All channels failed - log and stop without advancing state
                    for channel, (success, error) in push_results.items():
                        await self.repository.log_push(weiboid, info["id"], channel, "failed", error)
                    logger.error(f"{info['screen_name']}推送失败，中断后续推送")
                    break

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