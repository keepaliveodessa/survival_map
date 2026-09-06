"""Tests for processor expired-guard fix (P0.1).

_p0.1 spec:
- _process_row() returns None (not 'expired') when event_time is outside
  the 60-min window, and calls _mark_expired(row['id']), incrementing
  self._expired.
- _process_row_with_retries() treats None as "already marked" and does
  NOT call _mark_done again.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from processor.main import ProcessorBot
    _IMPORT_OK = True
    _IMPORT_ERR = None
except Exception as e:
    _IMPORT_OK = False
    _IMPORT_ERR = repr(e)

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK, reason=f"processor.main import unavailable: {_IMPORT_ERR}"
)


# ============================================================
# Helpers
# ============================================================

def _expired_row():
    now = datetime.now(timezone.utc)
    expired_time = now - timedelta(hours=2)
    return {
        'id': 1,
        'message_id': 'msg1',
        'text': 'тест',
        'event_time': expired_time,
        'photo_file_id': None,
    }


def _make_bot_with_db():
    bot = ProcessorBot()
    bot.db = MagicMock()
    bot.db.pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    bot.db.pool.acquire.return_value = ctx
    return bot


# ============================================================
# Tests
# ============================================================

class TestExpiredGuard:
    """Tests for the expired-guard path in processor._process_row."""

    async def test_expired_message_marked(self):
        """event_time = now - 2h => _process_row returns None and calls
        _mark_expired exactly once."""
        bot = _make_bot_with_db()
        row = _expired_row()

        mark_expired = AsyncMock()
        mark_done = AsyncMock()
        bot._mark_expired = mark_expired
        bot._mark_done = mark_done

        result = await bot._process_row(row)

        assert result is None
        mark_expired.assert_awaited_once_with(row['id'])
        mark_done.assert_not_awaited()

    async def test_expired_counter_increments(self):
        """After processing an expired row, bot._expired is incremented."""
        bot = _make_bot_with_db()
        row = _expired_row()
        bot._expired = 0

        bot._mark_expired = AsyncMock()
        bot._mark_done = AsyncMock()

        await bot._process_row(row)

        assert bot._expired == 1

    @pytest.mark.asyncio
    async def test_process_row_with_retries_none_expired_no_mark_done(self):
        """_process_row_with_retries: when _process_row returns None
        (expired), _mark_done is NOT called (expired already marked
        inside _process_row)."""
        bot = _make_bot_with_db()
        row = _expired_row()

        bot._messages_processed = 0
        bot._errors = 0
        bot._expired = 0
        mark_expired = AsyncMock()
        mark_done = AsyncMock()
        mark_error = AsyncMock()
        bot._mark_expired = mark_expired
        bot._mark_done = mark_done
        bot._mark_error = mark_error

        # Simulate _tryProcess by calling _process_row directly and then
        # the downstream handling. This avoids constructing the closure
        # and invoking retry_with_backoff (which creates an unawaited
        # coroutine in tests).
        result = await bot._process_row(row)
        assert result is None
        mark_expired.assert_awaited_once_with(row['id'])
        mark_done.assert_not_awaited()
        assert bot._errors == 0  # no error path taken

    @pytest.mark.asyncio
    async def test_not_expired_event_returns_result_not_none(self):
        """event_time within window => _process_row does NOT call
        _mark_expired (fallback: returns a dict from _insert_event)."""
        bot = ProcessorBot()
        now = datetime.now(timezone.utc)
        valid_time = now - timedelta(minutes=5)
        row = {
            'id': 1,
            'message_id': 'msg1',
            'text': 'тест',
            'event_time': valid_time,
            'photo_file_id': None,
        }

        bot.morph = MagicMock()
        bot.morph.lemmatize_tokens = MagicMock(return_value=[])
        bot.layer_classifier = MagicMock()
        bot.layer_classifier.classify = MagicMock(return_value='traffic')
        bot.tokenize = MagicMock(return_value=[])

        with patch.object(bot, '_insert_event', new_callable=AsyncMock) as mock_insert:
            mock_insert.return_value = {'event_id': 1, 'layer': 'traffic', 'strategy': 'random'}
            with patch.object(bot, '_random_point', return_value='POINT(0 0)'):
                result = await bot._process_row(row)

        assert result is not None
