import asyncio
from datetime import UTC, datetime
from functools import partial

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from core.config import load_config, LOG_DIR
from core.events import EventType
from monitor.events import EventMonitor, EventOptions
from monitor.weibo import WeiboMonitor
from notifer.notifer import Notifer, event_notification, format_serverchan_message, format_telegram_message
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
        self.session = aiohttp.ClientSession(
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0',
            },
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        self.monitor = WeiboMonitor(self.session, cookie=self.config.get("cookie", ""))
        self.notifer = Notifer(self.session, self.config["notification"])
        logger.info("App 启动完成")

    async def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if self.session and not self.session.closed:
            await self.session.close()
        logger.info("App 已停止")

    async def _check_updates(self):
        await asyncio.gather(*(self._check_single(account) for account in self.config.get("accounts", [])))

    def _event_monitor(self, account):
        return EventMonitor(
            str(account["weiboid"]), EventOptions.from_account(account), self.monitor, self.repository,
            EventMonitor.viewer_key(self.config.get("cookie")),
        )

    async def _check_single(self, account: dict):
        monitor = self._event_monitor(account)
        await self._scan_account(monitor, timeline=True, activity=True)
        await self._deliver_account(monitor.uid)

    async def _scan_account(self, monitor, *, timeline, activity):
        try:
            await monitor.poll(timeline=timeline, activity=activity)
        except Exception as error:
            logger.error(f"检查微博更新失败 {monitor.uid} ({type(error).__name__})")

    async def _deliver_account(self, uid):
        try:
            await self._deliver_pending(uid)
        except Exception as error:
            logger.error(f"处理微博通知失败 {uid} ({type(error).__name__})")

    async def _deliver_pending(self, uid):
        notification = self.config["notification"]
        if not notification.get("enable_telegram", True) and not notification.get("enable_serverchan", True):
            return
        for _ in range(100):
            delivery = await self.repository.claim_pending_event(uid)
            if delivery is None:
                return
            event, token = delivery
            try:
                if event.type == EventType.POST_CREATED:
                    post, url = event.data["post"], event.data["url"]
                    messages = (
                        partial(format_serverchan_message, post, url),
                        partial(format_telegram_message, notification, post, url),
                        f"{post['screen_name']}发微博啦",
                    )
                else:
                    messages = event_notification(event)
                results = await self.notifer.send_message(*messages)
            except Exception as error:
                results = {"delivery": (False, type(error).__name__)}
            if not await self.repository.finish_event_delivery(event, token, results):
                logger.warning(f"{uid} 的事件 {event.type} 尚未推送成功，将在下次检查重试")
                return
            if not any(sent for sent, _ in results.values()):
                logger.error(f"{uid} 的事件 {event.event_id} 已达到推送重试上限")

    def _schedule_jobs(self):
        start = datetime.now(UTC)
        for account in self.config.get("accounts", []):
            monitor = self._event_monitor(account)
            for enabled, interval, name in (
                (monitor.watches_timeline, 8, "timeline"),
                (monitor.watches_activity, 60, "activity"),
            ):
                if enabled:
                    self.scheduler.add_job(
                        self._scan_account, "interval", seconds=interval, next_run_time=start,
                        id=f"{name}:{monitor.uid}", args=[monitor],
                        kwargs={"timeline": name == "timeline", "activity": name == "activity"},
                        max_instances=1,
                    )
            self.scheduler.add_job(
                self._deliver_account, "interval", seconds=8, next_run_time=start,
                id=f"delivery:{monitor.uid}", args=[monitor.uid], max_instances=1,
            )

    async def _run(self):
        await self.start()
        self._schedule_jobs()
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
