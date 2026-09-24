"""Tests for common/db_adapter.py — DBAdapter (shared by parser & nlp_processor)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from common.db_adapter import DBAdapter
    _IMPORT_OK = True
    _IMPORT_ERR = None
except Exception as e:
    _IMPORT_OK = False
    _IMPORT_ERR = repr(e)

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK, reason=f"db_adapter import unavailable: {_IMPORT_ERR}"
)


# ============================================================
# Helpers
# ============================================================

class _AsyncCtx:
    def __init__(self, conn):
        self.conn = conn
    async def __aenter__(self):
        return self.conn
    async def __aexit__(self, exc_type, exc, tb):
        return None


def _mock_connection():
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    return conn


def _mock_pool():
    pool = MagicMock()
    pool.close = AsyncMock()
    pool.terminate = AsyncMock()
    conn = _mock_connection()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    return pool, conn


# ============================================================
# DBAdapter
# ============================================================

class TestDBAdapter:
    @pytest.mark.asyncio
    async def test_connect_retries_on_connection_error(self):
        adapter = DBAdapter()
        mock_pool, _ = _mock_pool()
        side_effects = [ConnectionRefusedError(), ConnectionRefusedError(), mock_pool]
        with patch('common.db_adapter.create_pool', side_effect=side_effects) as mock_create:
            with patch('asyncio.sleep', AsyncMock()):
                result = await adapter.connect(max_retries=3, retry_delay=0.1)
        assert result is True
        assert adapter._DBAdapter__pool is mock_pool
        assert mock_create.call_count == 3

    @pytest.mark.asyncio
    async def test_connect_validates_with_select_1(self):
        adapter = DBAdapter()
        mock_pool, mock_conn = _mock_pool()
        mock_conn.fetchval = AsyncMock(return_value=1)
        with patch('common.db_adapter.create_pool', return_value=mock_pool):
            result = await adapter.connect(max_retries=2, retry_delay=0.1)
        assert result is True
        mock_conn.fetchval.assert_called_once_with("SELECT 1")

    @pytest.mark.asyncio
    async def test_ensure_schema_adds_message_id_column(self):
        adapter = DBAdapter()
        mock_pool, mock_conn = _mock_pool()
        adapter._DBAdapter__pool = mock_pool
        await adapter.ensure_schema()
        calls = [c[0][0] for c in mock_conn.execute.call_args_list]
        assert any("ALTER TABLE events ADD COLUMN IF NOT EXISTS message_id BIGINT" in sql for sql in calls)

    @pytest.mark.asyncio
    async def test_ensure_schema_creates_unique_index(self):
        adapter = DBAdapter()
        mock_pool, mock_conn = _mock_pool()
        adapter._DBAdapter__pool = mock_pool
        await adapter.ensure_schema()
        calls = [c[0][0] for c in mock_conn.execute.call_args_list]
        assert any("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_message_id_unique" in sql for sql in calls)

    @pytest.mark.asyncio
    async def test_ensure_schema_migrates_photo_url(self):
        adapter = DBAdapter()
        mock_pool, mock_conn = _mock_pool()
        mock_conn.execute = AsyncMock(return_value="UPDATE 3")
        adapter._DBAdapter__pool = mock_pool
        await adapter.ensure_schema()
        calls = [c[0][0] for c in mock_conn.execute.call_args_list]
        assert any("UPDATE events" in sql and "photo_url" in sql for sql in calls)

    @pytest.mark.asyncio
    async def test_ensure_schema_idempotent(self):
        adapter = DBAdapter()
        mock_pool, mock_conn = _mock_pool()
        mock_conn.execute = AsyncMock(return_value="UPDATE 0")
        adapter._DBAdapter__pool = mock_pool
        await adapter.ensure_schema()
        await adapter.ensure_schema()
        assert mock_conn.execute.call_count == 6  # ALTER + INDEX + UPDATE (вызваны дважды)

    @pytest.mark.asyncio
    async def test_close_closes_pool(self):
        adapter = DBAdapter()
        mock_pool, _ = _mock_pool()
        adapter._DBAdapter__pool = mock_pool
        await adapter.close()
        mock_pool.close.assert_called_once()
        assert adapter._DBAdapter__pool is None

    def test_pool_property_returns_internal_pool(self):
        adapter = DBAdapter()
        mock_pool, _ = _mock_pool()
        adapter._DBAdapter__pool = mock_pool
        assert adapter.pool is mock_pool
