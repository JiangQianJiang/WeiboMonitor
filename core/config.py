from pathlib import Path
import yaml
from loguru import logger

from monitor.events import EventOptions
from notifer.notifer import format_telegram_message

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "users.yaml"
LOG_DIR = PROJECT_ROOT / "log"


def _validate_notification(config: dict) -> None:
    # Report incomplete channels at startup while allowing healthy channels to run.
    if config.get("enable_telegram", True):
        token = config.get("tgbottoken")
        if not isinstance(token, str) or not token.strip():
            logger.warning("Telegram 配置需要填写 notification.tgbottoken")

        chat_id = config.get("chatid")
        if type(chat_id) is not int and (not isinstance(chat_id, str) or not chat_id.strip()):
            logger.warning("Telegram 配置需要填写 notification.chatid")

        template = config.get("telegram_template")
        if not isinstance(template, str) or not template.strip():
            logger.warning("Telegram 配置需要填写 notification.telegram_template")
        else:
            try:
                format_telegram_message(config, {}, "https://weibo.com/1/1")
            except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                logger.warning("Telegram 配置 notification.telegram_template 的格式或占位符无效")

    if config.get("enable_serverchan", True):
        sendkey = config.get("sendkey")
        if not isinstance(sendkey, str) or not sendkey.strip():
            logger.warning("Server酱 配置需要填写 notification.sendkey")


def load_config() -> dict:
    """加载 users.yaml 配置"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        if not isinstance(config, dict):
            raise ValueError("users.yaml 必须是配置对象")
        notification = config.get("notification")
        if not isinstance(notification, dict):
            raise ValueError("users.yaml 的 notification 必须是配置对象")
        accounts = config.get("accounts", [])
        if not isinstance(accounts, list):
            raise ValueError("users.yaml 的 accounts 必须是账号列表")
        seen_ids = set()
        for account in accounts:
            if not isinstance(account, dict) or not str(account.get("weiboid") or "").strip():
                raise ValueError("每个 accounts 条目都需要 weiboid")
            uid = str(account["weiboid"])
            if uid in seen_ids:
                raise ValueError("users.yaml 的 accounts 包含重复的 weiboid")
            seen_ids.add(uid)
            EventOptions.from_account(account)
        _validate_notification(notification)
        logger.info("配置文件加载成功")
        return config
    except Exception:
        logger.exception("加载配置文件失败")
        raise


def save_config(config: dict) -> None:
    """保存配置到 users.yaml"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, allow_unicode=True)
        logger.debug("配置文件保存成功")
    except Exception:
        logger.exception("保存配置文件失败")
        raise
