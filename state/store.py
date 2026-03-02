from pathlib import Path
import yaml
from loguru import logger

STATE_PATH = Path(__file__).resolve().parent.parent / "state.yaml"


def load_state() -> dict:
    """加载运行时状态，不存在则返回空结构"""
    if not STATE_PATH.exists():
        return {"accounts": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {"accounts": {}}
    except Exception:
        logger.exception("加载状态文件失败")
        return {"accounts": {}}


def save_state(state: dict) -> None:
    """保存运行时状态到 state.yaml"""
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(state, f, allow_unicode=True)
        logger.debug("状态文件保存成功")
    except Exception:
        logger.exception("保存状态文件失败")
        raise


def get_latest_id(state: dict, weiboid: str) -> str:
    """获取某账号的 latest_id"""
    return state.get("accounts", {}).get(weiboid, {}).get("latest_id", "")


def set_latest_id(state: dict, weiboid: str, latest_id: str) -> None:
    """设置某账号的 latest_id"""
    state.setdefault("accounts", {})[weiboid] = {"latest_id": latest_id}
