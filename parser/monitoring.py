"""Monitoring — парсер Telegram каналов.

Клиент kurigram получает сообщения, предобрабатывает текст и записывает
в pending_events. NLP-пайплайн и вставка в events — в контейнере nlp_processor.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import asyncpg
from pyrogram import Client, filters
from pyrogram import errors
from pyrogram.types import Message

from common.settings import settings
from common.db.base import RETRYABLE_EXCEPTIONS as _TRANSIENT_ERRORS
from common.logging_config import setup_logging
from common.retry import retry_with_backoff
from common.pg_listener import PgNotifyListener

setup_logging(
    level=getattr(logging, settings.app.log_level.upper(), logging.INFO),
    json_format=settings.app.log_format.lower() == 'json',
)

from common.db_adapter import DBAdapter
from common.text_preprocessor import strip_tail, preprocess_light, truncate_for_geo, sanitize_text
from common.metrics import (
    parser_messages_processed_total,
    parser_messages_errors_total,
    parser_queue_size,
    parser_backpressure_active,
    start_metrics_server,
)


logger = logging.getLogger(__name__)

KIEV_TZ = ZoneInfo('Europe/Kiev')

_MIN_WORKERS = 2
_MAX_WORKERS = 8
_SCALE_UP_QSIZE = 20
_IDLE_TIMEOUT = 15


class ParserBot:
    """Бот для парсинга Telegram каналов — kurigram + предобработка + pending_events."""

    def __init__(self):
        """Инициализация бота: подключение к БД, Telegram клиент, очереди."""
        self.db: Optional[DBAdapter] = None
        self.app: Optional[Client] = None
        self._running = False
        self._messages_processed = 0
        self._errors = 0
        self._cleanup_listener_task: Optional[asyncio.Task] = None
        self._photo_listener_task: Optional[asyncio.Task] = None
        self._shutdown_started = False

        if not settings or not settings.bot or not settings.bot.channel_id:
            raise RuntimeError(
                "CHANNEL_ID not configured in settings. "
                "Check that CHANNEL_ID is set in .env and passed to the parser container."
            )
        self.channel_id = settings.bot.channel_id
        self.events_media_dir = settings.parser.events_media_dir

        self._pending_queue: asyncio.Queue = asyncio.Queue(maxsize=65)
        # Prometheus-гейджи очереди/backpressure стартуют в известном состоянии,
        # а не с дефолтом prometheus_client (0 после первой выборки и так верен).
        parser_queue_size.set(0)
        parser_backpressure_active.set(0)
        self._worker_tasks: list[asyncio.Task] = []
        self._worker_seq = 0
        self._adaptive_pool_task: Optional[asyncio.Task] = None
        self._idle_seconds = 0
        self._backpressure_active = False
        self._download_semaphore = asyncio.Semaphore(3)

    async def initialize(self) -> bool:
        """Инициализировать БД и Telegram клиент."""
        try:
            if not await self._init_database():
                return False
            if not await self._init_telegram_client():
                return False
            logger.info("✅ ParserBot initialized")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to initialize ParserBot: {e}")
            return False

    async def _init_database(self) -> bool:
        """Подключиться к PostgreSQL и проверить схему."""
        logger.info("Connecting to PostgreSQL...")
        self.db = DBAdapter()
        if not await self.db.connect():
            logger.error("Failed to connect to PostgreSQL, exiting")
            return False
        logger.info("✅ PostgreSQL connected")

        logger.info("Ensuring database schema...")
        if not await self.db.ensure_schema():
            logger.error("Failed to ensure database schema, exiting")
            return False
        return True

    async def _init_telegram_client(self) -> bool:
        """Запустить pyrogram клиент с сессией и прокси (если настроен)."""
        session_path = os.path.join("/app/parser", "session.session")
        if not os.path.exists(session_path):
            logger.error(
                f"❌ Session file not found: {session_path}. "
                "Файл session.session должен быть создан администратором "
                "вручную и смонтирован в контейнер (volume)."
            )
            return False

        proxy_host = settings.parser.socks5_host or settings.parser.proxy_host
        proxy_config = None
        if proxy_host:
            proxy_config = {
                "scheme": settings.parser.proxy_scheme,
                "hostname": proxy_host,
                "port": settings.parser.proxy_port,
            }
            logger.info(
                f"Using proxy: {proxy_config['scheme']}://"
                f"{proxy_config['hostname']}:{proxy_config['port']}"
            )

        try:
            async def _start_client():
                self.app = Client(
                    name="session",
                    workdir="/app/parser",
                    **({"proxy": proxy_config} if proxy_config else {})
                )
                logger.info("Starting Telegram client...")
                await self.app.start()
                logger.info("✅ Telegram client started successfully")
                return True

            return await retry_with_backoff(
                func=_start_client,
                max_attempts=3,
                max_transient_attempts=3,
            retryable_exceptions=(
                errors.AuthKeyInvalid,
                errors.SessionExpired,
                errors.RPCError,
            ),
                base_delay=1.0,
                max_delay=30.0,
                label="telegram-client-start",
            )
        except Exception:
            logger.error(
                "❌ Telegram client failed after retries. "
                "If session is revoked, regenerate it: python gen_session.py",
                exc_info=True,
            )
            return False

    async def _load_chat_history(self):
        """Загрузить историю сообщений канала в очередь."""
        try:
            logger.info(f"Loading history from channel {self.channel_id}...")
            await self._warmup_peer()

            count = 0
            async for message in self.app.get_chat_history(
                chat_id=self.channel_id,
                limit=settings.parser.history_limit,
            ):
                await self._pending_queue.put(message)
                count += 1
                if count % 50 == 0:
                    logger.debug(f"History loading progress: {count} messages")

            logger.info(f"✅ Chat history queued: {count} messages")
        except Exception as e:
            logger.error(f"Failed to load chat history: {e}")

    async def _warmup_peer(self) -> bool:
        """Найти канал в списке диалогов для прогрева кэша peer."""
        target_id = int(self.channel_id)
        try:
            async for dialog in self.app.get_dialogs():
                if dialog.chat.id == target_id:
                    logger.info(f"Peer cache warmed for channel {self.channel_id}")
                    return True
            logger.warning(
                f"Channel {self.channel_id} not found in dialogs — "
                "peer cache not warmed, history load may fail"
            )
            return False
        except Exception as e:
            logger.warning(f"Peer warmup failed ({e}) — will attempt history load anyway")
            return False

    async def start(self):
        """Запустить обработку сообщений: воркеры, адаптивный пул, слушатели."""
        self._running = True

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_stop)

        logger.info("🚀 Starting parser bot...")

        # Метрики для prometheus (9100) — до основных подсистем, чтобы скрейп
        # был доступен даже при проблемах с историей/слушателями. Сервер не
        # роняет парсер: при ошибке в лог уходит warning (см. common.metrics).
        if start_metrics_server(9100):
            logger.info("Prometheus metrics server started on port 9100")
        else:
            logger.warning("Metrics server not started on port 9100")

        target_filter = filters.chat(int(self.channel_id)) & (
            filters.text | filters.caption | filters.photo
        )

        @self.app.on_message(target_filter)
        async def handle_message(client: Client, message: Message):
            if not self._running:
                return

            # put_nowait — НЕ блокируем хендлер pyrogram: при полной очереди
            # QueueFull выбрасывается мгновенно. Вместо дропа пишем сообщение
            # НАПРЯМУЮ в pending_events (at-least-once): очередь — это буфер
            # скорости, а не фильтр потерь (раньше переполнение = потеря
            # события навсегда, без DLQ и повторной выборки).
            try:
                self._pending_queue.put_nowait(message)
                parser_messages_processed_total.inc()
                return
            except asyncio.QueueFull:
                logger.warning(
                    f"Message {message.id}: queue full ({self._pending_queue.qsize()}/65) "
                    "— direct DB write (backpressure)"
                )
            except Exception as e:
                logger.error(f"Message {message.id}: enqueue failed: {e}")

            try:
                await self._process_message(message)
            except Exception as e:
                self._errors += 1
                parser_messages_errors_total.inc()
                logger.error(f"Message {message.id}: direct write failed: {e}")

        try:
            if not self.app.is_connected:
                logger.error("Telegram client is not connected - check initialization")
                raise Exception("Telegram client not properly initialized")

            logger.info("✅ Telegram client already running")

            for _ in range(_MIN_WORKERS):
                self._spawn_worker()
            logger.info(f"Started {_MIN_WORKERS} initial queue worker(s)")

            self._adaptive_pool_task = asyncio.create_task(
                self._adaptive_pool_runner()
            )

            await self._load_chat_history()

            self._cleanup_listener_task = asyncio.create_task(
                self._run_photo_cleanup_listener()
            )
            self._photo_listener_task = asyncio.create_task(
                self._run_photo_download_listener()
            )

                    # H4: догоняем фото, чей NOTIFY photo_download был потерян
            # (parser был недоступен/рестартовал в момент перехода status→'done'
            # — триггер больше не сработает).
            await self._recover_missing_photos()

            # P1.2: страховка от потери pg_notify — удаление фото старше 70 минут
            self._stale_photo_task = asyncio.create_task(self._run_stale_photo_cleaner())

            while self._running:
                self._write_heartbeat()
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Telegram client error: {e}")
            raise

    @staticmethod
    def _to_kiev(dt: Optional[datetime]) -> datetime:
        """Привести datetime к киевскому часовому поясу."""
        if dt is None:
            return datetime.now(KIEV_TZ)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KIEV_TZ)

    @staticmethod
    def _write_heartbeat():
        """Записать timestamp в /tmp/parser_heartbeat для healthcheck."""
        try:
            with open('/tmp/parser_heartbeat', 'w') as f:  # nosec B108 — container /tmp, Docker healthcheck
                f.write(str(int(datetime.now(timezone.utc).timestamp())))
        except OSError:
            pass

    def _spawn_worker(self) -> asyncio.Task:
        """Создать и запустить новый воркер очереди."""
        worker_id = self._worker_seq
        self._worker_seq += 1
        task = asyncio.create_task(self._pending_worker(worker_id))
        task.add_done_callback(self._supervise_worker)
        self._worker_tasks.append(task)
        return task

    def _supervise_worker(self, task):
        """Перезапустить воркер, если он упал с исключением."""
        if not self._running:
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        logger.critical(f"Queue worker died unexpectedly ({exc!r}) — respawning")
        self._spawn_worker()

    def _remove_worker(self) -> bool:
        """Отменить и удалить одного воркера (если превышен минимум)."""
        if len(self._worker_tasks) <= _MIN_WORKERS:
            return False
        task = self._worker_tasks.pop()
        task.cancel()
        return True

    async def _adaptive_pool_runner(self):
        """Масштабировать число воркеров по размеру очереди."""
        logger.info("Adaptive pool runner started")
        while self._running:
            await asyncio.sleep(3)
            if not self._running:
                break
            qsize = self._pending_queue.qsize()
            n_workers = len(self._worker_tasks)
            # Экспорт состояния очереди в Prometheus (каждые 3s цикла).
            parser_queue_size.set(qsize)
            parser_backpressure_active.set(1 if self._backpressure_active else 0)
            
            # Backpressure: если очередь почти полная, замедляем прием
            if qsize >= 60:  # 60/65 = 92% full
                if not self._backpressure_active:
                    logger.warning(f"Backpressure ACTIVE: queue at {qsize}/65")
                    self._backpressure_active = True
            elif qsize < 40:
                if self._backpressure_active:
                    logger.info(f"Backpressure RELEASED: queue at {qsize}/65")
                    self._backpressure_active = False

            if qsize > _SCALE_UP_QSIZE and n_workers < _MAX_WORKERS:
                self._spawn_worker()
                self._idle_seconds = 0
                logger.info(
                    f"Scaled up: {n_workers + 1} workers (queue={qsize})"
                )
            elif qsize == 0:
                self._idle_seconds += 3
                if self._idle_seconds >= _IDLE_TIMEOUT and n_workers > _MIN_WORKERS:
                    if self._remove_worker():
                        self._idle_seconds = 0
                        logger.info(
                            f"Scaled down: {n_workers - 1} workers (idle)"
                        )
            else:
                self._idle_seconds = 0

    async def _pending_worker(self, worker_id: int):
        """Воркер: берёт message из очереди, предобрабатывает, пишет в pending_events."""
        logger.info(f"Pending worker {worker_id} started")
        while True:
            message = await self._pending_queue.get()
            try:
                await self._process_message_with_retries(message)
            except Exception as e:
                self._errors += 1
                parser_messages_errors_total.inc()
                logger.error(
                    f"Worker {worker_id}: message {message.id} failed permanently: {e}"
                )
            finally:
                self._pending_queue.task_done()

    async def _process_message_with_retries(self, message):
        await retry_with_backoff(
            self._process_message,
            args=(message,),
            max_attempts=1,
            max_transient_attempts=5,
            label=f"Message {message.id}",
        )

    @staticmethod
    def _extract_text(message) -> str:
        """Извлечь текст или caption из сообщения."""
        return str(message.text or message.caption or '')

    async def _process_message(self, message: Message):
        """Предобработка сообщения и запись в pending_events."""
        if str(message.chat.id) != str(self.channel_id):
            logger.debug(f"Skipping message from wrong channel: {message.chat.id}")
            return

        if not (message.text or message.caption or message.photo):
            logger.debug(f"Message {message.id}: no text/caption/photo, skipped")
            return

        start_time = datetime.now(timezone.utc)
        message_id = message.id

        raw_text = self._extract_text(message)
        stripped = strip_tail(raw_text)
        preserved = sanitize_text(preprocess_light(stripped)) or ''
        preserved = truncate_for_geo(preserved, settings.parser.max_text_length)

        photo_file_id = None
        if message.photo:
            photo_file_id = message.photo.file_id

        try:
            await self.db.pool.execute(
                """INSERT INTO pending_events
                   (message_id, text, event_time, photo_file_id)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (message_id, event_time) DO NOTHING""",
                message_id, preserved, self._to_kiev(message.date), photo_file_id,
            )
            self._messages_processed += 1
            parser_messages_processed_total.inc()
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            logger.debug(
                f"Message {message_id} enqueued in {elapsed:.2f}s "
                f"(photo={'yes' if photo_file_id else 'no'})"
            )
        except Exception as e:
            self._errors += 1
            logger.error(f"Message {message_id}: failed to enqueue: {e}")
            raise

    async def _run_photo_download_listener(self):
        """Слушать NOTIFY photo_download и скачивать фото через pyrogram."""
        async def _handle_notify(channel: str, payload: str):
            try:
                data = json.loads(payload)
                await self._download_photo_by_notify(data)
            except Exception as e:
                logger.warning(f"Photo download handler error: {e}")

        self._photo_listener = PgNotifyListener(
            pool=self.db.pool,
            channels=['photo_download'],
            handler=_handle_notify,
            shutdown_event=asyncio.Event() if not hasattr(self, '_shutdown_event') else self._shutdown_event,
            label="photo_download",
        )
        await self._photo_listener.run()

    async def _recover_missing_photos(self):
        """Найти события без photo_url, чьё скачивание не было запрошено.

        Триггер notify_photo_download шлёт NOTIFY только в момент перехода
        pending_events.status → 'done'. Если parser в этот момент не слушал
        канал (старт/рестарт), NOTIFY теряется и фото не скачивается никогда.
        Догоняющий запрос при старте закрывает этот пробел.
        """
        try:
            rows = await self.db.pool.fetch(
                """SELECT e.id AS event_id, pe.message_id, pe.photo_file_id
                   FROM pending_events pe
                   JOIN events e
                     ON e.message_id = pe.message_id
                    AND e.event_time = pe.event_time
                   WHERE pe.status = 'done'
                     AND pe.photo_file_id IS NOT NULL
                     AND e.photo_url IS NULL"""
            )
        except Exception as e:
            logger.warning(f"Photo recovery query failed: {e}")
            return
        if not rows:
            return
        logger.info(f"Recovering {len(rows)} missing photo download(s)")
        for row in rows:
            try:
                await self._download_photo_by_notify(dict(row))
            except Exception as e:
                logger.warning(
                    f"Photo recovery failed for event {row['event_id']}: {e}"
                )

    async def _download_photo_by_notify(self, data: dict):
        """Скачать фото по file_id из NOTIFY payload."""
        event_id = data.get('event_id')
        message_id = data.get('message_id')
        file_id = data.get('photo_file_id')

        if not file_id:
            return

        try:
            photo_url = await self._download_photo_by_file_id(file_id, message_id)
            if photo_url:
                await self.db.pool.execute(
                    "UPDATE events SET photo_url = $1 WHERE id = $2 AND photo_url IS NULL",
                    photo_url, event_id,
                )
                logger.info(f"Event {event_id}: photo attached: {photo_url}")
            else:
                logger.debug(f"Event {event_id}: photo download returned None")
        except Exception as e:
            logger.warning(f"Event {event_id}: photo download failed: {e}")

    async def _download_photo_by_file_id(self, file_id: str, message_id: int) -> Optional[str]:
        """Скачать фото по pyrogram file_id и вернуть публичный URL."""
        try:
            if not self.events_media_dir:
                return None

            from pathlib import Path

            target_dir = Path(self.events_media_dir).resolve()
            target_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
            safe_msg_id = abs(int(message_id))
            filename = Path(f"event_{timestamp}_{safe_msg_id}.jpg").name
            final_path = (target_dir / filename).resolve()

            try:
                final_path.relative_to(target_dir)
            except ValueError:
                logger.error(f"Path traversal blocked: {final_path} not under {target_dir}")
                return None

            if final_path.is_symlink():
                logger.warning(f"Removing pre-existing symlink: {final_path}")
                final_path.unlink()

            async with self._download_semaphore:
                await self.app.download_media(file_id, file_name=str(final_path))

            public_url = f"/media/events/{filename}"
            logger.debug(f"Downloaded photo to {final_path} → URL {public_url}")
            return public_url

        except Exception as e:
            logger.error(f"Failed to download photo by file_id: {e}")
            return None

    async def _run_photo_cleanup_listener(self):
        """Слушать NOTIFY events_cleaned и удалять физические файлы фото."""
        media_dir = self.events_media_dir.rstrip('/')

        def _resolve_photo_path(url: str) -> Optional[str]:
            if not url:
                return None
            if url.startswith('/media/events/'):
                return f"{media_dir}/{url[len('/media/events/'):]}"
            return url

        async def _handle_notify(channel: str, payload: str):
            try:
                data = json.loads(payload)
                deleted = 0
                for url in data.get('photo_urls') or []:
                    path = _resolve_photo_path(url)
                    if path and os.path.isfile(path):
                        try:
                            os.unlink(path)
                            deleted += 1
                        except OSError as e:
                            logger.warning(f"Failed to delete photo {path}: {e}")
                if deleted:
                    logger.info(f"Cleaned up {deleted} stale photo(s)")
            except Exception as e:
                logger.warning(f"Photo cleanup handler error: {e}")

        self._cleanup_listener = PgNotifyListener(
            pool=self.db.pool,
            channels=['events_cleaned'],
            handler=_handle_notify,
            shutdown_event=asyncio.Event() if not hasattr(self, '_shutdown_event') else self._shutdown_event,
            label="events_cleaned",
        )
        await self._cleanup_listener.run()

    def _request_stop(self):
        """Установить флаг остановки для graceful shutdown."""
        logger.info("Stop signal received — requesting graceful shutdown")
        self._running = False

    async def _run_stale_photo_cleaner(self):
        """Периодическая очистка фото старше 70 минут (страховка от потери pg_notify)."""
        while self._running:
            await self._cleanup_stale_photos()
            await asyncio.sleep(300)

    async def _cleanup_stale_photos(self):
        """Удалять файлы старше 70 минут, на которые больше НЕ ссылается ни одно
        событие (страховка от потери pg_notify).

        Раньше файлы старше 70 минут удалялись безусловно — но живое событие
        (60 минут в БД) могло ещё существовать, а его фото уже нет → битый src
        и 401 на API-fallback. Теперь удаляются только orphan-файлы: событие
        удалено из events (clean_old_events / partition_overflow) → строки нет →
        фото больше не нужно. Ссылающиеся фото живут столько же, сколько
        событие.
        """
        cutoff = time.time() - 70 * 60
        media_dir = Path(self.events_media_dir)
        if not media_dir.exists() or self.db is None or self.db.pool is None:
            return

        candidates = [
            f for f in media_dir.iterdir()
            if f.is_file() and f.stat().st_mtime < cutoff
        ]
        if not candidates:
            return

        try:
            async with self.db.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT photo_url FROM events WHERE photo_url LIKE $1",
                    "/media/events/%",
                )
        except Exception as e:
            logger.warning(f"stale photo DB check failed, skipping cleanup: {e}")
            return

        referenced = {row['photo_url'] for row in rows}

        deleted = 0
        for f in candidates:
            url = f"/media/events/{f.name}"
            if url in referenced:
                continue
            try:
                f.unlink()
                deleted += 1
            except FileNotFoundError:
                pass  # идемпотентно
            except Exception as e:
                logger.warning(f"stale photo delete failed: {f}: {e}")
        if deleted:
            logger.info(f"Stale photos cleaned (unreferenced): {deleted}")

    async def shutdown(self, drain_timeout: float = 20.0):
        """Корректно остановить бота: дождаться очереди, отменить задачи, закрыть соединения."""
        if self._shutdown_started:
            return
        self._shutdown_started = True

        logger.info("Shutting down parser...")
        self._running = False

        if self._worker_tasks and not self._pending_queue.empty():
            pending = self._pending_queue.qsize()
            logger.info(f"Draining pending queue ({pending} buffered)...")
            try:
                await asyncio.wait_for(self._pending_queue.join(), timeout=drain_timeout)
                logger.info("Pending queue drained")
            except asyncio.TimeoutError:
                logger.warning(
                    f"Queue drain timed out, {self._pending_queue.qsize()} message(s) "
                    "left unprocessed (recoverable via backfill on restart)"
                )

        tasks = [t for t in (*self._worker_tasks, self._cleanup_listener_task,
                              self._photo_listener_task, self._adaptive_pool_task,
                              self._stale_photo_task)
                 if t and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self.app:
            try:
                await self.app.stop()
                logger.info("Telegram client stopped")
            except Exception as e:
                logger.error(f"Error stopping Telegram client: {e}")

        if self.db:
            await self.db.close()

        logger.info(
            f"Parser stopped. Processed: {self._messages_processed}, "
            f"Errors: {self._errors}"
        )


async def main():
    """Точка входа: инициализировать, запустить, обработать прерывания."""
    parser = ParserBot()
    try:
        success = await parser.initialize()
        if not success:
            logger.error("Failed to initialize, exiting")
            sys.exit(1)
        await parser.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
    finally:
        await parser.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
