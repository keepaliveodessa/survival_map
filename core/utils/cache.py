"""
In-memory caching layer with TTL support and LRU eviction

Redis removed - using pure in-memory cache only.
"""
import time
import logging
import asyncio
from typing import Optional, Any
from collections import OrderedDict

logger = logging.getLogger(__name__)


class CacheEntry:
    """Cache entry with expiration time and LRU tracking."""

    def __init__(self, value: Any, ttl_seconds: int):
        """Создаёт запись кэша с указанным значением и TTL."""
        self.value = value
        self.expires_at = time.time() + ttl_seconds
        self.last_accessed = time.time()

    def is_expired(self) -> bool:
        """Проверяет, истёк ли срок жизни записи."""
        return time.time() > self.expires_at

    def touch(self):
        """Обновляет время последнего доступа для LRU-алгоритма."""
        self.last_accessed = time.time()


class CacheManager:
    """
    In-memory cache with TTL support and LRU eviction

    Features:
    - Per-key TTL
    - Auto cleanup of expired entries
    - LRU eviction when max_size exceeded
    - Thread-safe operations
    - Statistics tracking
    """

    def __init__(self, max_size: int = 10000):
        """Инициализирует in-memory кэш с LRU-вытеснением и поддержкой TTL."""
        # OrderedDict для LRU eviction
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.max_size = max_size
        self._eviction_count = 0
        
        # Блокировка для потокобезопасности
        self._lock = asyncio.Lock()
        
        # Background cleanup task
        self._cleanup_task: Optional[asyncio.Task] = None

    async def connect(self, max_retries: int = 3):
        """Инициализирует in-memory кэш и запускает фоновую задачу очистки."""
        logger.info(f"✅ In-memory cache initialized (max_size={self.max_size})")
        
        # Start background cleanup task
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._background_cleanup())
            logger.info("Cache background cleanup task started")
        
        return True

    async def _background_cleanup(self):
        """Background task to cleanup expired entries periodically."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                async with self._lock:
                    expired_keys = [k for k, v in self._cache.items() if v.is_expired()]
                    for key in expired_keys:
                        del self._cache[key]
                    if expired_keys:
                        logger.debug(f"Background cleanup: removed {len(expired_keys)} expired entries")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cache cleanup task: {e}")

    async def close(self):
        """Очищает кэш при завершении работы приложения."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        self._cache.clear()
        logger.info("In-memory cache cleared")
    
    def _evict_lru(self, count: int = None):
        """
        Evict least recently used entries.
        
        Args:
            count: Number of entries to evict (default: 10% of max_size)
        """
        if count is None:
            count = max(1, self.max_size // 10)
        
        evicted = 0
        while evicted < count and self._cache:
            # Удаляем самый старый элемент (первый в OrderedDict)
            self._cache.popitem(last=False)
            evicted += 1
        
        self._eviction_count += evicted
        logger.debug(f"LRU eviction: {evicted} entries removed")

    async def getItem(self, key: str) -> Optional[Any]:
        """Get value from in-memory cache with LRU update."""
        async with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self.misses += 1
                return None

            if entry.is_expired():
                del self._cache[key]
                self.misses += 1
                return None

            # Обновляем время доступа для LRU
            entry.touch()
            # Перемещаем в конец OrderedDict (most recently used)
            self._cache.move_to_end(key)
            
            self.hits += 1
            return entry.value

    async def setItem(self, key: str, value: Any, ttl: int = 3600) -> bool:
        """Set value in in-memory cache with TTL and LRU eviction."""
        async with self._lock:
            # Если ключ существует - обновляем и перемещаем в конец
            if key in self._cache:
                self._cache[key] = CacheEntry(value, ttl)
                self._cache.move_to_end(key)
                return True
            
            # Проверка на превышение размера
            if len(self._cache) >= self.max_size:
                self._evict_lru()
            
            # Добавляем новый элемент в конец
            self._cache[key] = CacheEntry(value, ttl)
            return True

    # =====================
    # Events API methods
    # =====================

    async def get_events_geojson(self, time_filter: int, layers: list = None) -> Optional[str]:
        """Возвращает закешированный GeoJSON событий."""
        key = f"events:geojson:{time_filter}"
        if layers:
            key += f":{','.join(sorted(layers))}"
        return await self.getItem(key)

    async def set_events_geojson(self, time_filter: int, layers: list, data: str, ttl: int = 30) -> bool:
        """Кеширует GeoJSON-ответ событий с указанным TTL."""
        key = f"events:geojson:{time_filter}"
        if layers:
            key += f":{','.join(sorted(layers))}"
        return await self.setItem(key, data, ttl)

    async def get_geo_geojson(self) -> Optional[str]:
        """Возвращает закешированный GeoJSON гео-данных."""
        return await self.getItem("geo:geojson")

    async def set_geo_geojson(self, data: str, ttl: int = 3600) -> bool:
        """Кеширует GeoJSON гео-данных с указанным TTL."""
        return await self.setItem("geo:geojson", data, ttl)

    async def get_stats(self) -> dict:
        """Возвращает статистику кэша, включая LRU-метрики."""
        total = self.hits + self.misses
        return {
            'connected': True,
            'backend': 'memory',
            'memory_size': len(self._cache),
            'max_size': self.max_size,
            'hits': self.hits,
            'misses': self.misses,
            'hit_rate': self.hits / total if total > 0 else 0,
            'eviction_count': self._eviction_count,
            'utilization': len(self._cache) / self.max_size if self.max_size > 0 else 0
        }
