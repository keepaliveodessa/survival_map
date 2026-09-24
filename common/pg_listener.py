"""Reusable PostgreSQL LISTEN/NOTIFY listener with auto-reconnect.

Eliminates the duplicated connection-acquire → listen → keep-alive →
backoff → cleanup pattern across app_factory, parser, and nlp_processor.

Usage:
    listener = PgNotifyListener(
        pool=db_pool,
        channels=['events_new', 'events_cleaned'],
        handler=my_handler,
        shutdown_event=shutdown_event,
    )
    await listener.run()  # blocks until shutdown_event is set
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Default backoff schedule in seconds
DEFAULT_BACKOFF = [1, 5, 30]


class PgNotifyListener:
    """Auto-reconnecting PostgreSQL LISTEN/NOTIFY listener."""

    def __init__(
        self,
        pool: Any,  # asyncpg.Pool or Database.pool
        channels: List[str],
        handler: Callable[[str, Any], Coroutine],
        shutdown_event: Optional[asyncio.Event] = None,
        backoff_schedule: Optional[List[float]] = None,
        reconnect_delay: float = 5.0,
        label: str = "",
        post_subscribe: Optional[Callable[[], Coroutine]] = None,
    ):
        """
        Args:
            pool: asyncpg pool (must have .acquire() and .release()).
            channels: List of PostgreSQL channel names to listen on.
            handler: Async callable(channel: str, payload: str) for each NOTIFY.
            shutdown_event: Event that signals shutdown (listener exits when set).
            backoff_schedule: List of delays in seconds for retry backoff.
            reconnect_delay: Delay between reconnect attempts (fallback).
            label: Label for log messages.
            post_subscribe: Optional async callback called once after listeners are added
                (e.g., for catch-up queries). Only called on successful subscribe.
        """
        self._pool = pool
        self._channels = channels
        self._handler = handler
        self._shutdown_event = shutdown_event
        self._backoff = backoff_schedule or DEFAULT_BACKOFF
        self._reconnect_delay = reconnect_delay
        self._label = label or f"PGListener({','.join(channels)})"
        self._post_subscribe = post_subscribe
        self._connection: Optional[Any] = None
        self._notify_tasks: Set[asyncio.Task] = set()
        self._listeners: Dict[str, Callable] = {}

    async def run(self):
        """Main loop: acquire, listen, keep-alive, reconnect on failure.

        Blocks until shutdown_event is set.
        """
        backoff_idx = 0

        while not self._is_shutdown():
            try:
                self._connection = await self._pool.acquire()

                for channel in self._channels:
                    listener = self._make_listener(channel)
                    self._listeners[channel] = listener
                    await self._connection.add_listener(
                        channel, listener
                    )

                logger.info(f"{self._label}: listening on {self._channels}")
                backoff_idx = 0

                # Run optional post-subscribe hook (e.g., catch-up query)
                if self._post_subscribe:
                    try:
                        await self._post_subscribe()
                    except Exception as e:
                        logger.warning(f"{self._label}: post_subscribe failed: {e}")

                # Keep-alive loop: detect closed connections
                while not self._is_shutdown():
                    await asyncio.sleep(5)
                    if self._connection.is_closed():
                        raise ConnectionError(f"{self._label}: connection closed")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                delay = self._backoff[min(backoff_idx, len(self._backoff) - 1)]
                logger.warning(f"{self._label}: lost connection ({e}), retry in {delay}s")
                backoff_idx += 1
            finally:
                await self._cleanup()

            if not self._is_shutdown():
                await asyncio.sleep(self._reconnect_delay)

    async def stop(self):
        """Graceful shutdown: cancel tasks, remove listeners, release connection."""
        self._cleanup()

    def _is_shutdown(self) -> bool:
        return self._shutdown_event is not None and self._shutdown_event.is_set()

    def _make_listener(self, channel: str):
        """Create a listener callback that spawns handler as a tracked task."""
        loop = asyncio.get_running_loop()

        def _on_notify(connection, pid, received_channel, payload):
            task = loop.create_task(self._handler(received_channel, payload))
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)

        return _on_notify

    async def _cleanup(self):
        """Remove listeners and release connection."""
        # Cancel pending notify handler tasks
        for task in list(self._notify_tasks):
            if not task.done():
                task.cancel()
        self._notify_tasks.clear()

        if self._connection is not None:
            conn = self._connection
            self._connection = None

            # Remove listeners
            for channel in self._channels:
                listener = self._listeners.get(channel)
                if listener is None:
                    continue
                try:
                    await asyncio.wait_for(
                        conn.remove_listener(channel, listener),
                        timeout=1.0,
                    )
                except (asyncio.TimeoutError, Exception):
                    pass
            self._listeners.clear()

            # Release connection back to pool
            try:
                await asyncio.wait_for(self._pool.release(conn), timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass
