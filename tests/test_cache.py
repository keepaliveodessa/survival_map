"""Tests for core/utils/cache.py — CacheEntry + CacheManager."""
import asyncio

import pytest
from freezegun import freeze_time

from conftest import load_module_by_path

cache_mod = load_module_by_path("_cache_under_test", "core/utils/cache.py")
CacheEntry = cache_mod.CacheEntry
CacheManager = cache_mod.CacheManager


# ============================================================
# CacheEntry
# ============================================================

class TestCacheEntry:
    def test_is_expired_false_before_ttl(self):
        with freeze_time("2024-01-01 00:00:00") as frozen:
            entry = CacheEntry(value="x", ttl_seconds=60)
            assert entry.is_expired() is False

    def test_is_expired_true_after_ttl(self):
        with freeze_time("2024-01-01 00:00:00") as frozen:
            entry = CacheEntry(value="x", ttl_seconds=60)
            frozen.move_to("2024-01-01 00:01:01")
            assert entry.is_expired() is True

    def test_touch_updates_last_accessed(self):
        with freeze_time("2024-01-01 00:00:00") as frozen:
            entry = CacheEntry(value="x", ttl_seconds=60)
            frozen.move_to("2024-01-01 00:00:10")
            entry.touch()
            frozen.move_to("2024-01-01 00:01:00")
            # 50s after touch, still within original 60s TTL
            assert entry.is_expired() is False


# ============================================================
# CacheManager — sync helpers
# ============================================================

class TestCacheManagerHelpers:
    def test_evict_lru_default_count(self):
        cm = CacheManager(max_size=10)
        for i in range(10):
            cm._cache[f"key{i}"] = CacheEntry(value=i, ttl_seconds=3600)
        cm._evict_lru()
        assert len(cm._cache) == 9
        assert "key0" not in cm._cache

    def test_evict_lru_custom_count(self):
        cm = CacheManager(max_size=10)
        for i in range(10):
            cm._cache[f"key{i}"] = CacheEntry(value=i, ttl_seconds=3600)
        cm._evict_lru(count=3)
        assert len(cm._cache) == 7
        assert "key0" not in cm._cache
        assert "key1" not in cm._cache
        assert "key2" not in cm._cache


# ============================================================
# CacheManager — async getItem / setItem
# ============================================================

class TestCacheManagerItemOps:
    @pytest.mark.asyncio
    async def test_set_and_get_item(self):
        cm = CacheManager(max_size=10)
        await cm.setItem("k1", "v1", ttl=3600)
        assert await cm.getItem("k1") == "v1"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self):
        cm = CacheManager()
        assert await cm.getItem("nonexistent") is None

    @pytest.mark.asyncio
    async def test_ttl_expiration_returns_none(self):
        cm = CacheManager()
        with freeze_time("2024-01-01 00:00:00"):
            await cm.setItem("k1", "v1", ttl=60)
        with freeze_time("2024-01-01 00:01:01"):
            assert await cm.getItem("k1") is None

    @pytest.mark.asyncio
    async def test_setitem_updates_existing_key(self):
        cm = CacheManager()
        await cm.setItem("k1", "v1", ttl=3600)
        await cm.setItem("k1", "v2", ttl=3600)
        assert await cm.getItem("k1") == "v2"

    @pytest.mark.asyncio
    async def test_lru_eviction_when_full(self):
        cm = CacheManager(max_size=3)
        await cm.setItem("k1", "v1", ttl=3600)
        await cm.setItem("k2", "v2", ttl=3600)
        await cm.setItem("k3", "v3", ttl=3600)
        await cm.setItem("k4", "v4", ttl=3600)
        assert len(cm._cache) == 3
        assert "k1" not in cm._cache
        assert await cm.getItem("k2") == "v2"

    @pytest.mark.asyncio
    async def test_hits_and_misses_counted(self):
        cm = CacheManager()
        await cm.setItem("k1", "v1", ttl=3600)
        await cm.getItem("k1")  # hit
        await cm.getItem("k1")  # hit
        await cm.getItem("missing")  # miss
        stats = await cm.get_stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1

    @pytest.mark.asyncio
    async def test_expired_entry_counts_as_miss(self):
        cm = CacheManager()
        with freeze_time("2024-01-01 00:00:00"):
            await cm.setItem("k1", "v1", ttl=60)
        with freeze_time("2024-01-01 00:01:01"):
            await cm.getItem("k1")  # miss (expired)
        stats = await cm.get_stats()
        assert stats["misses"] == 1
        assert stats["hits"] == 0


# ============================================================
# CacheManager — connect / close / background cleanup
# ============================================================

class TestCacheManagerLifecycle:
    @pytest.mark.asyncio
    async def test_connect_starts_cleanup_task(self):
        cm = CacheManager()
        await cm.connect()
        assert cm._cleanup_task is not None
        assert cm._cleanup_task.done() is False
        cm._cleanup_task.cancel()
        try:
            await cm._cleanup_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_close_cancels_cleanup_and_clears_cache(self):
        cm = CacheManager()
        await cm.connect()
        await cm.setItem("k1", "v1", ttl=3600)
        await cm.close()
        assert len(cm._cache) == 0
        assert cm._cleanup_task.done()


# ============================================================
# CacheManager — domain helpers
# ============================================================

class TestCacheManagerDomainHelpers:
    @pytest.mark.asyncio
    async def test_get_events_geojson_key_without_layers(self):
        cm = CacheManager()
        key = "events:geojson:60"
        await cm.setItem(key, '{"type":"FeatureCollection"}', ttl=30)
        assert await cm.get_events_geojson(60) == '{"type":"FeatureCollection"}'

    @pytest.mark.asyncio
    async def test_get_events_geojson_key_with_layers(self):
        cm = CacheManager()
        key = "events:geojson:60:bus,cops"
        await cm.setItem(key, '{"type":"FeatureCollection"}', ttl=30)
        assert await cm.get_events_geojson(60, layers=["bus", "cops"]) == '{"type":"FeatureCollection"}'

    @pytest.mark.asyncio
    async def test_get_geo_geojson(self):
        cm = CacheManager()
        await cm.set_geo_geojson('{"type":"FeatureCollection"}', ttl=3600)
        assert await cm.get_geo_geojson() == '{"type":"FeatureCollection"}'

    @pytest.mark.asyncio
    async def test_get_stats_defaults(self):
        cm = CacheManager(max_size=100)
        stats = await cm.get_stats()
        assert stats["connected"] is True
        assert stats["backend"] == "memory"
        assert stats["memory_size"] == 0
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["hit_rate"] == 0
        assert stats["eviction_count"] == 0
        assert stats["utilization"] == 0


# ============================================================
# CacheManager — background cleanup behavior
# ============================================================

class TestCacheManagerBackgroundCleanup:
    @pytest.mark.asyncio
    async def test_connect_starts_cleanup_and_close_cancels_it(self):
        cm = CacheManager()
        await cm.connect()
        assert cm._cleanup_task is not None
        assert not cm._cleanup_task.done()

        await cm.setItem("k1", "v1", ttl=3600)
        await cm.close()

        assert cm._cleanup_task.done()
        assert len(cm._cache) == 0
