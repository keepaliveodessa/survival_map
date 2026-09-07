"""
Geo-related database operations.
Данные гео-объектов грузит postgres-контейнер (init-scripts/04-load-data.sql); здесь —
только чтение (count, geojson, время обновления).
"""

import logging
import asyncio

from common.settings import settings
from common.db.base import Database

logger = logging.getLogger(__name__)


class GeoOperations:
    """Handles geo-related database operations."""

    def __init__(self, db: Database):
        """Инициализирует обработчик гео-операций БД."""
        self.db = db

    async def get_geo_count(self) -> int:
        """Get the total count of geo records."""
        try:
            return await self.db.fetchval("SELECT COUNT(*) FROM geo", timeout=settings.db.command_timeout)
        except Exception as e:
            logger.error(f"Failed to get geo count: {e}")
            return 0

    async def get_all_geo_as_geojson(self) -> str:
        """Fetch all geo records as a GeoJSON FeatureCollection."""
        query = """
            SELECT json_build_object(
                'type', 'FeatureCollection',
                'features', COALESCE(json_agg(
                    json_build_object(
                        'type', 'Feature',
                        'geometry', ST_AsGeoJSON(geom)::json,
                        'properties', json_build_object(
                            'name', array_to_string(names, '|'),
                            'id', id
                        )
                    )
                ), '[]'::json)
            )
            FROM geo
            WHERE ST_IsValid(geom);
        """
        try:
            async with self.db.pool.acquire() as connection:
                result = await asyncio.wait_for(
                    connection.fetchval(query),
                    timeout=settings.db.command_timeout,
                )
            return result if result else '{"type": "FeatureCollection", "features": []}'
        except Exception as e:
            logger.error(f"Failed to fetch geo as GeoJSON: {e}")
            return '{"type": "FeatureCollection", "features": []}'
