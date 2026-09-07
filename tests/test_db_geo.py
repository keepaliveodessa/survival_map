"""Tests for core/db/db_geo.py — GeoOperations."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from core.db.db_geo import GeoOperations
    _IMPORT_OK = True
    _IMPORT_ERR = None
except Exception as e:
    _IMPORT_OK = False
    _IMPORT_ERR = repr(e)

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK, reason=f"db_geo import unavailable: {_IMPORT_ERR}"
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
    conn.fetchval = AsyncMock(return_value=None)
    return conn


class FakeDb:
    def __init__(self):
        self.pool = MagicMock()
        self.pool.acquire = MagicMock(return_value=_AsyncCtx(_mock_connection()))
        self.fetchval = AsyncMock(return_value=None)


# ============================================================
# GeoOperations
# ============================================================

class TestGeoOperations:
    @pytest.fixture
    def ops(self):
        return GeoOperations(db=FakeDb())

    @pytest.mark.asyncio
    async def test_get_geo_count_returns_count(self, ops):
        ops.db.fetchval = AsyncMock(return_value=42)
        result = await ops.get_geo_count()
        assert result == 42

    @pytest.mark.asyncio
    async def test_get_geo_count_returns_zero_on_error(self, ops):
        ops.db.fetchval = AsyncMock(side_effect=Exception("DB error"))
        result = await ops.get_geo_count()
        assert result == 0

    @pytest.mark.asyncio
    async def test_get_all_geo_as_geojson_returns_feature_collection(self, ops):
        geojson = '{"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": null, "properties": {"name": "Test", "id": 1}}]}'
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchval = AsyncMock(return_value=geojson)
        result = await ops.get_all_geo_as_geojson()
        assert json.loads(result)["type"] == "FeatureCollection"

    @pytest.mark.asyncio
    async def test_get_all_geo_as_geojson_returns_empty_fallback_on_error(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchval = AsyncMock(side_effect=Exception("DB error"))
        result = await ops.get_all_geo_as_geojson()
        assert json.loads(result) == {"type": "FeatureCollection", "features": []}

    @pytest.mark.asyncio
    async def test_get_all_geo_as_geojson_returns_empty_fallback_when_none(self, ops):
        ctx = ops.db.pool.acquire.return_value
        ctx.conn.fetchval = AsyncMock(return_value=None)
        result = await ops.get_all_geo_as_geojson()
        assert json.loads(result) == {"type": "FeatureCollection", "features": []}
