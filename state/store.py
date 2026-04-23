"""State management layer with async-only API as per plan."""
from typing import Dict, Optional

from state.repository import StateRepository

_repository: Optional[StateRepository] = None


async def get_repository() -> StateRepository:
    """Get or create global repository instance (singleton pattern)."""
    global _repository
    if _repository is None:
        _repository = StateRepository()
        await _repository.initialize()
    return _repository


async def load_state() -> Dict:
    """Async state loading - returns memory cache structure with {"accounts": {...}} format."""
    repo = await get_repository()
    return {
        "accounts": {
            weiboid: {"latest_id": data["latest_id"]}
            for weiboid, data in repo._cache.items()
        }
    }


async def get_latest_id(weiboid: str) -> str:
    """Async get_latest_id - reads from memory cache (zero database queries)."""
    repo = await get_repository()
    return repo.get_latest_id(weiboid)


async def set_latest_id(weiboid: str, latest_id: str) -> None:
    """Async set_latest_id - updates database then memory cache."""
    repo = await get_repository()
    await repo.set_latest_id(weiboid, latest_id)


async def save_state(state: Dict) -> None:
    """Async save_state - no-op since state is auto-persisted by repository."""
    pass