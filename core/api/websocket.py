"""WebSocket API handlers for real-time event updates"""
import asyncio
import orjson
import logging
from typing import Dict, Set, Optional
from datetime import datetime, timezone
from aiohttp import web, WSMsgType
from core.db.dbconnect import Request
from common.settings import settings
from core.middlewares.auth import verify_jwt_token
from core.utils.telegram_validation import validate_telegram_webapp_data
from core.metrics import (
    ws_connections_total, ws_connections_rejected_total,
    ws_messages_sent_total, ws_broadcast_duration_seconds
)

logger = logging.getLogger(__name__)

# Верхняя граница одновременных WS-соединений — защита от утечки/перегрузки.
MAX_CONNECTIONS = 1000
# Таймаут отправки одному клиенту: зависший клиент не должен тормозить
# рассылку остальным (asyncio.gather ждёт все корутины).
SEND_TIMEOUT = 5.0
# Heartbeat интервал для очистки зависших соединений
HEARTBEAT_INTERVAL = 30.0
# Rate limiting для ping сообщений (макс. в секунду)
PING_RATE_LIMIT = 5
# Макс. время на аутентификацию после открытия соединения
WS_AUTH_TIMEOUT = 5.0
# Макс. размер одного сообщения (bytes)
WS_MAX_MSG_BYTES = 65536


def _json_msg(type_: str, **fields) -> str:
    """Сериализовать server→client сообщение в JSON-строку.

    Единая точка сборки исходящих WS-сообщений: раньше паттерн
    orjson.dumps({...}).decode() повторялся 10+ раз, и каждый новый вызов
    мог забыть .decode() (send_str принимает только str).
    """
    return orjson.dumps({
        'type': type_,
        **fields,
    }).decode()


class WebSocketManager:
    """Manages WebSocket connections and broadcasts individual features to clients."""

    def __init__(self, db_request: Request, cache_manager=None):
        """Инициализирует менеджер WebSocket-соединений."""
        self.db_request = db_request
        self.cache_manager = cache_manager
        self.connections: Set[web.WebSocketResponse] = set()
        self.broadcast_lock = asyncio.Lock()
        self._ping_counters: Dict[web.WebSocketResponse, list] = {}  # Rate limiting
        self._ws_subscriptions: Dict[web.WebSocketResponse, Set[str]] = {}  # Layer subscriptions
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start_cleanup_task(self):
        """Start background task for cleaning up stale connections."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_stale_connections())
            logger.info("WebSocket cleanup task started")

    async def _cleanup_stale_connections(self):
        """Background task to cleanup stale connections."""
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                stale = [ws for ws in list(self.connections) if ws.closed]
                for ws in stale:
                    await self.unregister_connection(ws)
                if stale:
                    logger.info(f"Cleaned up {len(stale)} stale connections")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup task: {e}")

    async def register_connection(self, ws: web.WebSocketResponse) -> bool:
        """Register a new WebSocket connection.

        Returns False if the connection limit is reached — caller must close ws.
        Data is sent only after the client authenticates.
        """
        if len(self.connections) >= MAX_CONNECTIONS:
            logger.warning(f"WebSocket connection rejected: limit {MAX_CONNECTIONS} reached")
            ws_connections_rejected_total.inc()
            return False
        self.connections.add(ws)
        self._ping_counters[ws] = []
        self._ws_subscriptions[ws] = set()
        ws_connections_total.inc()
        logger.info(f"WebSocket connection registered. Total: {len(self.connections)}")
        return True

    def _check_rate_limit(self, ws: web.WebSocketResponse) -> bool:
        """Check if websocket is within rate limit for ping messages."""
        now = asyncio.get_running_loop().time()
        if ws not in self._ping_counters:
            self._ping_counters[ws] = []
        
        # Remove timestamps older than 1 second
        self._ping_counters[ws] = [t for t in self._ping_counters[ws] if now - t < 1.0]
        
        if len(self._ping_counters[ws]) >= PING_RATE_LIMIT:
            return False
        
        self._ping_counters[ws].append(now)
        return True

    async def unregister_connection(self, ws: web.WebSocketResponse):
        """Unregister a WebSocket connection."""
        if ws in self.connections:
            self.connections.discard(ws)
            self._ping_counters.pop(ws, None)
            self._ws_subscriptions.pop(ws, None)
        logger.debug(f"WebSocket connection unregistered. Total: {len(self.connections)}")

    async def close_all(self) -> None:
        """Close all active WebSocket connections (called during server shutdown)."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        for ws in list(self.connections):
            try:
                await asyncio.wait_for(
                    ws.close(code=1001, message=b'server shutdown'), timeout=2.0
                )
            except Exception:
                pass
        self.connections.clear()
        self._ping_counters.clear()
        self._ws_subscriptions.clear()

    async def _broadcast_payload(self, payload: str, layer: str = None) -> int:
        """Send a pre-serialized JSON string to connected clients, optionally
        filtered by layer subscription.

        Optimized: snapshot connections once (fast copy), send to all clients in
        parallel without holding a lock, then batch-unregister failed connections
        under a brief lock. This allows multiple concurrent broadcasts to proceed
        in parallel instead of serializing on broadcast_lock.
        """
        snapshot = list(self.connections)
        if not snapshot:
            return 0

        async def _send(ws: web.WebSocketResponse) -> Optional[web.WebSocketResponse]:
            """Send payload to one client. Returns ws on failure (for batch unregister), None on success."""
            try:
                subscriptions = self._ws_subscriptions.get(ws, set())
                if layer is not None and subscriptions and layer not in subscriptions:
                    return None  # Skipped by filter, not a failure
                await asyncio.wait_for(ws.send_str(payload), timeout=SEND_TIMEOUT)
                return None
            except Exception as e:
                logger.debug(f"Broadcast send error/timeout: {e}")
                return ws  # Return ws for batch unregister

        # Send to ALL clients in parallel — no lock held during I/O.
        # snapshot is a point-in-time copy; new connections added during
        # send are simply missed (will get the next broadcast).
        results = await asyncio.gather(*[_send(ws) for ws in snapshot], return_exceptions=True)

        # Collect failed connections for batch unregister
        failed = [r for r in results if isinstance(r, web.WebSocketResponse)]
        success = len(snapshot) - len(failed)

        # Batch-unregister failed connections under a brief lock
        if failed:
            async with self.broadcast_lock:
                for ws in failed:
                    self.connections.discard(ws)
                    self._ping_counters.pop(ws, None)
                    self._ws_subscriptions.pop(ws, None)

        # Track broadcast metrics
        ws_messages_sent_total.inc(success)
        if layer:
            ws_broadcast_duration_seconds.labels(layer=layer).observe(0)
        return success

    async def send_snapshot(self, events_data: dict, channel: str = 'events_new'):
        """Broadcast recent events as a single FeatureCollection to all clients."""
        features = events_data.get('features', [])
        if not features or not self.connections:
            return

        payload = _json_msg(
            'events_snapshot',
            data={
                'type': 'FeatureCollection',
                'features': features
            },
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        await self._broadcast_payload(payload)

        end_payload = _json_msg(
            'events_snapshot_end',
            count=len(features),
            timestamp=datetime.now(timezone.utc).isoformat(),
            channel=channel,
        )
        await self._broadcast_payload(end_payload)

    async def send_events_since(
        self,
        ws: web.WebSocketResponse,
        since_timestamp: Optional[str] = None,
        since_id: Optional[int] = None,
        since_message_id: Optional[int] = None
    ):
        """
        Send individual GeoJSON features to a client.

        Watermark priority: since_message_id > since_id > since_timestamp.
        message_id (Telegram) стабилен между рестартами БД: события
        пере-вставляются с теми же message_id, тогда как id события
        перезапускается с 1 — поэтому catch-up идёт по message_id.

        Если since_message_id вне текущего диапазона message_id в БД —
        кэш клиента устарел или чужой (рестарт БД, чистка партиций):
        сначала шлём {type:'resync_required'}, затем полный snapshot
        60-мин окна. Флаг идёт ДО features, чтобы клиент успел очистить
        store до приёма данных.
        """
        resync = False
        if since_message_id is not None:
            min_mid, max_mid = await self.db_request.get_events_message_id_range()
            if since_message_id > max_mid or (min_mid > 0 and since_message_id < min_mid - 1):
                resync = True
                logger.info(
                    f"Client watermark out of DB range: since_message_id={since_message_id}, "
                    f"DB=[{min_mid}..{max_mid}] — sending resync_required + full snapshot"
                )

        try:
            if resync:
                await ws.send_str(_json_msg(
                    'resync_required',
                    timestamp=datetime.now(timezone.utc).isoformat(),
                ))
                since_timestamp = None
                since_id = None
                since_message_id = None

            events_data = await self.db_request.get_filtered_events_as_geojson(
                time_interval_minutes=60,
                since_timestamp=(
                    None if (since_id is not None or since_message_id is not None)
                    else since_timestamp
                ),
                after_id=since_id,
                after_message_id=since_message_id
            )

            features = events_data.get('features', [])
            logger.info(
                f"Sending {len(features)} features to client "
                f"(after_message_id={since_message_id or 'initial'}, "
                f"after_id={since_id or 'initial'}, "
                f"since={'' if since_message_id is not None else (since_timestamp or 'initial')}, "
                f"resync={resync})"
            )

            for feature in features:
                message = _json_msg(
                    'feature',
                    data=feature,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                try:
                    await asyncio.wait_for(
                        ws.send_str(message), timeout=SEND_TIMEOUT
                    )
                except Exception as e:
                    logger.warning(f"Failed to send feature to client: {e}")
                    return

            # Terminal marker for the batch. The client treats every feature
            # received before this as a silent snapshot (initial load or
            # reconnect catch-up); only live pushes after it raise per-event
            # notifications.
            try:
                await ws.send_str(_json_msg(
                    'events_snapshot_end',
                    count=len(features),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                ))
            except Exception as e:
                logger.warning(f"Failed to send events_snapshot_end: {e}")

        except Exception as e:
            logger.error(f"Error sending events to client: {e}", exc_info=True)

    async def broadcast_event(self, event_data: Dict):
        """
        Broadcast a single event to all connected clients.
        
        Accepts either:
          - Full GeoJSON Feature (type: 'Feature', properties, geometry)
          - Minimal notification from processor: {"id": N, "layer": "...", "strategy": "..."}
            (R-DB0: pg_notify sends minimal payloads; core fetches full data from DB)
        """
        if not self.connections:
            return

        # Normalise: if the parser sent a FeatureCollection, extract the first feature
        if event_data.get('type') == 'FeatureCollection':
            features = event_data.get('features', [])
            if not features:
                logger.warning("broadcast_event: empty FeatureCollection, skipping")
                return
            event_data = features[0]

        # R-DB0: Minimal notification from processor — fetch full Feature from DB
        if event_data.get('type') != 'Feature':
            event_id = event_data.get('id')
            if event_id and hasattr(self, 'db_request') and self.db_request:
                try:
                    full_feature = await self.db_request.get_event_by_id(event_id)
                    if full_feature:
                        event_data = full_feature
                    else:
                        logger.warning(f"broadcast_event: event {event_id} not found in DB")
                        return
                except Exception as e:
                    logger.warning(f"broadcast_event: failed to fetch event {event_id}: {e}")
                    return
            else:
                logger.warning(f"broadcast_event: unexpected data type and no db_request: {event_data}")
                return

        layer = event_data.get('properties', {}).get('layer')
        payload = _json_msg(
            'feature',
            data=event_data,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        success = await self._broadcast_payload(payload, layer=layer)
        logger.info(f"Feature broadcasted: {success}/{len(self.connections)} clients")

    async def broadcast_events_cleaned(self, data: Dict):
        """Broadcast events_cleaned notification to all connected clients."""
        if not self.connections:
            return

        payload = _json_msg(
            'events_cleaned',
            data=data,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        success = await self._broadcast_payload(payload)
        logger.info(f"events_cleaned broadcasted: {success}/{len(self.connections)} clients")


def _ws_authenticate(data: dict) -> bool:
    """Проверить учётные данные из auth-сообщения WebSocket.

    `/ws` исключён из jwt_auth_middleware (см. core/middlewares/jwt_auth.py),
    поэтому проверка ДОЛЖНА выполняться здесь — иначе сокет открыт для всех.
    Принимаем JWT access-токен (token) ИЛИ Telegram initData (init_data),
    как их шлёт клиент (web/js/core/websocket.ts sendAuth).

    Security:
    - If validation enabled: STRICT checks on both token and init_data
    - If validation disabled: accept any (dev mode)
    """
    validation_enabled = getattr(settings.app, 'telegram_webview_validation', True)
    
    if not validation_enabled:
        # Dev mode: валидация отключена
        logger.debug("[WS] Dev mode: authentication bypassed")
        return True
    
    # STRICT MODE: require valid JWT or initData
    token = data.get('token')
    if token:
        payload = verify_jwt_token(token, 'access')
        if payload:
            logger.debug(f"[WS] Authenticated via JWT: {payload.get('sub')}")
            return True
        logger.warning("[WS] Invalid JWT token")
    
    init_data = data.get('init_data')
    if init_data:
        if not settings.bot.token:
            logger.error("[WS] BOT_TOKEN not configured but validation required")
            return False
        
        is_valid, user_data = validate_telegram_webapp_data(
            init_data, 
            settings.bot.token,
            max_age_hours=24
        )
        if is_valid and user_data:
            logger.debug(f"[WS] Authenticated via initData: {user_data.get('id')}")
            return True
        logger.warning("[WS] Invalid initData")
    
    logger.warning("[WS] Authentication failed: no valid token or initData")
    return False


async def websocket_handler(request: web.Request):
    """WebSocket endpoint for real-time event updates."""
    # Origin check: reject cross-origin connections unless explicitly allowed
    origin = request.headers.get('Origin', '')
    if origin:
        allowed_origins = getattr(settings.app, 'allowed_origins', ())
        if allowed_origins:
            if origin not in allowed_origins and origin != '*':
                logger.warning(f"WebSocket rejected: origin {origin} not in allowed list")
                return web.json_response({'error': 'Origin not allowed'}, status=403)

    # aiohttp >= 3.10 removed max_ws_bytes (renamed max_msg_size in 3.9);
    # on 3.14 the old kwarg raises TypeError and breaks every /ws connection.
    ws = web.WebSocketResponse(
        heartbeat=120,
        max_msg_size=WS_MAX_MSG_BYTES,
    )
    await ws.prepare(request)

    ws_manager = request.app.get('websocket_manager')
    if not ws_manager:
        logger.error("WebSocket manager not found in app")
        await ws.close()
        return ws

    if not await ws_manager.register_connection(ws):
        await ws.close(code=1013, message=b'server busy')  # 1013 = Try Again Later
        return ws
    authenticated = False

    # Auth timeout: используем asyncio.Event чтобы избежать race condition
    # между проверкой auth_deadline_task.done() и cancel().
    # _auth_event.set() атомарно отменяет таймаут — не нужен cancel() на таске.
    _auth_event = asyncio.Event()
    auth_deadline_task: Optional[asyncio.Task] = None

    async def _auth_timeout():
        try:
            await asyncio.wait_for(_auth_event.wait(), timeout=WS_AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            # Событие не было выставлено в течение WS_AUTH_TIMEOUT —
            # клиент не прошёл аутентификацию вовремя.
            if not ws.closed:
                logger.warning(f"WebSocket auth timeout from {request.remote}")
                await ws.close(code=1008, message=b'auth timeout')

    auth_deadline_task = asyncio.create_task(_auth_timeout())

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    # Per-message size guard
                    if len(msg.data) > WS_MAX_MSG_BYTES:
                        logger.warning(f"WebSocket message too large from {request.remote}")
                        await ws.send_str(_json_msg('error', message='message too large'))
                        continue

                    data = orjson.loads(msg.data)
                    message_type = data.get('type')

                    if message_type == 'ping':
                        # Rate limiting для защиты от спама
                        if not ws_manager._check_rate_limit(ws):
                            logger.warning("WebSocket ping rate limit exceeded")
                            await ws.send_str(_json_msg('error', message='rate limit exceeded'))
                            continue

                        await ws.send_str(_json_msg(
                            'pong',
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        ))

                    elif message_type == 'auth':
                        # /ws исключён из jwt_auth_middleware → проверяем здесь.
                        if _ws_authenticate(data):
                            authenticated = True
                            logger.info("WebSocket client authenticated")
                            # Атомарно сигнализируем таймауту — он завершится
                            # сам без cancel(). Нет race condition между
                            # проверкой done() и cancel().
                            _auth_event.set()
                            await ws.send_str(_json_msg('auth_ok'))
                        else:
                            logger.warning("WebSocket auth failed — closing connection")
                            await ws.send_str(_json_msg('error', message='authentication failed'))
                            await ws.close(code=1008, message=b'auth failed')  # policy violation
                            break

                    elif message_type == 'subscribe_layers':
                        if not authenticated:
                            await ws.send_str(_json_msg('error', message='not authenticated'))
                            continue
                        layers = data.get('layers') or []
                        if not isinstance(layers, list):
                            await ws.send_str(_json_msg('error', message='layers must be a list'))
                            continue
                        ws_manager._ws_subscriptions[ws] = set(layers)
                        await ws.send_str(_json_msg('subscribed', layers=layers))

                    elif message_type == 'get_events':
                        if not authenticated:
                            await ws.send_str(_json_msg('error', message='not authenticated'))
                            continue

                        since_timestamp = data.get('since_timestamp')  # ISO string or null
                        since_id = None
                        since_id_raw = data.get('since_id')  # int or null
                        if since_id_raw is not None:
                            try:
                                since_id = int(since_id_raw)
                            except (TypeError, ValueError):
                                logger.warning(f"Invalid since_id '{since_id_raw}', ignoring")

                        # Основной водяной знак — message_id (стабилен между
                        # рестартами БД); since_id остаётся fallback'ом.
                        since_message_id = None
                        since_message_id_raw = data.get('since_message_id')  # int or null
                        if since_message_id_raw is not None:
                            try:
                                since_message_id = int(since_message_id_raw)
                            except (TypeError, ValueError):
                                logger.warning(
                                    f"Invalid since_message_id '{since_message_id_raw}', ignoring"
                                )

                        await ws_manager.send_events_since(
                            ws, since_timestamp, since_id, since_message_id
                        )

                except orjson.JSONDecodeError:
                    logger.warning("Invalid JSON received from WebSocket client")
                except Exception as e:
                    logger.error(f"Error processing WebSocket message: {e}", exc_info=True)

            elif msg.type == WSMsgType.ERROR:
                logger.error(f"WebSocket connection error: {ws.exception()}")
                break

    finally:
        if auth_deadline_task and not auth_deadline_task.done():
            # Страховочная отмена: если вышли до аутентификации (например,
            # клиент закрыл соединение сам) — гасим таймер, чтобы не висел.
            _auth_event.set()  # безопасно вызывать повторно
        await ws_manager.unregister_connection(ws)

    return ws
