from pathlib import Path
import yaml
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "users.yaml"
LOG_DIR = PROJECT_ROOT / "log"


def load_config() -> dict:
    """加载 users.yaml 配置"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
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
