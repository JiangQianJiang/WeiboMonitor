"""Tests for notifer fixes: concurrent push and no double-escaping."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notifer.notifer import Notifer, escape_markdown_v2


class TestMarkdownV2Escaping:
    """Verify single-escaping (no double-escaping) in telegram_send."""

    @pytest.mark.asyncio
    async def test_telegram_send_no_double_escape(self):
        """telegram_send should NOT escape message again (caller already escaped)."""
        session = MagicMock()
        config = {
            'tgbottoken': 'fake_token',
            'chatid': '123456',
            'enable_telegram': True,
            'enable_serverchan': False
        }
        notifer = Notifer(session, config)

        # Pre-escaped message (as app.py would send it)
        pre_escaped = r"*TestUser* 发表了新微博：\nHello\_World"

        with patch('telegram.Bot') as mock_bot_class:
            mock_bot = AsyncMock()
            mock_bot_class.return_value = mock_bot
            mock_bot.send_message = AsyncMock()

            await notifer.telegram_send(pre_escaped)

            # Verify send_message was called with the SAME pre-escaped text (no re-escaping)
            mock_bot.send_message.assert_called_once()
            call_args = mock_bot.send_message.call_args
            assert call_args.kwargs['text'] == pre_escaped
            # Should NOT be double-escaped like: r"\*TestUser\* ... Hello\\_World"

    def test_escape_markdown_v2_basic(self):
        """Verify escape_markdown_v2 escapes special chars correctly."""
        assert escape_markdown_v2("hello_world") == r"hello\_world"
        assert escape_markdown_v2("test*bold*") == r"test\*bold\*"
        assert escape_markdown_v2("[link](url)") == r"\[link\]\(url\)"


class TestConcurrentPush:
    """Verify send_message uses asyncio.gather for concurrent push."""

    @pytest.mark.asyncio
    async def test_concurrent_push_both_succeed(self):
        """Both channels pushed concurrently, both succeed."""
        session = MagicMock()
        config = {
            'tgbottoken': 'fake_token',
            'chatid': '123456',
            'sendkey': 'fake_sendkey',
            'enable_telegram': True,
            'enable_serverchan': True
        }
        notifer = Notifer(session, config)

        # Mock both methods to track call order
        call_order = []

        async def mock_telegram(msg):
            call_order.append('telegram_start')
            await asyncio.sleep(0.1)  # Simulate network delay
            call_order.append('telegram_end')

        async def mock_serverchan(msg, title):
            call_order.append('serverchan_start')
            await asyncio.sleep(0.1)  # Simulate network delay
            call_order.append('serverchan_end')

        notifer.telegram_send = mock_telegram
        notifer.ms_send = mock_serverchan

        results = await notifer.send_message("test", "test_tg", "title")

        # Both should succeed
        assert results['telegram'] == (True, None)
        assert results['serverchan'] == (True, None)

        # Verify concurrent execution: both should start before either ends
        assert call_order.index('telegram_start') < call_order.index('serverchan_end')
        assert call_order.index('serverchan_start') < call_order.index('telegram_end')

    @pytest.mark.asyncio
    async def test_concurrent_push_partial_failure(self):
        """One channel fails, other succeeds, both pushed concurrently."""
        session = MagicMock()
        config = {
            'tgbottoken': 'fake_token',
            'chatid': '123456',
            'sendkey': 'fake_sendkey',
            'enable_telegram': True,
            'enable_serverchan': True
        }
        notifer = Notifer(session, config)

        async def mock_telegram(msg):
            raise Exception("Telegram API error")

        async def mock_serverchan(msg, title):
            pass  # Success

        notifer.telegram_send = mock_telegram
        notifer.ms_send = mock_serverchan

        results = await notifer.send_message("test", "test_tg", "title")

        # Telegram failed, serverchan succeeded
        assert results['telegram'] == (False, "Telegram API error")
        assert results['serverchan'] == (True, None)

    @pytest.mark.asyncio
    async def test_concurrent_push_both_fail(self):
        """Both channels fail, both errors captured."""
        session = MagicMock()
        config = {
            'tgbottoken': 'fake_token',
            'chatid': '123456',
            'sendkey': 'fake_sendkey',
            'enable_telegram': True,
            'enable_serverchan': True
        }
        notifer = Notifer(session, config)

        async def mock_telegram(msg):
            raise Exception("Telegram error")

        async def mock_serverchan(msg, title):
            raise Exception("Serverchan error")

        notifer.telegram_send = mock_telegram
        notifer.ms_send = mock_serverchan

        results = await notifer.send_message("test", "test_tg", "title")

        # Both failed
        assert results['telegram'] == (False, "Telegram error")
        assert results['serverchan'] == (False, "Serverchan error")
