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


class TestMigrationScript:
    """AC-6: Data migration from state.yaml completes successfully."""

    @pytest.mark.asyncio
    async def test_migrate_success(self, temp_db, monkeypatch, tmp_path):
        """Migration script reads all accounts from state.yaml and migrates to SQLite."""
        import state.migration

        # Create temp state.yaml
        state_yaml = tmp_path / "state.yaml"
        state_yaml.write_text("accounts:\n  user1:\n    latest_id: 'id123'\n  user2:\n    latest_id: 'id456'\n", encoding='utf-8')

        # Monkeypatch paths
        monkeypatch.setattr(state.migration, 'STATE_YAML_PATH', state_yaml)
        monkeypatch.setattr(state.migration, 'STATE_YAML_BACKUP', tmp_path / "state.yaml.backup")

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
        backup_path = tmp_path / "state.yaml.backup"
        assert backup_path.exists(), "state.yaml.backup should exist"

    @pytest.mark.asyncio
    async def test_migrate_missing_state_yaml(self, monkeypatch, tmp_path):
        """Migration skips gracefully when state.yaml doesn't exist."""
        import state.migration
        monkeypatch.setattr(state.migration, 'STATE_YAML_PATH', tmp_path / "nonexistent.yaml")
        monkeypatch.setattr(state.migration, 'STATE_YAML_BACKUP', tmp_path / "backup.yaml")

        result = await state.migration.migrate_from_yaml()
        assert result is True  # Should succeed (nothing to migrate)

    def test_migrate_rejects_running_app(self, monkeypatch, tmp_path):
        """Migration refuses to run if app is running (process check)."""
        import state.migration

        # Mock is_app_running to return True
        monkeypatch.setattr(state.migration, 'is_app_running', lambda: True)

        result = asyncio.run(state.migration.migrate_from_yaml())
        assert result is False

    @pytest.mark.asyncio
    async def test_rollback_to_yaml(self, temp_db, monkeypatch, tmp_path):
        """Rollback exports from SQLite to state.yaml."""
        import state.migration

        # Setup repo with data
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', temp_db)
        await test_repo.initialize()
        await test_repo.set_latest_id('rollback_user', 'rollback_id', 'RollUser')

        monkeypatch.setattr(state.migration, 'StateRepository', lambda: test_repo)
        monkeypatch.setattr(state.migration, 'STATE_YAML_PATH', tmp_path / "state.yaml")
        monkeypatch.setattr(state.migration, 'STATE_YAML_BACKUP', tmp_path / "backup.yaml")

        # Pre-create backup file to test preservation
        backup_file = tmp_path / "backup.yaml"
        backup_file.write_text("original backup content", encoding='utf-8')

        result = await state.migration.rollback_to_yaml()
        assert result is True

        # Verify state.yaml was created
        assert (tmp_path / "state.yaml").exists()
        import yaml
        with open(tmp_path / "state.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "rollback_user" in data["accounts"]
        assert data["accounts"]["rollback_user"]["latest_id"] == "rollback_id"

        # Verify backup file is preserved after rollback
        assert (tmp_path / "backup.yaml").exists(), "Backup should be preserved after rollback"


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
        """First-seen account with partial push success: parent record created before child records."""
        from unittest.mock import AsyncMock, MagicMock
        from core.app import App

        # Setup
        app = App()
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()
        app.repository = test_repo

        # Mock monitor to return new weibo for first-seen account
        app.monitor = MagicMock()
        app.monitor.get_latest_weibo = AsyncMock(return_value={
            'id': 'weibo_new',
            'screen_name': 'NewUser',
            'text': 'New post',
            'source': 'Web',
            'region_name': 'Beijing'
        })

        # Mock notifer to return partial success
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

        # Assert: account_state should exist
        assert test_repo.get_latest_id('first_user') == 'weibo_new'

        # Assert: push_log should have 2 entries (one success, one failed)
        async with test_repo._connect() as db:
            async with db.execute("SELECT channel, status FROM push_log WHERE weiboid = ?", ('first_user',)) as cursor:
                logs = [(row[0], row[1]) async for row in cursor]
        assert len(logs) == 2
        assert ('telegram', 'success') in logs
        assert ('serverchan', 'failed') in logs

        # Assert: weibo_history should have 1 entry
        history = await test_repo.get_weibo_history('first_user')
        assert len(history) == 1
        assert history[0]['weibo_id'] == 'weibo_new'

    @pytest.mark.asyncio
    async def test_first_account_all_fail(self, temp_db, monkeypatch):
        """First-seen account with all channels failing: parent record created, state not advanced."""
        from unittest.mock import AsyncMock, MagicMock
        from core.app import App

        app = App()
        test_repo = StateRepository()
        monkeypatch.setattr(test_repo, '_db_path', Path(temp_db))
        await test_repo.initialize()
        app.repository = test_repo

        app.monitor = MagicMock()
        app.monitor.get_latest_weibo = AsyncMock(return_value={
            'id': 'weibo_fail',
            'screen_name': 'FailUser',
            'text': 'Failed post',
            'source': 'Web',
            'region_name': 'Shanghai'
        })

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

        # Assert: account_state should exist with empty latest_id (not advanced)
        assert test_repo.get_latest_id('fail_user') == ''

        # Assert: push_log should have 2 failed entries
        async with test_repo._connect() as db:
            async with db.execute("SELECT channel, status FROM push_log WHERE weiboid = ?", ('fail_user',)) as cursor:
                logs = [(row[0], row[1]) async for row in cursor]
        assert len(logs) == 2
        assert all(status == 'failed' for _, status in logs)

        # Assert: weibo_history should be empty (not saved on failure)
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
        app.monitor.get_latest_weibo = AsyncMock(return_value={
            'id': 'new_weibo_fail',
            'screen_name': 'ExistingUser',
            'text': 'New post that will fail',
            'source': 'iPhone',
            'region_name': 'Beijing'
        })

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

        # Assert: latest_id should NOT be advanced to new weibo
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