"""Application factory for creating and configuring the aiohttp application"""
import json
import time
import logging
import asyncio
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
import aiohttp_cors

from common.settings import settings
from core.db.dbconnect import Database, Request
from core.utils.cache import CacheManager
from core.handlers import basic_router
from core.middlewares.ratelimit import RateLimiter
from common.logging_config import setup_logging, logging_middleware, register_http_metrics
from core.metrics import http_requests_total, http_request_duration_seconds
from core.api.routes import setup_routes
from core.api.auth import init_cache
from core.api.websocket import WebSocketManager
from core.middlewares.jwt_auth import jwt_auth_middleware
from core.middlewares.body_size_limit import body_size_limit_middleware
from common.pg_listener import PgNotifyListener

logger = logging.getLogger(__name__)

# common/ не может импортировать core (common-invariant) — регистрируем
# HTTP-метрики через инжекцию, middleware common/logging_config.py подхватит.
register_http_metrics(
    requests_total=http_requests_total,
    request_duration_seconds=http_request_duration_seconds,
)


async def _run_bot_polling(app: web.Application):
    """Bot polling loop with exponential backoff on network errors."""
    bot: Bot = app['bot']
    dp: Dispatcher = app['dp']
    shutdown_event = app['shutdown_event']

    if not settings or not settings.bot or not settings.bot.token:
        logger.warning("BOT_TOKEN not configured, skipping bot polling")
        return

    delay = 30
    max_delay = 60  # 60s вместо 300s — graceful shutdown не должен ждать 5 минут

    try:
        while not shutdown_event.is_set():
            try:
                logger.info("Starting bot polling...")
                delay = 30
                # handle_signals=False: иначе aiogram ставит СВОИ SIGTERM/SIGINT-хендлеры
                # поверх main.py — он останавливает только polling, а aiohttp-сервер
                # (site/runner) не получает shutdown и процесс висит до SIGKILL (137).
                # Сигналы обрабатывает единый хендлер в main.py → shutdown_event.
                await dp.start_polling(
                    bot,
                    handle_signals=False,
                    allowed_updates=dp.resolve_used_update_types(),
                )
                break
            except asyncio.CancelledError:
                logger.info("Bot polling cancelled (shutdown requested)")
                break
            except Exception as e:
                if shutdown_event.is_set():
                    break
                logger.warning(f"Bot polling failed: {e}. Retry in {delay}s...")
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    break
                delay = min(delay * 2, max_delay)
    finally:
        # Сигналы обрабатывает main.py (polling запущен с handle_signals=False).
        # Этот finally — страховка: если polling завершился по иной причине
        # (исчерпан ретрай, краш), main() всё равно выйдет из shutdown_event.wait()
        # и запустит runner.cleanup().
        shutdown_event.set()


async def _run_pg_notify_listener(app: web.Application):
    """PostgreSQL LISTEN/NOTIFY → WebSocket bridge for parser-originated events."""
    shutdown_event = app.get('shutdown_event')

    db_pool = app.get('db_pool')
    ws_manager = app.get('websocket_manager')
    if not db_pool or not getattr(db_pool, 'pool', None) or not ws_manager:
        logger.warning("PG NOTIFY listener not started: missing db_pool or websocket_manager")
        return

    async def _handle_notify(channel: str, payload: str):
        try:
            data = json.loads(payload)
            if channel == 'events_new':
                await ws_manager.broadcast_event(data)
            elif channel == 'events_cleaned':
                await ws_manager.broadcast_events_cleaned(data)
        except Exception as e:
            logger.warning(f"Failed to process NOTIFY {channel}: {e}")

    async def _catch_up():
        """R-C5 Catch-Up: fetch recent events after LISTEN subscribe."""
        try:
            recent_events = await ws_manager.db_request.get_filtered_events_as_geojson(
                time_interval_minutes=5,
            )
            if recent_events and recent_events.get('features'):
                await ws_manager.send_snapshot(recent_events, channel='events_new')
                logger.info(f"Catch-Up: sent {len(recent_events['features'])} events_snapshot to WS clients")
        except Exception as e:
            logger.warning(f"Catch-Up failed: {e}")

    listener = PgNotifyListener(
        pool=db_pool.pool,
        channels=['events_new', 'events_cleaned'],
        handler=_handle_notify,
        shutdown_event=shutdown_event,
        label="pg_notify_ws",
        post_subscribe=_catch_up,
    )
    await listener.run()


async def on_startup(app: web.Application):
    """Actions to perform on application startup."""
    logger.info("--- ON_STARTUP CALLED ---")

    # Log bot token prefix for debugging (first 10 chars only, no secret exposed)
    bot_token = getattr(settings.bot, 'token', '')
    if bot_token:
        logger.debug(
            "Telegram bot token configured",
            extra={'extra_data': {'bot_token_prefix': bot_token[:10] + '...' if len(bot_token) > 10 else bot_token}}
        )
    else:
        logger.error("BOT_TOKEN is EMPTY - Telegram validation will fail!")

    # Предохранитель: при выключенной валидации /api/* и /ws принимают любого
    # клиента (jwt_auth_middleware и _ws_authenticate уходят в dev-bypass).
    # Это режим только для локальной разработки — громко предупреждаем, чтобы
    # случайно не уехало в прод.
    if not getattr(settings.app, 'telegram_webview_validation', True):
        logger.warning(
            "⚠️  TELEGRAM_WEBVIEW_VALIDATION=False — валидация Telegram initData/JWT "
            "ОТКЛЮЧЕНА: /api/* и /ws принимают ЛЮБОГО клиента. Только для локальной "
            "разработки — НЕ включать в production."
        )
    db_request: Request = app['db']
    bot: Bot = app['bot']
    dp: Dispatcher = app['dp']

    logger.info("--- Starting Sequential Initialization ---")

    logger.info("Step 1/3: Initializing in-memory cache...")
    await init_cache(app)

    logger.info("Step 2/3: Database schema initialization is handled by init.sql.")

    logger.info("Step 3/3: Starting background tasks...")

    shutdown_event = asyncio.Event()
    app['shutdown_event'] = shutdown_event

    app['bot_polling_task'] = asyncio.create_task(_run_bot_polling(app))
    logger.info("Мониторинг канала и удаление фото выполняются в сервисе parser (отдельный микросервис)")
    app['channel_monitor_task'] = None

    logger.info("PostgreSQL LISTEN для WebSocket-событий включён")
    app['pg_notify_task'] = asyncio.create_task(_run_pg_notify_listener(app))

    logger.info("Background tasks started.")
    logger.info("--- Initialization Complete ---")


async def on_shutdown(app: web.Application):
    """Actions to perform on application shutdown."""
    logger.info("Shutting down application...")

    shutdown_event = app.get('shutdown_event')
    if shutdown_event:
        shutdown_event.set()

    ws_manager = app.get('websocket_manager')
    if ws_manager:
        try:
            await asyncio.wait_for(ws_manager.close_all(), timeout=5.0)
            logger.info("WebSocket connections closed")
        except asyncio.TimeoutError:
            logger.warning("WebSocket close timed out")

    shutdown_tasks = []

    async def stop_bot_polling():
        """Останавливает polling бота и отменяет соответствующую задачу."""
        bot_polling_task = app.get('bot_polling_task')
        if bot_polling_task and not bot_polling_task.done():
            logger.info("Stopping bot polling...")
            dp: Dispatcher = app.get('dp')
            if dp:
                try:
                    await asyncio.wait_for(dp.stop_polling(), timeout=3.0)
                except (asyncio.TimeoutError, Exception) as e:
                    logger.debug(f"Dispatcher stop: {e}")
            bot_polling_task.cancel()
            try:
                await asyncio.wait_for(bot_polling_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception) as e:
                logger.debug(f"Bot polling cancellation: {e}")

    shutdown_tasks.append(stop_bot_polling())

    async def stop_pg_notify_listener():
        """Останавливает слушатель PostgreSQL NOTIFY с таймаутом."""
        pg_task = app.get('pg_notify_task')
        if not pg_task or pg_task.done():
            return
        # shutdown_event уже выставлен (см. начало on_shutdown) — listener сам
        # выходит из await и выполняет finally: снимает подписки и возвращает
        # соединение в пул. НЕ отменяем его здесь преждевременно — cancel прервал
        # бы release, соединение утекло бы, и graceful pool.close() завис бы до
        # terminate(). Отменяем только как аварийный fallback по таймауту.
        try:
            await asyncio.wait_for(pg_task, timeout=3.0)
        except asyncio.TimeoutError:
            pg_task.cancel()
            await asyncio.gather(pg_task, return_exceptions=True)
        except Exception:
            pass

    shutdown_tasks.append(stop_pg_notify_listener())

    try:
        await asyncio.wait_for(
            asyncio.gather(*shutdown_tasks, return_exceptions=True),
            timeout=10.0
        )
    except asyncio.TimeoutError:
        logger.warning("Some shutdown operations timed out, continuing...")

    bot: Bot = app.get('bot')
    if bot:
        try:
            await asyncio.wait_for(bot.session.close(), timeout=3.0)
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"Bot session close: {e}")

    cache: CacheManager = app.get('cache')
    if cache:
        try:
            await asyncio.wait_for(cache.close(), timeout=3.0)
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"Cache close: {e}")

    db_pool = app.get('db_pool')
    if db_pool:
        try:
            await asyncio.wait_for(db_pool.close(), timeout=5.0)
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"Database close: {e}")


async def create_app():
    """Creates and configures the aiohttp application."""
    db_pool = Database()
    try:
        await db_pool.connect(
            host=settings.db.host, port=settings.db.port, database=settings.db.database,
            user=settings.db.user, password=settings.db.password
        )
    except (asyncpg.PostgresError, OSError, ConnectionError, asyncio.TimeoutError) as e:
        logger.critical(f"Database connection failed after all retries: {e}")
        raise RuntimeError(f"Failed to connect to database: {e}") from e

    db_request = Request(db_pool)

    cache_manager = CacheManager()
    await cache_manager.connect()
    logger.info("In-memory cache initialized")

    _proxy_host = settings.parser.socks5_host or settings.parser.proxy_host
    _bot_session = None
    if _proxy_host:
        _proxy_url = f"{settings.parser.proxy_scheme}://{_proxy_host}:{settings.parser.proxy_port}"
        _bot_session = AiohttpSession(proxy=_proxy_url)

    bot = Bot(token=settings.bot.token, default=DefaultBotProperties(), session=_bot_session)
    dp = Dispatcher()
    dp.include_router(basic_router)

    rate_limiter = RateLimiter(
        default_limit=60,
        window_seconds=60,
        cleanup_interval=300
    )

    # Middleware chain. CSRF-проверка удалена: клиент авторизуется ТОЛЬКО
    # через Authorization: Bearer (JWT в sessionStorage) — cookie session_token
    # нигде не устанавливается, поэтому csrf_middleware всегда проходил
    # насквозь (мёртвый код). Bearer-токены браузер не отправляет
    # автоматически, CORS выключен (same-origin) — CSRF-вектор отсутствует.
    app = web.Application(middlewares=[
        logging_middleware,
        body_size_limit_middleware,
        jwt_auth_middleware,
        rate_limiter.middleware
    ])

    app['start_time'] = time.time()
    app['db_pool'] = db_pool
    app['db'] = db_request
    app['bot'] = bot
    app['dp'] = dp
    app['cache'] = cache_manager
    app['websocket_manager'] = WebSocketManager(db_request, cache_manager)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    setup_routes(app)

    # CORS: фронтенд приходит через тот же nginx (same-origin) — CORS вообще
    # не нужен в нормальном режиме. settings.app.allowed_origins пустой =
    # CORS выключен. При явно перечисленных origins-ах включаем CORS с
    # credentials на каждый перечисленный домен.
    allowed_origins = [
        o for o in settings.app.allowed_origins
        if o and o != '*'
    ]

    if allowed_origins:
        cors_defaults = {
            origin: aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
                allow_methods=["GET", "POST", "OPTIONS"]
            )
            for origin in allowed_origins
        }
        cors = aiohttp_cors.setup(app, defaults=cors_defaults)
        for route in list(app.router.routes()):
            cors.add(route)
        logger.info(f"CORS configured for explicit origins: {allowed_origins}")
    else:
        logger.info("CORS disabled (same-origin only) — ALLOWED_ORIGINS not set")

    return app
