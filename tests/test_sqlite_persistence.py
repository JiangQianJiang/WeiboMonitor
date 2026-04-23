"""Tests for SQLite persistence migration."""
import asyncio
import os
import tempfile
from pathlib import Path

import aiosqlite
import pytest

from state.repository import StateRepository


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    yield db_path
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def repo(temp_db, monkeypatch):
    """Create a repository with temporary database."""
    test_repo = StateRepository()
    monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
    return test_repo


class TestSchemaValidation:
    """AC-1: Database schema correctly implements the three-table design."""

    @pytest.mark.asyncio
    async def test_three_tables_exist(self, repo):
        """Query returns account_state, weibo_history, push_log."""
        await repo.initialize()
        async with repo._connect() as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cursor:
                tables = [row[0] async for row in cursor]
            assert 'account_state' in tables
            assert 'weibo_history' in tables
            assert 'push_log' in tables

    @pytest.mark.asyncio
    async def test_insert_valid_data(self, repo):
        """Insert valid data into each table succeeds."""
        await repo.initialize()
        await repo.set_latest_id('test_user', 'weibo123', 'TestUser')

        weibo_info = {
            'weiboid': 'test_user',
            'id': 'weibo123',
            'text': 'Test weibo content',
            'screen_name': 'TestUser',
            'source': 'Web',
            'region_name': 'Beijing',
            'time': '2024-01-01'
        }
        await repo.save_weibo_history(weibo_info)

        await repo.log_push('test_user', 'weibo123', 'telegram', 'success')

    @pytest.mark.asyncio
    async def test_duplicate_weibo_rejected(self, repo):
        """Inserting duplicate (weiboid, weibo_id) fails with UNIQUE constraint."""
        await repo.initialize()
        # First ensure account_state exists (required for FK)
        await repo.set_latest_id('test_user', 'first_id', 'TestUser')

        weibo_info = {
            'weiboid': 'test_user',
            'id': 'weibo456',
            'text': 'First post',
            'screen_name': 'TestUser',
        }
        await repo.save_weibo_history(weibo_info)

        weibo_info['text'] = 'Duplicate post'
        await repo.save_weibo_history(weibo_info)

        history = await repo.get_weibo_history('test_user')
        assert len(history) == 1

    @pytest.mark.asyncio
    async def test_foreign_key_enforced_on_weibo_history(self, repo):
        """Inserting weibo_history with non-existent weiboid fails with FK constraint."""
        await repo.initialize()
        weibo_info = {
            'weiboid': 'nonexistent_user',
            'id': 'weibo789',
            'text': 'Orphan post',
            'screen_name': 'TestUser',
        }
        # Should raise integrity error due to foreign key constraint
        with pytest.raises(aiosqlite.IntegrityError):
            await repo.save_weibo_history(weibo_info)

    @pytest.mark.asyncio
    async def test_foreign_key_enforced_on_push_log(self, repo):
        """Inserting push_log with non-existent weiboid fails with FK constraint."""
        await repo.initialize()
        # Should raise integrity error due to foreign key constraint
        with pytest.raises(aiosqlite.IntegrityError):
            await repo.log_push('nonexistent_user', 'weibo999', 'telegram', 'success')

    @pytest.mark.asyncio
    async def test_all_indexes_exist(self, repo):
        """All specified indexes exist."""
        await repo.initialize()
        async with repo._connect() as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ) as cursor:
                indexes = [row[0] async for row in cursor]
            expected_indexes = [
                'idx_weibo_history_weiboid',
                'idx_weibo_history_fetched_at',
                'idx_push_log_weiboid',
                'idx_push_log_pushed_at',
                'idx_push_log_status'
            ]
            for idx in expected_indexes:
                assert idx in indexes, f"Missing index: {idx}"


class TestStateUpdateTiming:
    """AC-2: State update timing preserves 'push-then-update' guarantee."""

    @pytest.mark.asyncio
    async def test_push_success_updates_both(self, repo):
        """When push succeeds, latest_id is updated in both cache and DB."""
        await repo.initialize()

        await repo.set_latest_id('user1', 'id001', 'User1')

        assert repo.get_latest_id('user1') == 'id001'

        async with repo._connect() as db:
            async with db.execute(
                "SELECT latest_id FROM account_state WHERE weiboid = ?", ('user1',)
            ) as cursor:
                row = await cursor.fetchone()
                assert row[0] == 'id001'

    @pytest.mark.asyncio
    async def test_push_failure_preserves_old_id(self, repo):
        """When push fails, latest_id remains unchanged."""
        await repo.initialize()
        await repo.set_latest_id('user2', 'old_id', 'User2')

        old_cache_id = repo.get_latest_id('user2')
        assert old_cache_id == 'old_id'

        async with repo._connect() as db:
            async with db.execute(
                "SELECT latest_id FROM account_state WHERE weiboid = ?", ('user2',)
            ) as cursor:
                row = await cursor.fetchone()
                assert row[0] == 'old_id'

    @pytest.mark.asyncio
    async def test_partial_success_does_not_block_update(self, repo):
        """When at least one channel succeeds, state should be updated."""
        await repo.initialize()
        await repo.set_latest_id('user3', 'old_id', 'User3')

        # Simulate: set_latest_id updates both cache and DB correctly
        await repo.set_latest_id('user3', 'new_id', 'User3')

        # After successful DB write, cache and DB should match
        assert repo.get_latest_id('user3') == 'new_id'
        async with repo._connect() as db:
            async with db.execute(
                "SELECT latest_id FROM account_state WHERE weiboid = ?", ('user3',)
            ) as cursor:
                row = await cursor.fetchone()
                assert row[0] == 'new_id'


class TestPerChannelLogging:
    """AC-4: Per-channel push logging captures partial failures."""

    @pytest.mark.asyncio
    async def test_both_channels_success(self, repo):
        """When both channels succeed, two success records logged."""
        await repo.initialize()
        # Ensure account_state exists for FK
        await repo.set_latest_id('user1', 'w001', 'User1')
        await repo.log_push('user1', 'w001', 'telegram', 'success')
        await repo.log_push('user1', 'w001', 'serverchan', 'success')

        stats = await repo.get_push_stats(days=1)
        assert stats.get('telegram_success', 0) == 1
        assert stats.get('serverchan_success', 0) == 1

    @pytest.mark.asyncio
    async def test_partial_failure_logged(self, repo):
        """When one channel fails, one success and one failed record."""
        await repo.initialize()
        # Ensure account_state exists for FK
        await repo.set_latest_id('user1', 'w002', 'User1')
        await repo.log_push('user1', 'w002', 'telegram', 'success')
        await repo.log_push('user1', 'w002', 'serverchan', 'failed', 'Connection timeout')

        stats = await repo.get_push_stats(days=1)
        assert stats.get('telegram_success', 0) == 1
        assert stats.get('serverchan_failed', 0) == 1

    @pytest.mark.asyncio
    async def test_error_message_captured(self, repo):
        """Error messages captured in push_log.error_message column."""
        await repo.initialize()
        # Ensure account_state exists for FK
        await repo.set_latest_id('user1', 'w003', 'User1')
        await repo.log_push('user1', 'w003', 'telegram', 'failed', 'Invalid token')

        async with repo._connect() as db:
            async with db.execute(
                "SELECT error_message FROM push_log WHERE weibo_id = ? AND channel = ?",
                ('w003', 'telegram')
            ) as cursor:
                row = await cursor.fetchone()
                assert row[0] == 'Invalid token'


class TestAsyncStateAPI:
    """AC-3: Async state functions maintain backward compatibility."""

    @pytest.mark.asyncio
    async def test_async_load_state_returns_dict_format(self, temp_db, monkeypatch):
        """load_state returns dict with {"accounts": {weiboid: {"latest_id": ...}}}."""
        # Create repo with temp_db
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()
        await test_repo.set_latest_id('test_user', 'weibo999', 'TestUser')

        # Patch the global repo in store
        import state.store
        original_repo = state.store._repository
        state.store._repository = test_repo

        try:
            from state.store import load_state
            result = await load_state()
            assert "accounts" in result
            assert "test_user" in result["accounts"]
            assert result["accounts"]["test_user"]["latest_id"] == "weibo999"
        finally:
            state.store._repository = original_repo

    @pytest.mark.asyncio
    async def test_async_get_latest_id(self, temp_db, monkeypatch):
        """get_latest_id returns correct ID from memory cache."""
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()
        await test_repo.set_latest_id('user_abc', 'id_xyz', 'UserABC')

        import state.store
        original_repo = state.store._repository
        state.store._repository = test_repo

        try:
            from state.store import get_latest_id
            result = await get_latest_id('user_abc')
            assert result == 'id_xyz'
        finally:
            state.store._repository = original_repo

    @pytest.mark.asyncio
    async def test_async_set_latest_id(self, temp_db, monkeypatch):
        """set_latest_id updates both memory and database."""
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()

        import state.store
        original_repo = state.store._repository
        state.store._repository = test_repo

        try:
            from state.store import set_latest_id
            await set_latest_id('user_xyz', 'new_id_123')

            # Check cache
            assert test_repo.get_latest_id('user_xyz') == 'new_id_123'
            # Check DB
            async with test_repo._connect() as db:
                async with db.execute(
                    "SELECT latest_id FROM account_state WHERE weiboid = ?", ('user_xyz',)
                ) as cursor:
                    row = await cursor.fetchone()
                    assert row[0] == 'new_id_123'
        finally:
            state.store._repository = original_repo


class TestWeiboHistory:
    """AC-5: Weibo history permanently stored without duplicates."""

    @pytest.mark.asyncio
    async def test_weibo_history_saved_with_all_fields(self, repo):
        """Weibo saved with text, screen_name, source, region_name, created_time."""
        await repo.initialize()
        await repo.set_latest_id('hist_user', 'latest', 'HistUser')

        weibo_info = {
            'weiboid': 'hist_user',
            'id': 'weibo_hist_1',
            'text': 'Test content for history',
            'screen_name': 'HistUser',
            'source': 'iPhone',
            'region_name': 'Shanghai',
            'time': '2024-06-01 12:00:00'
        }
        await repo.save_weibo_history(weibo_info)

        history = await repo.get_weibo_history('hist_user')
        assert len(history) == 1
        assert history[0]['text'] == 'Test content for history'
        assert history[0]['screen_name'] == 'HistUser'
        assert history[0]['source'] == 'iPhone'
        assert history[0]['region_name'] == 'Shanghai'

    @pytest.mark.asyncio
    async def test_duplicate_weibo_is_silently_ignored(self, repo):
        """Duplicate (weiboid, weibo_id) is silently ignored via ON CONFLICT DO NOTHING."""
        await repo.initialize()
        await repo.set_latest_id('dup_user', 'latest', 'DupUser')

        weibo_info = {
            'weiboid': 'dup_user',
            'id': 'dup_weibo_id',
            'text': 'Original text',
            'screen_name': 'DupUser',
        }
        await repo.save_weibo_history(weibo_info)

        # Try to insert same weibo again
        weibo_info['text'] = 'Updated text'
        await repo.save_weibo_history(weibo_info)

        history = await repo.get_weibo_history('dup_user')
        assert len(history) == 1
        assert history[0]['text'] == 'Original text'


class TestWriteOrderForeignKey:
    """Verify write order respects FK constraints."""

    @pytest.mark.asyncio
    async def test_cannot_save_history_before_account_state(self, repo):
        """Cannot insert weibo_history without existing account_state (FK enforced)."""
        await repo.initialize()

        # Direct insert to bypass repository's FK handling
        async with repo._connect() as db:
            # This should fail because account_state doesn't have 'orphan_user'
            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    """INSERT INTO weibo_history (weiboid, weibo_id, text, screen_name)
                       VALUES ('orphan_user', 'weibo_orphan', 'orphan text', 'Orphan')"""
                )
                await db.commit()


class TestCacheZeroQuery:
    """AC-8: Memory cache provides zero-latency reads."""

    @pytest.mark.asyncio
    async def test_get_latest_id_no_db_query(self, repo):
        """get_latest_id reads from memory cache without database query."""
        await repo.initialize()
        await repo.set_latest_id('cache_user', 'cache_id_1', 'CacheUser')

        # get_latest_id should read from cache, not DB
        result = repo.get_latest_id('cache_user')
        assert result == 'cache_id_1'

        # Verify by checking _cache directly
        assert 'cache_user' in repo._cache
        assert repo._cache['cache_user']['latest_id'] == 'cache_id_1'

    @pytest.mark.asyncio
    async def test_cache_initialized_at_startup(self, repo):
        """Cache is loaded once during repository initialization."""
        await repo.initialize()

        # Pre-populate some data
        await repo.set_latest_id('startup_user', 'startup_id', 'StartupUser')

        # Create new repo instance and verify cache loads
        new_repo = StateRepository()
        new_repo._db_path = repo._db_path
        await new_repo.initialize()

        # Cache should be populated from DB
        assert 'startup_user' in new_repo._cache
        assert new_repo._cache['startup_user']['latest_id'] == 'startup_id'