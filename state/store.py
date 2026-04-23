"""Backward-compatible state management layer with async support."""
from typing import Dict, Optional

from loguru import logger

from state.repository import StateRepository

_repository: Optional[StateRepository] = None


async def get_repository() -> StateRepository:
    """Get or create global repository instance (singleton pattern)."""
    global _repository
    if _repository is None:
        _repository = StateRepository()
        await _repository.initialize()
    return _repository


def load_state() -> Dict:
    """
    Synchronous state loading for backward compatibility.
    Note: This is deprecated. Use async_load_state() instead.
    """
    logger.warning("load_state() is deprecated, use await async_load_state() instead")
    return {"accounts": {}}


async def async_load_state() -> Dict:
    """Async state loading - returns memory cache structure."""
    repo = await get_repository()
    return {
        "accounts": {
            weiboid: {"latest_id": data["latest_id"]}
            for weiboid, data in repo._cache.items()
        }
    }


def get_latest_id(state: Dict, weiboid: str) -> str:
    """
    Synchronous get_latest_id for backward compatibility.
    Note: This is deprecated. Use async_get_latest_id() instead.
    """
    return state.get("accounts", {}).get(weiboid, {}).get("latest_id", "")


async def async_get_latest_id(weiboid: str) -> str:
    """Async get_latest_id - reads from memory cache (zero database queries)."""
    repo = await get_repository()
    return repo.get_latest_id(weiboid)


def set_latest_id(state: Dict, weiboid: str, latest_id: str) -> None:
    """
    Synchronous set_latest_id for backward compatibility.
    Note: This is deprecated. Use async_set_latest_id() instead.
    """
    state.setdefault("accounts", {})[weiboid] = {"latest_id": latest_id}


async def async_set_latest_id(weiboid: str, latest_id: str) -> None:
    """Async set_latest_id - updates database then memory cache."""
    repo = await get_repository()
    await repo.set_latest_id(weiboid, latest_id)


def save_state(state: Dict) -> None:
    """
    Backward compatibility: save_state is a no-op now since state
    is auto-persisted by the repository.
    """
    logger.debug("save_state() called - state is now auto-persisted, this is a no-op")


async def async_save_state(state: Dict) -> None:
    """Async save_state - no-op for backward compatibility."""
    logger.debug("async_save_state() called - state is now auto-persisted")