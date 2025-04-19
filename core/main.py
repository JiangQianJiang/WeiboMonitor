import asyncio
from monitor import weibo
from loguru import logger
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from notifer.notifer import Notifer

CONFIG_PATH = "../users.yaml"
logger.add("../log/runtime_{time}.log", rotation="1 day", retention="3 days", compression="zip", level="INFO")


async def load_config() -> dict:
    """加载配置文件并返回配置字典"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            users = yaml.safe_load(f) or {}
        logger.info("配置文件加载成功")
        return users
    except Exception as e:
        logger.exception("加载配置文件失败")
        raise


async def save_config(users: dict) -> None:
    """保存配置文件"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(users, f, allow_unicode=True)
        logger.debug("配置文件保存成功")
    except Exception:
        logger.exception("保存配置文件失败")
        raise


async def check_weibo_update(users: dict, monitor: weibo.WeiboMonitor, noti: Notifer) -> None:
    """检测每个被监控的微博更新"""
    logger.debug("开始检查微博更新")
    tasks = []
    for account in users['accounts']:
        tasks.append(check_single_account(monitor, account, users, noti))
    await asyncio.gather(*tasks)


async def check_single_account(monitor, account: dict, users: dict, noti: Notifer) -> None:
    """检测微博更新"""
    weiboid = account['weiboid']
    try:
        info = await monitor.get_latest_weibo(weiboid)
        if info:
            # 有更新才推送
            if account['latest_id'] != info['id']:
                account['latest_id'] = info['id']
                await save_config(users)
                message = (f"【{info['screen_name']}】发表微博：\n\n"
                           f"{info['text']}\n\n"
                           f"{info['region_name']} | {info['source']}\n\n"
                           f"https://weibo.com/{weiboid}/{info['id']}\n\n")
                telegram_message = users['notification']['telegram_template'].format(
                    screen_name=info['screen_name'],
                    text=info['text'],
                    region_name=info['region_name'],
                    source=info['source'],
                    url=f"https://weibo.com/{weiboid}/{info['id']}"
                )
                logger.info(f"{info['screen_name']}检测到新微博，已推送")
                await noti.send_message(message, telegram_message, f"{info['screen_name']}发微博啦")
            else:
                logger.debug(f"{info['screen_name']}无新微博更新")
        else:
            logger.warning(f"{weiboid}未获取到微博内容")
    except Exception as e:
        logger.exception("检查微博更新失败")


async def main() -> None:
    # 加载配置
    users = await load_config()
    # 初始化 Notifier
    noti = Notifer(users['notification'])
    # 创建 WeiboMonitor 实例并检查更新
    async with weibo.WeiboMonitor() as monitor:
        await check_weibo_update(users, monitor, noti)


def run():
    asyncio.run(main())


if __name__ == '__main__':
    try:
        scheduler = BlockingScheduler(timezone="Asia/Shanghai")
        scheduler.add_job(run, 'interval', seconds=8)
        logger.info("调度器启动，检查间隔8秒")
        scheduler.start()
    except Exception as e:
        logger.exception("服务异常终止")