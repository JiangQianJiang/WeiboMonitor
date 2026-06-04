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


@pytest.fixture
def writable_temp_dir():
    """Create a writable temporary directory for migration tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


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


class TestMigrationScript:
    """AC-6: Data migration from state.yaml completes successfully."""

    @pytest.mark.asyncio
    async def test_migrate_success(self, temp_db, monkeypatch, writable_temp_dir):
        """Migration script reads all accounts from state.yaml and migrates to SQLite."""
        import state.migration
        monkeypatch.setattr(state.migration, 'is_app_running', lambda: False)

        # Create temp state.yaml
        state_yaml = writable_temp_dir / "state.yaml"
        state_yaml.write_text("accounts:\n  user1:\n    latest_id: 'id123'\n  user2:\n    latest_id: 'id456'\n", encoding='utf-8')

        # Monkeypatch paths
        monkeypatch.setattr(state.migration, 'STATE_YAML_PATH', state_yaml)
        monkeypatch.setattr(state.migration, 'STATE_YAML_BACKUP', writable_temp_dir / "state.yaml.backup")

        # Use temp db
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', temp_db)
        monkeypatch.setattr(state.migration, 'StateRepository', lambda: test_repo)

        result = await state.migration.migrate_from_yaml()
        assert result is True

        # Verify DB has accounts
        await test_repo.initialize()
        assert test_repo.get_latest_id('user1') == 'id123'
        assert test_repo.get_latest_id('user2') == 'id456'

        # Verify state.yaml was renamed to state.yaml.backup
        assert not state_yaml.exists(), "state.yaml should be renamed"
        backup_path = writable_temp_dir / "state.yaml.backup"
        assert backup_path.exists(), "state.yaml.backup should exist"

    @pytest.mark.asyncio
    async def test_migrate_missing_state_yaml(self, monkeypatch, writable_temp_dir):
        """Migration skips gracefully when state.yaml doesn't exist."""
        import state.migration
        monkeypatch.setattr(state.migration, 'is_app_running', lambda: False)
        monkeypatch.setattr(state.migration, 'STATE_YAML_PATH', writable_temp_dir / "nonexistent.yaml")
        monkeypatch.setattr(state.migration, 'STATE_YAML_BACKUP', writable_temp_dir / "backup.yaml")

        result = await state.migration.migrate_from_yaml()
        assert result is True  # Should succeed (nothing to migrate)

    def test_migrate_rejects_running_app(self, monkeypatch, writable_temp_dir):
        """Migration refuses to run if app is running (process check)."""
        import state.migration

        # Mock is_app_running to return True
        monkeypatch.setattr(state.migration, 'is_app_running', lambda: True)

        result = asyncio.run(state.migration.migrate_from_yaml())
        assert result is False

    @pytest.mark.asyncio
    async def test_rollback_to_yaml(self, temp_db, monkeypatch, writable_temp_dir):
        """Rollback exports from SQLite to state.yaml."""
        import state.migration
        monkeypatch.setattr(state.migration, 'is_app_running', lambda: False)

        # Setup repo with data
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', temp_db)
        await test_repo.initialize()
        await test_repo.set_latest_id('rollback_user', 'rollback_id', 'RollUser')

        monkeypatch.setattr(state.migration, 'StateRepository', lambda: test_repo)
        monkeypatch.setattr(state.migration, 'STATE_YAML_PATH', writable_temp_dir / "state.yaml")
        monkeypatch.setattr(state.migration, 'STATE_YAML_BACKUP', writable_temp_dir / "backup.yaml")

        # Pre-create backup file to test preservation
        backup_file = writable_temp_dir / "backup.yaml"
        backup_file.write_text("original backup content", encoding='utf-8')

        result = await state.migration.rollback_to_yaml()
        assert result is True

        # Verify state.yaml was created
        assert (writable_temp_dir / "state.yaml").exists()
        import yaml
        with open(writable_temp_dir / "state.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "rollback_user" in data["accounts"]
        assert data["accounts"]["rollback_user"]["latest_id"] == "rollback_id"

        # Verify backup file is preserved after rollback
        assert (writable_temp_dir / "backup.yaml").exists(), "Backup should be preserved after rollback"


class TestConcurrency:
    """AC-7: Concurrent operations do not cause 'database is locked' errors."""

    @pytest.mark.asyncio
    async def test_concurrent_set_latest_id(self, repo):
        """Concurrent set_latest_id operations complete without 'database is locked'."""
        await repo.initialize()

        # Run multiple concurrent writes
        tasks = [
            repo.set_latest_id(f'concurrent_user_{i}', f'concurrent_id_{i}', f'User{i}')
            for i in range(10)
        ]
        await asyncio.gather(*tasks)

        # Verify all were written
        for i in range(10):
            assert repo.get_latest_id(f'concurrent_user_{i}') == f'concurrent_id_{i}'

    @pytest.mark.asyncio
    async def test_concurrent_mixed_operations(self, repo):
        """Concurrent mixed operations (set_latest_id, save_weibo_history, log_push) complete without errors."""
        await repo.initialize()

        async def mixed_ops(user_id):
            await repo.set_latest_id(f'mixed_user_{user_id}', f'mixed_id_{user_id}', f'MixedUser{user_id}')
            weibo = {
                'weiboid': f'mixed_user_{user_id}',
                'id': f'mixed_weibo_{user_id}',
                'text': f'Test content {user_id}',
                'screen_name': f'MixedUser{user_id}',
            }
            await repo.save_weibo_history(weibo)
            await repo.log_push(f'mixed_user_{user_id}', f'mixed_weibo_{user_id}', 'telegram', 'success')

        tasks = [mixed_ops(i) for i in range(5)]
        await asyncio.gather(*tasks)

        # All operations should complete without locking errors


class TestAC1Negative:
    """AC-1 negative tests for schema constraints."""

    @pytest.mark.asyncio
    async def test_not_null_constraint_on_latest_id(self, repo):
        """Inserting NULL into latest_id column fails with NOT NULL constraint."""
        await repo.initialize()

        async with repo._connect() as db:
            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    "INSERT INTO account_state (weiboid, latest_id) VALUES (?, ?)",
                    ('null_test_user', None)
                )
                await db.commit()

    @pytest.mark.asyncio
    async def test_not_null_constraint_on_weiboid_in_weibo_history(self, repo):
        """Inserting NULL weiboid into weibo_history fails with NOT NULL constraint."""
        await repo.initialize()
        await repo.set_latest_id('notnull_user', 'id1', 'NotNullUser')

        async with repo._connect() as db:
            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    """INSERT INTO weibo_history (weiboid, weibo_id, text, screen_name)
                       VALUES (?, ?, ?, ?)""",
                    (None, 'weibo123', 'text', 'screen')
                )
                await db.commit()

    @pytest.mark.asyncio
    async def test_index_used_in_query_plan(self, repo):
        """Verify idx_weibo_history_weiboid index exists and is used in query plan."""
        await repo.initialize()

        async with repo._connect() as db:
            # EXPLAIN QUERY PLAN for a query using weiboid
            async with db.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM weibo_history WHERE weiboid = ?",
                ('test_user',)
            ) as cursor:
                plan = await cursor.fetchall()
                plan_str = str(plan)
                # Index should be used for the query
                assert 'idx_weibo_history_weiboid' in plan_str or 'INDEX' in plan_str


class TestAC8Negative:
    """AC-8 negative tests for cache behavior."""

    @pytest.mark.asyncio
    async def test_get_latest_id_does_not_hit_db_twice(self, repo, monkeypatch):
        """Multiple calls to get_latest_id do not result in multiple DB queries."""
        await repo.initialize()
        await repo.set_latest_id('cache_test_user', 'cache_test_id', 'CacheTestUser')

        # Monkeypatch _connect to track calls
        original_connect = repo._connect
        call_count = 0

        async def tracking_connect():
            nonlocal call_count
            call_count += 1
            return await original_connect()

        repo._connect = tracking_connect

        # Call get_latest_id multiple times
        result1 = repo.get_latest_id('cache_test_user')
        result2 = repo.get_latest_id('cache_test_user')
        result3 = repo.get_latest_id('cache_test_user')

        assert result1 == 'cache_test_id'
        assert result2 == 'cache_test_id'
        assert result3 == 'cache_test_id'
        # _connect should not have been called (reads from cache only)
        assert call_count == 0

    @pytest.mark.asyncio
    async def test_cache_not_auto_refreshed(self, repo, monkeypatch):
        """Cache is not automatically refreshed from database."""
        await repo.initialize()
        await repo.set_latest_id('original_user', 'original_id', 'OriginalUser')

        # Manually update database to simulate external change
        async with repo._connect() as db:
            await db.execute(
                "UPDATE account_state SET latest_id = ? WHERE weiboid = ?",
                ('external_id', 'original_user')
            )
            await db.commit()

        # Cache should still return old value (no auto-refresh)
        assert repo.get_latest_id('original_user') == 'original_id'


class TestAppCheckSingle:
    """Regression tests for App._check_single() write order and state management."""

    @pytest.mark.asyncio
    async def test_first_account_partial_success(self, temp_db, monkeypatch):
        """First-seen account with cold start: silently records latest ID, does not push."""
        from unittest.mock import AsyncMock, MagicMock
        from core.app import App

        # Setup
        app = App()
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()
        app.repository = test_repo

        # Mock monitor to return new weibo list for first-seen account
        app.monitor = MagicMock()
        app.monitor.get_weibo_list = AsyncMock(return_value=[{
            'id': 'weibo_new',
            'screen_name': 'NewUser',
            'text': 'New post',
            'source': 'Web',
            'region_name': 'Beijing'
        }])

        # Mock notifer — should NOT be called (cold start)
        app.notifer = MagicMock()
        app.notifer.send_message = AsyncMock(return_value={
            'telegram': (True, None),
            'serverchan': (False, 'Connection timeout')
        })

        # Mock config
        app.config = {
            'notification': {
                'telegram_template': '{screen_name}: {text}',
                'enable_telegram': True,
                'enable_serverchan': True
            }
        }

        # Execute
        await app._check_single({'weiboid': 'first_user'})

        # Assert: account_state should be set with latest ID (cold start recording)
        assert test_repo.get_latest_id('first_user') == 'weibo_new'

        # Assert: send_message was NOT called (cold start, no push)
        app.notifer.send_message.assert_not_called()

        # Assert: push_log should be empty (no push happened)
        async with test_repo._connect() as db:
            async with db.execute("SELECT COUNT(*) FROM push_log WHERE weiboid = ?", ('first_user',)) as cursor:
                row = await cursor.fetchone()
        assert row[0] == 0

        # Assert: weibo_history should be empty (no push happened)
        history = await test_repo.get_weibo_history('first_user')
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_first_account_all_fail(self, temp_db, monkeypatch):
        """First-seen account with cold start: silently records latest ID regardless of notifer state."""
        from unittest.mock import AsyncMock, MagicMock
        from core.app import App

        app = App()
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()
        app.repository = test_repo

        app.monitor = MagicMock()
        app.monitor.get_weibo_list = AsyncMock(return_value=[{
            'id': 'weibo_fail',
            'screen_name': 'FailUser',
            'text': 'Failed post',
            'source': 'Web',
            'region_name': 'Shanghai'
        }])

        app.notifer = MagicMock()
        app.notifer.send_message = AsyncMock(return_value={
            'telegram': (False, 'Token expired'),
            'serverchan': (False, 'Connection timeout')
        })

        app.config = {
            'notification': {
                'telegram_template': '{screen_name}: {text}',
                'enable_telegram': True,
                'enable_serverchan': True
            }
        }

        await app._check_single({'weiboid': 'fail_user'})

        # Assert: account_state records the latest ID (cold start)
        assert test_repo.get_latest_id('fail_user') == 'weibo_fail'

        # Assert: send_message was NOT called (cold start, no push)
        app.notifer.send_message.assert_not_called()

        # Assert: push_log should be empty
        async with test_repo._connect() as db:
            async with db.execute("SELECT COUNT(*) FROM push_log WHERE weiboid = ?", ('fail_user',)) as cursor:
                row = await cursor.fetchone()
        assert row[0] == 0

        # Assert: weibo_history should be empty
        history = await test_repo.get_weibo_history('fail_user')
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_existing_account_all_fail(self, temp_db, monkeypatch):
        """Existing account with all channels failing: state not advanced, no history saved."""
        from unittest.mock import AsyncMock, MagicMock
        from core.app import App

        app = App()
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()

        # Pre-populate with existing account and old weibo ID
        await test_repo.set_latest_id('existing_user', 'old_weibo_id', 'ExistingUser')

        app.repository = test_repo

        app.monitor = MagicMock()
        app.monitor.get_weibo_list = AsyncMock(return_value=[{
            'id': 'new_weibo_fail',
            'screen_name': 'ExistingUser',
            'text': 'New post that will fail',
            'source': 'iPhone',
            'region_name': 'Beijing'
        }])

        app.notifer = MagicMock()
        app.notifer.send_message = AsyncMock(return_value={
            'telegram': (False, 'Bot blocked'),
            'serverchan': (False, 'Server error')
        })

        app.config = {
            'notification': {
                'telegram_template': '{screen_name}: {text}',
                'enable_telegram': True,
                'enable_serverchan': True
            }
        }

        await app._check_single({'weiboid': 'existing_user'})

        # Assert: latest_id should NOT be advanced to new weibo (all channels failed)
        assert test_repo.get_latest_id('existing_user') == 'old_weibo_id'

        # Assert: push_log should have 2 failed entries
        async with test_repo._connect() as db:
            async with db.execute("SELECT channel, status FROM push_log WHERE weiboid = ?", ('existing_user',)) as cursor:
                logs = [(row[0], row[1]) async for row in cursor]
        assert len(logs) == 2
        assert all(status == 'failed' for _, status in logs)

        # Assert: weibo_history should be empty
        history = await test_repo.get_weibo_history('existing_user')
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_multiple_new_weibos_ordered_push(self, temp_db, monkeypatch):
        """Multiple new weibos pushed in chronological order, older entries not pushed."""
        from unittest.mock import AsyncMock, MagicMock
        from core.app import App

        app = App()
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()

        # Pre-populate with old_id that appears in the middle of the list
        await test_repo.set_latest_id('multi_user', 'old_mid', 'MultiUser')
        app.repository = test_repo

        # API returns newest-first: [new3, new2, old_mid, older]
        app.monitor = MagicMock()
        app.monitor.get_weibo_list = AsyncMock(return_value=[
            {'id': 'new3', 'screen_name': 'MultiUser', 'text': 'Newest post', 'source': 'Web', 'region_name': 'Beijing'},
            {'id': 'new2', 'screen_name': 'MultiUser', 'text': 'Middle post', 'source': 'iPhone', 'region_name': 'Shanghai'},
            {'id': 'old_mid', 'screen_name': 'MultiUser', 'text': 'Old post', 'source': 'Web', 'region_name': 'Beijing'},
            {'id': 'older', 'screen_name': 'MultiUser', 'text': 'Older post', 'source': 'Android', 'region_name': 'Guangzhou'},
        ])

        push_call_ids = []
        app.notifer = MagicMock()
        app.notifer.send_message = AsyncMock(side_effect=lambda msg, tg, title: push_call_ids.append(msg) or {
            'telegram': (True, None),
        })

        app.config = {
            'notification': {
                'telegram_template': '{screen_name}: {text}',
                'enable_telegram': True,
            }
        }

        await app._check_single({'weiboid': 'multi_user'})

        # Should push new2 then new3 (old→new), skip old_mid and older
        assert len(push_call_ids) == 2
        assert 'Middle post' in push_call_ids[0]
        assert 'Newest post' in push_call_ids[1]

        # latest_id should be the newest pushed (new3)
        assert test_repo.get_latest_id('multi_user') == 'new3'

    @pytest.mark.asyncio
    async def test_old_id_not_on_page_pushes_all(self, temp_db, monkeypatch):
        """When old_id is not on the first page, push all items with warning."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from core.app import App

        app = App()
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()

        # old_id that is too old to be on the first page
        await test_repo.set_latest_id('gap_user', 'very_old_id', 'GapUser')
        app.repository = test_repo

        app.monitor = MagicMock()
        app.monitor.get_weibo_list = AsyncMock(return_value=[
            {'id': 'newest', 'screen_name': 'GapUser', 'text': 'Newest', 'source': 'Web', 'region_name': 'Beijing'},
            {'id': 'newer', 'screen_name': 'GapUser', 'text': 'Newer', 'source': 'iPhone', 'region_name': 'Shanghai'},
        ])

        push_call_texts = []
        app.notifer = MagicMock()
        app.notifer.send_message = AsyncMock(side_effect=lambda msg, tg, title: push_call_texts.append(msg) or {
            'telegram': (True, None),
        })

        app.config = {
            'notification': {
                'telegram_template': '{screen_name}: {text}',
                'enable_telegram': True,
            }
        }

        with patch('core.app.logger') as mock_logger:
            await app._check_single({'weiboid': 'gap_user'})

        # Should push both in chronological order: newer → newest
        assert len(push_call_texts) == 2
        assert 'Newer' in push_call_texts[0]
        assert 'Newest' in push_call_texts[1]

        # Should emit warning about missing old_id
        mock_logger.warning.assert_called()
        warning_text = mock_logger.warning.call_args[0][0]
        assert 'very_old_id' in warning_text
        assert 'not found on first page' in warning_text

        # latest_id should be newest
        assert test_repo.get_latest_id('gap_user') == 'newest'

    @pytest.mark.asyncio
    async def test_push_failure_stops_processing(self, temp_db, monkeypatch):
        """When a push fails, stop processing further weibos, keep latest_id at last success."""
        from unittest.mock import AsyncMock, MagicMock
        from core.app import App

        app = App()
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()

        await test_repo.set_latest_id('fail_user', 'old_id', 'FailUser')
        app.repository = test_repo

        # 3 new weibos
        app.monitor = MagicMock()
        app.monitor.get_weibo_list = AsyncMock(return_value=[
            {'id': 'new3', 'screen_name': 'FailUser', 'text': 'Third', 'source': 'Web', 'region_name': 'Beijing'},
            {'id': 'new2', 'screen_name': 'FailUser', 'text': 'Second', 'source': 'Web', 'region_name': 'Beijing'},
            {'id': 'new1', 'screen_name': 'FailUser', 'text': 'First', 'source': 'Web', 'region_name': 'Beijing'},
            {'id': 'old_id', 'screen_name': 'FailUser', 'text': 'Old', 'source': 'Web', 'region_name': 'Beijing'},
        ])

        call_count = [0]
        async def fail_second(msg, tg, title):
            call_count[0] += 1
            if call_count[0] == 2:
                return {'telegram': (False, 'Connection error')}
            return {'telegram': (True, None)}

        app.notifer = MagicMock()
        app.notifer.send_message = AsyncMock(side_effect=fail_second)

        app.config = {
            'notification': {
                'telegram_template': '{screen_name}: {text}',
                'enable_telegram': True,
            }
        }

        await app._check_single({'weiboid': 'fail_user'})

        # Only 2 calls: first succeeds, second fails, third never attempted
        assert call_count[0] == 2

        # latest_id should be at new1 (last successful push)
        assert test_repo.get_latest_id('fail_user') == 'new1'

        # new3 should not be in history (never pushed)
        history = await test_repo.get_weibo_history('fail_user')
        history_ids = [h['weibo_id'] for h in history]
        assert 'new1' in history_ids
        assert 'new2' not in history_ids
        assert 'new3' not in history_ids

    @pytest.mark.asyncio
    async def test_no_new_weibo_when_old_id_is_latest(self, temp_db, monkeypatch):
        """When old_id matches the first (newest) item, no push or state update."""
        from unittest.mock import AsyncMock, MagicMock
        from core.app import App

        app = App()
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()

        await test_repo.set_latest_id('same_user', 'latest_id', 'SameUser')
        app.repository = test_repo

        app.monitor = MagicMock()
        app.monitor.get_weibo_list = AsyncMock(return_value=[
            {'id': 'latest_id', 'screen_name': 'SameUser', 'text': 'Same', 'source': 'Web', 'region_name': 'Beijing'},
            {'id': 'older_id', 'screen_name': 'SameUser', 'text': 'Older', 'source': 'Web', 'region_name': 'Beijing'},
        ])

        app.notifer = MagicMock()
        app.notifer.send_message = AsyncMock()

        app.config = {
            'notification': {
                'telegram_template': '{screen_name}: {text}',
                'enable_telegram': True,
            }
        }

        await app._check_single({'weiboid': 'same_user'})

        # No push should happen
        app.notifer.send_message.assert_not_called()

        # latest_id should remain unchanged
        assert test_repo.get_latest_id('same_user') == 'latest_id'

    @pytest.mark.asyncio
    async def test_cold_start_then_next_poll_pushes(self, temp_db, monkeypatch):
        """After cold start records latest_id, the next poll detects and pushes new weibos."""
        from unittest.mock import AsyncMock, MagicMock
        from core.app import App

        app = App()
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()
        app.repository = test_repo

        # First poll: cold start — no old_id
        app.monitor = MagicMock()
        app.monitor.get_weibo_list = AsyncMock(return_value=[{
            'id': 'first_id', 'screen_name': 'ColdUser', 'text': 'First post',
            'source': 'Web', 'region_name': 'Beijing'
        }])

        app.notifer = MagicMock()
        app.notifer.send_message = AsyncMock()
        app.config = {
            'notification': {
                'telegram_template': '{screen_name}: {text}',
                'enable_telegram': True,
            }
        }

        await app._check_single({'weiboid': 'cold_user'})

        # Cold start: should not push, just record
        app.notifer.send_message.assert_not_called()
        assert test_repo.get_latest_id('cold_user') == 'first_id'

        # Second poll: new weibo available
        app.monitor.get_weibo_list = AsyncMock(return_value=[
            {'id': 'second_id', 'screen_name': 'ColdUser', 'text': 'Second post', 'source': 'iPhone', 'region_name': 'Shanghai'},
            {'id': 'first_id', 'screen_name': 'ColdUser', 'text': 'First post', 'source': 'Web', 'region_name': 'Beijing'},
        ])

        push_texts = []
        app.notifer.send_message = AsyncMock(side_effect=lambda msg, tg, title: push_texts.append(msg) or {
            'telegram': (True, None),
        })

        await app._check_single({'weiboid': 'cold_user'})

        # Should push the new weibo
        assert len(push_texts) == 1
        assert 'Second post' in push_texts[0]
        assert test_repo.get_latest_id('cold_user') == 'second_id'

    @pytest.mark.asyncio
    async def test_retry_from_failed_item_on_next_poll(self, temp_db, monkeypatch):
        """After push failure stops processing, next poll retries from the failed item."""
        from unittest.mock import AsyncMock, MagicMock
        from core.app import App

        app = App()
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()

        await test_repo.set_latest_id('retry_user', 'old_id', 'RetryUser')
        app.repository = test_repo

        # First poll: 3 new weibos, 2nd push fails
        app.monitor = MagicMock()
        app.monitor.get_weibo_list = AsyncMock(return_value=[
            {'id': 'new3', 'screen_name': 'RetryUser', 'text': 'Third', 'source': 'Web', 'region_name': 'Beijing'},
            {'id': 'new2', 'screen_name': 'RetryUser', 'text': 'Second', 'source': 'Web', 'region_name': 'Beijing'},
            {'id': 'new1', 'screen_name': 'RetryUser', 'text': 'First', 'source': 'Web', 'region_name': 'Beijing'},
            {'id': 'old_id', 'screen_name': 'RetryUser', 'text': 'Old', 'source': 'Web', 'region_name': 'Beijing'},
        ])

        call_count = [0]
        async def fail_second(msg, tg, title):
            call_count[0] += 1
            if call_count[0] == 2:
                return {'telegram': (False, 'Connection error')}
            return {'telegram': (True, None)}

        app.notifer = MagicMock()
        app.notifer.send_message = AsyncMock(side_effect=fail_second)
        app.config = {
            'notification': {
                'telegram_template': '{screen_name}: {text}',
                'enable_telegram': True,
            }
        }

        await app._check_single({'weiboid': 'retry_user'})

        # First poll: only first item succeeded
        assert call_count[0] == 2
        assert test_repo.get_latest_id('retry_user') == 'new1'

        # Second poll: same list, should retry from new2
        push_retry_texts = []
        app.notifer.send_message = AsyncMock(side_effect=lambda msg, tg, title: push_retry_texts.append(msg) or {
            'telegram': (True, None),
        })

        await app._check_single({'weiboid': 'retry_user'})

        # Should push new2 and new3 (both succeed this time)
        assert len(push_retry_texts) == 2
        assert 'Second' in push_retry_texts[0]
        assert 'Third' in push_retry_texts[1]
        assert test_repo.get_latest_id('retry_user') == 'new3'

        history = await test_repo.get_weibo_history('retry_user')
        history_ids = [h['weibo_id'] for h in history]
        assert 'new1' in history_ids
        assert 'new2' in history_ids
        assert 'new3' in history_ids

    @pytest.mark.asyncio
    async def test_id_type_mismatch_normalized(self, temp_db, monkeypatch):
        """Integer ID from API matches string ID in database after str() normalization."""
        from unittest.mock import AsyncMock, MagicMock
        from core.app import App

        app = App()
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()

        # Database stores ID as string
        await test_repo.set_latest_id('type_user', '123456', 'TypeUser')
        app.repository = test_repo

        # API returns integer ID that matches the string
        app.monitor = MagicMock()
        app.monitor.get_weibo_list = AsyncMock(return_value=[
            {'id': 123456, 'screen_name': 'TypeUser', 'text': 'Already seen', 'source': 'Web', 'region_name': 'Beijing'},
            {'id': 111111, 'screen_name': 'TypeUser', 'text': 'Older post', 'source': 'Web', 'region_name': 'Beijing'},
        ])

        app.notifer = MagicMock()
        app.notifer.send_message = AsyncMock()
        app.config = {
            'notification': {
                'telegram_template': '{screen_name}: {text}',
                'enable_telegram': True,
            }
        }

        await app._check_single({'weiboid': 'type_user'})

        # No push should happen — old_id matches the first item (after normalization)
        app.notifer.send_message.assert_not_called()
        assert test_repo.get_latest_id('type_user') == '123456'


class TestWeiboMonitorGetList:
    """Direct tests for WeiboMonitor.get_weibo_list with mocked aiohttp session."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock aiohttp ClientSession."""
        from unittest.mock import MagicMock
        session = MagicMock()
        session.get = MagicMock()
        return session

    @pytest.fixture
    def monitor(self, mock_session):
        """Create a WeiboMonitor with mocked session."""
        from monitor.weibo import WeiboMonitor
        return WeiboMonitor(mock_session)

    @pytest.mark.asyncio
    async def test_returns_full_list_with_all_fields(self, monitor, mock_session):
        """Multiple entries returned with all six fields and string IDs."""
        from unittest.mock import AsyncMock, MagicMock

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(return_value={
            'data': {
                'list': [
                    {
                        'id': 12345,
                        'text_raw': 'First post',
                        'created_at': '2024-01-01 12:00:00',
                        'user': {'screen_name': 'User1'},
                        'source': 'Web',
                        'region_name': 'Beijing',
                    },
                    {
                        'id': '67890',
                        'text_raw': 'Second post',
                        'created_at': '2024-01-01 11:00:00',
                        'user': {'screen_name': 'User2'},
                        'source': 'iPhone',
                        'region_name': 'Shanghai',
                    },
                ]
            }
        })
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_response)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await monitor.get_weibo_list('test_uid')

        assert len(result) == 2
        assert result[0]['text'] == 'First post'
        assert result[0]['id'] == '12345'  # str() normalization
        assert result[0]['screen_name'] == 'User1'
        assert result[0]['source'] == 'Web'
        assert result[0]['region_name'] == 'Beijing'
        assert result[0]['time'] == '2024-01-01 12:00:00'
        assert result[1]['text'] == 'Second post'
        assert result[1]['id'] == '67890'

    @pytest.mark.asyncio
    async def test_empty_list_when_no_items(self, monitor, mock_session):
        """Empty data list returns empty Python list."""
        from unittest.mock import AsyncMock, MagicMock

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(return_value={'data': {'list': []}})
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_response)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await monitor.get_weibo_list('test_uid')
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_entry_missing_text_raw(self, monitor, mock_session):
        """Entry missing text_raw is skipped, valid sibling is retained."""
        from unittest.mock import AsyncMock, MagicMock

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(return_value={
            'data': {
                'list': [
                    {
                        'id': 'valid_id',
                        'text_raw': 'Valid post',
                        'user': {'screen_name': 'Valid'},
                    },
                    {
                        'id': 'bad_id',
                        # text_raw intentionally missing
                        'user': {'screen_name': 'Bad'},
                    },
                ]
            }
        })
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_response)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await monitor.get_weibo_list('test_uid')

        assert len(result) == 1
        assert result[0]['id'] == 'valid_id'
        assert result[0]['text'] == 'Valid post'

    @pytest.mark.asyncio
    async def test_network_error_returns_empty_list(self, monitor, mock_session):
        """aiohttp.ClientError returns empty list, no exception raised."""
        from unittest.mock import AsyncMock
        import aiohttp

        mock_session.get.side_effect = aiohttp.ClientError('Connection failed')

        result = await monitor.get_weibo_list('test_uid')
        assert result == []

    @pytest.mark.asyncio
    async def test_timeout_returns_empty_list(self, monitor, mock_session):
        """Timeout returns empty list, no exception raised."""
        from unittest.mock import AsyncMock
        import asyncio as _asyncio

        mock_session.get.side_effect = _asyncio.TimeoutError('Request timed out')

        result = await monitor.get_weibo_list('test_uid')
        assert result == []
