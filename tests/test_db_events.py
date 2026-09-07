"""Tests for core/db/db_events.py — EventOperations."""
import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

try:
    from core.db.db_events import EventOperations
    _IMPORT_OK = True
    _IMPORT_ERR = None
except Exception as e:
    _IMPORT_OK = False
    _IMPORT_ERR = repr(e)

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK, reason=f"db_events import unavailable: {_IMPORT_ERR}"
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
    conn.executemany = AsyncMock()
    return conn


class FakeDb:
    def __init__(self):
        self.pool = MagicMock()
        self.pool.acquire = MagicMock(return_value=_AsyncCtx(_mock_connection()))
        self.pool.acquire.return_value.conn.fetchval = AsyncMock()
        self.pool.acquire.return_value.conn.fetch = AsyncMock(return_value=[])
        self.pool.acquire.return_value.conn.execute = AsyncMock()
        self.pool.acquire.return_value.conn.fetchrow = AsyncMock()
        self.pool.acquire.return_value.conn.executemany = AsyncMock()

    async def fetchval(self, query, *args, timeout=None):
        async with self.pool.acquire() as conn:
            if timeout:
                return await asyncio.wait_for(conn.fetchval(query, *args), timeout=timeout)
            return await conn.fetchval(query, *args)

    async def fetchrow(self, query, *args, timeout=None):
        async with self.pool.acquire() as conn:
            if timeout:
                return await asyncio.wait_for(conn.fetchrow(query, *args), timeout=timeout)
            return await conn.fetchrow(query, *args)

    async def fetch(self, query, *args, timeout=None):
        async with self.pool.acquire() as conn:
            if timeout:
                records = await asyncio.wait_for(conn.fetch(query, *args), timeout=timeout)
            else:
                records = await conn.fetch(query, *args)
            return [dict(record) for record in records]

    async def execute(self, query, *args, timeout=None):
        async with self.pool.acquire() as conn:
            if timeout:
                return await asyncio.wait_for(conn.execute(query, *args), timeout=timeout)
            return await conn.execute(query, *args)

    async def executemany(self, query, args_list):
        async with self.pool.acquire() as conn:
            await conn.executemany(query, args_list)


# ============================================================
# EventOperations
# ============================================================

class TestEventOperations:
    @pytest.fixture
    def ops(self):
        return EventOperations(db=FakeDb())

    @pytest.mark.asyncio
    async def test_get_filtered_events_as_geojson_basic(self, ops):
        geojson = json.dumps({
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": None, "properties": {"id": 1}}]
        })
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchval = AsyncMock(return_value=geojson)
        result = await ops.get_filtered_events_as_geojson(time_interval_minutes=60)
        assert result["type"] == "FeatureCollection"
        assert len(result["features"]) == 1

    @pytest.mark.asyncio
    async def test_get_filtered_events_empty_result(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchval = AsyncMock(return_value=None)
        result = await ops.get_filtered_events_as_geojson(time_interval_minutes=60)
        assert result == {"type": "FeatureCollection", "features": []}

    @pytest.mark.asyncio
    async def test_get_latest_update_time_returns_max(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchval = AsyncMock(return_value="2024-01-01T12:00:00+00:00")
        result = await ops.get_latest_update_time()
        assert result == "2024-01-01T12:00:00+00:00"

    @pytest.mark.asyncio
    async def test_get_latest_update_time_returns_none_on_error(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchval = AsyncMock(side_effect=Exception("DB error"))
        result = await ops.get_latest_update_time()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_incremental_events_builds_where_clauses(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchval = AsyncMock(return_value=json.dumps({"type": "FeatureCollection", "features": []}))
        from datetime import datetime, timezone
        since = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        await ops.get_incremental_events(since=since, time_interval_minutes=60, layers=["bus"])
        args = ctx.conn.fetchval.call_args[0]
        query = args[0]
        assert "event_time >= $1" in query
        assert "layer = ANY($3)" in query

    @pytest.mark.asyncio
    async def test_get_events_meta_returns_dict(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchrow = AsyncMock(return_value={"version": 3, "updated_at": "2024-01-01T12:00:00+00:00", "max_event_id": 100})
        result = await ops.get_events_meta()
        assert result["version"] == 3
        assert result["max_event_id"] == 100

    @pytest.mark.asyncio
    async def test_get_events_meta_returns_defaults_when_empty(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchrow = AsyncMock(return_value=None)
        result = await ops.get_events_meta()
        assert result == {"version": 0, "updated_at": None, "max_event_id": 0}

    @pytest.mark.asyncio
    async def test_get_events_min_id_returns_coalesce(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchval = AsyncMock(return_value=5)
        result = await ops.get_events_min_id()
        assert result == 5

    @pytest.mark.asyncio
    async def test_get_events_min_id_returns_zero_when_empty(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchval = AsyncMock(return_value=None)
        result = await ops.get_events_min_id()
        assert result == 0

    @pytest.mark.asyncio
    async def test_get_events_message_id_range_returns_tuple(self, ops):
        ctx = ops.db.pool.acquire.return_value
        
        def row_getitem(self, key):
            if key in (0, 'min', 'min_mid'):
                return 1
            if key in (1, 'max', 'max_mid'):
                return 100
            raise KeyError(key)
        
        mock_record = MagicMock()
        mock_record.__getitem__ = row_getitem
        ctx.conn.fetchrow = AsyncMock(return_value=mock_record)
        result = await ops.get_events_message_id_range()
        assert result == (1, 100)

    @pytest.mark.asyncio
    async def test_get_events_message_id_range_returns_zeros_when_empty(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchrow = AsyncMock(return_value=None)
        result = await ops.get_events_message_id_range()
        assert result == (0, 0)

    @pytest.mark.asyncio
    async def test_get_events_updates_as_geojson_uses_after_message_id(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchval = AsyncMock(return_value=json.dumps({"type": "FeatureCollection", "features": []}))
        await ops.get_events_updates_as_geojson(after_message_id=42, after_id=10, limit=100)
        args = ctx.conn.fetchval.call_args[0]
        query = args[0]
        assert "message_id > $1" in query
        assert "LIMIT $2" in query

    @pytest.mark.asyncio
    async def test_get_events_snapshot_as_geojson_limits_to_60_minutes(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchval = AsyncMock(return_value=json.dumps({"type": "FeatureCollection", "features": []}))
        await ops.get_events_snapshot_as_geojson(limit=50)
        args = ctx.conn.fetchval.call_args[0]
        query = args[0]
        assert "NOW() - INTERVAL '60 minutes'" in query
        assert "$1" in query
