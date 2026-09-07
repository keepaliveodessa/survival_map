"""
Event-related database operations.
Handles event creation, querying, and cleanup.
"""

import json
import logging
import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional

from common.settings import settings
from common.db.base import Database
from common.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

EMPTY_GEOJSON: Dict = {'type': 'FeatureCollection', 'features': []}

# Single source of truth for GeoJSON Feature properties mapping.
# Change schema here — all 4 query methods update automatically.
_FEATURE_PROPERTIES = """json_build_object(
    'id', id, 'message_id', message_id, 'description', description,
    'layer', layer, 'strategy', strategy, 'photo_url', photo_url,
    'matches', matches, 'time', event_time
)"""


class EventOperations:
    """Handles event-related database operations."""

    def __init__(self, db: Database):
        self.db = db
        self._db_breaker = CircuitBreaker(failure_threshold=5, timeout=60.0)

    # ------------------------------------------------------------------
    # Internal helpers — GeoJSON query building
    # ------------------------------------------------------------------

    def _geojson_select(self, order_by: str = "event_time") -> str:
        """Build the outer SELECT that wraps rows into a FeatureCollection."""
        return f"""json_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(json_agg(
                json_build_object(
                    'type', 'Feature',
                    'geometry', ST_AsGeoJSON(geom)::json,
                    'properties', {_FEATURE_PROPERTIES}
                ) ORDER BY {order_by}
            ), '[]'::json)
        )"""

    async def _fetch_geojson(self, query: str, params: List[Any]) -> Dict:
        """Execute a GeoJSON query with timeout and standard error handling."""
        try:
            async def _do_query():
                return await self.db.fetchval(query, *params)

            result = await self._db_breaker.call(
                lambda: asyncio.wait_for(
                    _do_query(),
                    timeout=settings.db.command_timeout,
                )
            )
            return json.loads(result) if result else EMPTY_GEOJSON
        except Exception as e:
            logger.error(f"GeoJSON query failed: {e}", exc_info=True)
            return EMPTY_GEOJSON

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_filtered_events_as_geojson(
        self,
        time_interval_minutes: int,
        layers: Optional[List[str]] = None,
        since_timestamp: Optional[str] = None,
        after_id: Optional[int] = None,
        after_message_id: Optional[int] = None
    ) -> Dict:
        """Return recent events as GeoJSON FeatureCollection.

        Args:
            time_interval_minutes: Max age of events (in minutes).
            layers: Optional layer filter.
            since_timestamp: ISO-8601 string — only events newer than this.
            after_id: Only events with id > after_id (catch-up by serial id).
            after_message_id: Only events with message_id > after_message_id
                (preferred watermark — stable across DB restarts).
        """
        where_clauses = ["event_time >= NOW() - $1 * interval '1 minute'"]
        params: List[Any] = [time_interval_minutes]

        if since_timestamp:
            since_dt = since_timestamp
            if isinstance(since_timestamp, str):
                try:
                    since_dt = datetime.fromisoformat(since_timestamp.replace('Z', '+00:00'))
                except ValueError:
                    logger.warning(f"Invalid since_timestamp '{since_timestamp}', ignoring")
                    since_dt = None
            if since_dt is not None:
                params.append(since_dt)
                where_clauses.append(f"event_time > ${len(params)}")

        if after_id is not None:
            params.append(after_id)
            where_clauses.append(f"id > ${len(params)}")

        if after_message_id is not None:
            params.append(after_message_id)
            where_clauses.append(f"message_id > ${len(params)}")

        if layers:
            valid_layers = [layer for layer in layers if layer]
            if valid_layers:
                params.append(valid_layers)
                where_clauses.append(f"layer = ANY(${len(params)})")

        query = f"SELECT {self._geojson_select('event_time')} FROM events WHERE {' AND '.join(where_clauses)}"
        return await self._fetch_geojson(query, params)

    async def get_incremental_events(
        self,
        since: datetime,
        time_interval_minutes: int,
        layers: Optional[List[str]] = None
    ) -> Dict:
        """Fetch events created after 'since' timestamp, filtered by time and layers."""
        where_clauses = [
            "event_time >= $1",
            "event_time >= NOW() - $2 * interval '1 minute'"
        ]
        params: List[Any] = [since, time_interval_minutes]

        if layers:
            valid_layers = [layer for layer in layers if layer]
            if valid_layers:
                params.append(valid_layers)
                where_clauses.append(f"layer = ANY(${len(params)})")

        query = f"SELECT {self._geojson_select('event_time')} FROM events WHERE {' AND '.join(where_clauses)}"
        return await self._fetch_geojson(query, params)

    async def get_events_updates_as_geojson(
        self,
        after_id: Optional[int] = None,
        after_message_id: Optional[int] = None,
        limit: int = 2000
    ) -> Dict:
        """Fetch events with id/message_id > watermark, limited to last 60 min.

        Watermark priority: after_message_id (stable across DB restarts)
        takes precedence over after_id.
        """
        conditions = ["event_time >= NOW() - INTERVAL '60 minutes'"]
        params: List[Any] = []
        if after_message_id is not None:
            params.append(after_message_id)
            conditions.append(f"message_id > ${len(params)}")
        elif after_id is not None:
            params.append(after_id)
            conditions.append(f"id > ${len(params)}")
        params.append(limit)

        query = f"""SELECT {self._geojson_select('id')}
            FROM (
                SELECT * FROM events
                WHERE {' AND '.join(conditions)}
                ORDER BY id LIMIT ${len(params)}
            ) e"""
        return await self._fetch_geojson(query, params)

    async def get_events_snapshot_as_geojson(self, limit: int = 5000) -> Dict:
        """Fetch snapshot of last 60 minutes events (used for resync)."""
        query = f"""SELECT {self._geojson_select('id')}
            FROM (
                SELECT * FROM events
                WHERE event_time >= NOW() - INTERVAL '60 minutes'
                ORDER BY id LIMIT $1
            ) e"""
        return await self._fetch_geojson(query, [limit])

    # ------------------------------------------------------------------
    # Non-GeoJSON queries
    # ------------------------------------------------------------------

    async def get_latest_update_time(self) -> Optional[datetime]:
        """Get the timestamp of the newest event."""
        try:
            result = await asyncio.wait_for(
                self.db.fetchval("SELECT MAX(event_time) FROM events"),
                timeout=settings.db.command_timeout,
            )
            return result
        except Exception as e:
            logger.error(f"Failed to get latest events update time: {e}")
            return None

    async def get_events_meta(self) -> Dict[str, Any]:
        """Get events synchronization metadata (version/max_event_id/updated_at)."""
        query = """
            SELECT version, updated_at, max_event_id
            FROM events_meta WHERE id = 1
        """
        try:
            row = await asyncio.wait_for(
                self.db.fetchrow(query),
                timeout=settings.db.command_timeout,
            )
            if not row:
                return {'version': 0, 'updated_at': None, 'max_event_id': 0}
            data = dict(row)
            return {
                'version': int(data.get('version') or 0),
                'updated_at': data.get('updated_at'),
                'max_event_id': int(data.get('max_event_id') or 0)
            }
        except Exception as e:
            logger.error(f"Failed to get events_meta: {e}", exc_info=True)
            return {'version': 0, 'updated_at': None, 'max_event_id': 0}

    async def get_events_min_id(self) -> int:
        """Get minimum event id currently present in DB."""
        query = "SELECT COALESCE(MIN(id), 0) FROM events"
        try:
            val = await asyncio.wait_for(
                self.db.fetchval(query),
                timeout=settings.db.command_timeout,
            )
            return int(val or 0)
        except Exception as e:
            logger.error(f"Failed to get min events id: {e}", exc_info=True)
            return 0

    async def get_events_message_id_range(self) -> tuple:
        """Get (min, max) message_id currently present in events."""
        query = """
            SELECT COALESCE(MIN(message_id), 0) AS min_mid,
                   COALESCE(MAX(message_id), 0) AS max_mid
            FROM events
        """
        try:
            row = await asyncio.wait_for(
                self.db.fetchrow(query),
                timeout=settings.db.command_timeout,
            )
            if not row:
                return (0, 0)
            return (int(row['min_mid'] or 0), int(row['max_mid'] or 0))
        except Exception as e:
            logger.error(f"Failed to get message_id range: {e}", exc_info=True)
            return (0, 0)

    async def get_event_by_id(self, event_id: int) -> Optional[Dict]:
        """Fetch a single event as a GeoJSON Feature by its id.

        Used by core WebSocket handler to hydrate minimal pg_notify payloads
        (R-DB0) into full Features for broadcast.
        """
        query = (
            "SELECT json_build_object("
            "'type', 'Feature', "
            "'geometry', ST_AsGeoJSON(geom)::json, "
            "'properties', " + _FEATURE_PROPERTIES + 
            ") FROM events WHERE id = $1"
        )
        try:
            result = await asyncio.wait_for(
                self.db.fetchval(query, event_id),
                timeout=settings.db.command_timeout,
            )
            return json.loads(result) if result else None
        except Exception as e:
            logger.error(f"Failed to fetch event by id {event_id}: {e}")
            return None
