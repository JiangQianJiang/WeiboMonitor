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
        async with aiosqlite.connect(repo._db_path) as db:
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


class TestStateUpdateTiming:
    """AC-2: State update timing preserves 'push-then-update' guarantee."""

    @pytest.mark.asyncio
    async def test_push_success_updates_both(self, repo):
        """When push succeeds, latest_id is updated in both cache and DB."""
        await repo.initialize()

        await repo.set_latest_id('user1', 'id001', 'User1')

        assert repo.get_latest_id('user1') == 'id001'

        async with aiosqlite.connect(repo._db_path) as db:
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

        async with aiosqlite.connect(repo._db_path) as db:
            async with db.execute(
                "SELECT latest_id FROM account_state WHERE weiboid = ?", ('user2',)
            ) as cursor:
                row = await cursor.fetchone()
                assert row[0] == 'old_id'


class TestPerChannelLogging:
    """AC-4: Per-channel push logging captures partial failures."""

    @pytest.mark.asyncio
    async def test_both_channels_success(self, repo):
        """When both channels succeed, two success records logged."""
        await repo.initialize()
        await repo.log_push('user1', 'w001', 'telegram', 'success')
        await repo.log_push('user1', 'w001', 'serverchan', 'success')

        stats = await repo.get_push_stats(days=1)
        assert stats.get('telegram_success', 0) == 1
        assert stats.get('serverchan_success', 0) == 1

    @pytest.mark.asyncio
    async def test_partial_failure_logged(self, repo):
        """When one channel fails, one success and one failed record."""
        await repo.initialize()
        await repo.log_push('user1', 'w002', 'telegram', 'success')
        await repo.log_push('user1', 'w002', 'serverchan', 'failed', 'Connection timeout')

        stats = await repo.get_push_stats(days=1)
        assert stats.get('telegram_success', 0) == 1
        assert stats.get('serverchan_failed', 0) == 1

    @pytest.mark.asyncio
    async def test_error_message_captured(self, repo):
        """Error messages captured in push_log.error_message column."""
        await repo.initialize()
        await repo.log_push('user1', 'w003', 'telegram', 'failed', 'Invalid token')

        async with aiosqlite.connect(repo._db_path) as db:
            async with db.execute(
                "SELECT error_message FROM push_log WHERE weibo_id = ? AND channel = ?",
                ('w003', 'telegram')
            ) as cursor:
                row = await cursor.fetchone()
                assert row[0] == 'Invalid token'