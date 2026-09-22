"""Processor — NLP pipeline: потребляет из pending_events, обрабатывает, пишет в events."""

import asyncio
import gc
import json as json_lib
import logging
import math
import os
import random
import signal
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

from common.settings import settings
from common.logging_config import setup_logging
from common.circuit_breaker import CircuitBreaker, CircuitState
from common.retry import retry_with_backoff

setup_logging(
    level=getattr(logging, settings.app.log_level.upper(), logging.INFO),
    json_format=settings.app.log_format.lower() == 'json',
)

from common.db_adapter import DBAdapter
from common.metrics import (
    processor_messages_processed_total,
    processor_messages_errors_total,
    processor_messages_expired_total,
    processor_worker_active,
    processor_circuit_breaker_state,
)
from .morphology import Morphology
from .phonetic_index import PhoneticIndex
from .geo_matcher import GeoMatcher
from .health import HealthServer
from .layer_classifier import LayerClassifier
from .word_tokenizer import tokenize
from common.text_preprocessor import is_promotional, truncate_for_geo

logger = logging.getLogger(__name__)

_MAX_WORKER_CONCURRENCY = 8

# Интервал зависания задачи в статусе 'processing' — после этого фоновый
# очиститель возвращает её в 'pending' (R-PR11). Передаётся параметром
# (R-PR20) как timedelta — asyncpg кодирует его в native interval.
_STALE_PROCESSING_INTERVAL = timedelta(minutes=5)
# Периодичность прогона очистителя зависших задач.
_CLEANER_INTERVAL = 60.0

# Маппинг CircuitState → числовое значение гейджа processor_circuit_breaker_state
# (0=closed 1=half_open 2=open): ненулевое значение удобно алертить как деградацию БД.
_CIRCUIT_STATE_PROM = {
    CircuitState.CLOSED: 0,
    CircuitState.HALF_OPEN: 1,
    CircuitState.OPEN: 2,
}


# Общий хвост обоих INSERT-запросов: инкремент events_meta и pg_notify.
# Держится в одном месте — раньше блок дублировался в двух запросах и разъезжался.
# R-DB0: minimal payload to stay well under pg_notify 8KB limit.
# Core fetches full Feature via targeted SELECT or CacheManager.
_INSERT_TAIL = """
    meta_upd AS (
        UPDATE events_meta
        SET version = version + 1,
            updated_at = now(),
            max_event_id = (SELECT id FROM inserted)
        WHERE id = 1 AND EXISTS (SELECT 1 FROM inserted)
        RETURNING 1
    ),
    notify_call AS (
        SELECT pg_notify(
            'events_new',
            jsonb_build_object(
                'id', i.id,
                'layer', i.layer,
                'strategy', i.strategy
            )::text
        )
        FROM inserted i
    )
    SELECT i.id, i.layer, i.strategy
    FROM inserted i,
         (SELECT count(*) FROM notify_call) _force_notify
"""

_INSERT_EVENT_SIMPLE = f"""
    WITH inserted AS (
        INSERT INTO events
            (message_id, event_time, description, photo_url,
             layer, strategy, geom, matches)
        VALUES ($1, $2, $3, $4, $5, $6, ST_GeomFromText($7, 4326), '[]'::jsonb)
        ON CONFLICT (message_id, event_time) DO NOTHING
        RETURNING id, event_time, geom, layer, strategy, description,
                  photo_url, matches
    ),
    {_INSERT_TAIL}
"""

_INSERT_EVENT_FROM_CANDIDATES = f"""
    WITH pc AS (
        SELECT result_strategy, result_geom, result_matches,
               result_confidence, result_diagnostics
        FROM process_candidates_v2(
            $6::int[], $7::double precision[], $8::text[], $9::varchar,
            $10::double precision, $11::double precision, $12::double precision
        )
    ),
    inserted AS (
        INSERT INTO events
            (message_id, event_time, description, photo_url,
             layer, strategy, geom, matches, confidence, geo_diagnostics)
        SELECT $1, $2, $3, $4, $5,
               pc.result_strategy, pc.result_geom, pc.result_matches,
               pc.result_confidence, pc.result_diagnostics
        FROM pc
        WHERE pc.result_strategy != 'random_null'
        ON CONFLICT (message_id, event_time) DO NOTHING
        RETURNING id, event_time, geom, layer, strategy, description,
                  photo_url, matches
    ),
    {_INSERT_TAIL}
"""


class ProcessorBot:
    """NLP processor: потребляет pending_events, обрабатывает, пишет в events."""

    def __init__(self):
        """Инициализация процессора: подсистемы NLP, БД, health-сервер."""
        self.db: Optional[DBAdapter] = None
        self._running = False
        self._messages_processed = 0
        self._errors = 0
        self._expired = 0
        self._worker_tasks: list[asyncio.Task] = []
        self._worker_seq = 0
        self._shutdown_started = False
        self._listen_conn: Optional[asyncpg.Connection] = None
        self._cleaner_task: Optional[asyncio.Task] = None
        # Идентификатор этого процесса для записи в pending_events.worker_id
        # при двухфазном claim задачи (R-PR11).
        self._worker_id = f"proc-{os.getpid()}-{uuid.uuid4().hex[:6]}"

        self.morph = Morphology()
        self.index = PhoneticIndex(self.morph)
        self.matcher = GeoMatcher(self.morph, self.index)
        self.health_server = HealthServer()
        self.layer_classifier = LayerClassifier(self.morph)

        self._worker_concurrency = max(
            1, min(_MAX_WORKER_CONCURRENCY, settings.processor.worker_concurrency)
        )
        self._poll_interval = settings.processor.poll_interval

        # asyncio.Event для корректного shutdown через signal handler.
        # Используется в _request_stop() — set() через call_soon_threadsafe
        # безопасен из любого потока/signal handler.
        self._shutdown_event = asyncio.Event()

        # Circuit breaker для защиты БД
        self._circuit_breaker = CircuitBreaker(failure_threshold=5, timeout=60.0)
        # Prometheus: стартовое состояние защитной сетки (0 = closed).
        processor_circuit_breaker_state.set(0)

    async def initialize(self) -> bool:
        """Инициализация всех компонентов: БД, NLP, подписки PG notify."""
        try:
            if not await self._init_database():
                return False
            if not await self._init_nlp():
                return False
            logger.info("✅ ProcessorBot initialized")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to initialize ProcessorBot: {e}")
            return False

    async def _init_database(self) -> bool:
        """Подключение к PostgreSQL через DBAdapter."""
        logger.info("Connecting to PostgreSQL...")
        self.db = DBAdapter()
        if not await self.db.connect():
            logger.error("Failed to connect to PostgreSQL, exiting")
            return False
        if not await self._ensure_pending_schema():
            logger.error("Failed to ensure pending_events schema, exiting")
            return False
        logger.info("✅ PostgreSQL connected")
        return True

    async def _ensure_pending_schema(self) -> bool:
        """Идемпотентно дополнить схему pending_events колонками для R-PR11.

        Init-скрипты PostgreSQL (`/docker-entrypoint-initdb.d`) выполняются
        только при создании тома данных; на уже существующем томе колонки
        locked_at/worker_id нужно добавить здесь — безопасно для повторов
        (R-DB22, R-DB23: всё через IF NOT EXISTS).
        """
        try:
            async with self.db.pool.acquire() as conn:
                await conn.execute(
                    "ALTER TABLE pending_events "
                    "ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ"
                )
                await conn.execute(
                    "ALTER TABLE pending_events "
                    "ADD COLUMN IF NOT EXISTS worker_id TEXT"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pending_events_stale "
                    "ON pending_events(locked_at) "
                    "WHERE status = 'processing'"
                )
            logger.info("✅ pending_events schema ensured: locked_at/worker_id")
            return True
        except Exception as e:
            logger.error(f"❌ _ensure_pending_schema failed: {e}")
            return False

    async def _init_nlp(self) -> bool:
        """Загрузка и инициализация всех NLP-компонентов (матчеры, резолверы)."""
        logger.info("Initializing NLP pipeline...")
        try:
            logger.info("Initializing GeoMatcher + PhoneticIndex...")
            if not await self.matcher.initialize(self.db.pool):
                logger.error("GeoMatcher initialization failed")
                return False

            logger.info("Setting up PostgreSQL notifications...")
            await self._setup_pg_notify()

            self.health_server.set_initialized(True)

            logger.info("✅ NLP pipeline initialized")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to initialize NLP pipeline: {e}")
            return False

    async def _setup_pg_notify(self):
        """Подписка на канал geo_updated для автообновления индекса."""
        try:
            self._listen_conn = await self.db.pool.acquire()
            await self._listen_conn.add_listener("geo_updated", self._on_geo_updated)
            logger.info("Subscribed to geo_updated channel")
        except Exception as e:
            logger.error(f"Failed to setup pg_notify: {e}")

    async def _on_geo_updated(self, conn, pid, channel, payload):
        """Обработка уведомления об изменении geo-данных."""
        logger.info("🔄 geo_updated received, reindexing...")

        async def _reindex(func, *args):
            """Перестроить индекс после обновления geo-данных."""
            try:
                await func(*args)
                logger.info("✅ Reindexing completed")
            except Exception as e:
                logger.error(f"❌ Reindexing failed: {e}")

        def _on_reindex_done(task):
            """Обработка результата фоновой перестройки индекса."""
            try:
                exc = task.exception()
                if exc is not None:
                    logger.error(f"❌ Background reindex task crashed: {exc}")
            except asyncio.CancelledError:
                pass

        try:
            geo_id = json_lib.loads(payload).get('geo_id')
        except Exception:
            geo_id = None

        if geo_id:
            task = asyncio.create_task(_reindex(self.matcher.reindex_geo, self.db.pool, geo_id))
        else:
            task = asyncio.create_task(_reindex(self.matcher.reindex_all, self.db.pool))
        task.add_done_callback(_on_reindex_done)

    async def run(self):
        """Главный цикл: запуск health-сервера и воркеров."""
        self._running = True

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_stop)

        logger.info("🚀 Starting processor...")

        try:
            await self.health_server.start(port=8765)
        except Exception as e:
            logger.error(f"❌ Failed to start health server: {e}")

        # Фоновый очиститель зависших 'processing' задач (R-PR11):
        # покрывает падения воркеров (SIGKILL/OOM), когда requeue невозможен.
        self._cleaner_task = asyncio.create_task(self._cleanup_stale_processing())
        self._cleaner_task.add_done_callback(self._on_cleaner_done)

        for _ in range(self._worker_concurrency):
            self._spawn_worker()
        logger.info(f"Started {self._worker_concurrency} worker(s)")

        while not self._shutdown_event.is_set():
            self.health_server.touch()
            # R-PR4: memory fallback every iteration
            if not self.health_server.check_memory():
                if not self.health_server._memory_warning_sent:
                    logger.warning("⚠️  RSS approaching 1GB limit — applying graceful degradation")
                    self.health_server._memory_warning_sent = True
                self._apply_memory_fallback()
            else:
                self.health_server._memory_warning_sent = False
            # R-PR2: не даём _worker_tasks расти при рестартах воркеров.
            self._worker_tasks = [t for t in self._worker_tasks if not t.done()]
            processor_worker_active.set(len(self._worker_tasks))
            processor_circuit_breaker_state.set(
                _CIRCUIT_STATE_PROM[self._circuit_breaker.state]
            )
            self._write_heartbeat(self)
            await asyncio.sleep(1)

    def _spawn_worker(self) -> asyncio.Task:
        """Запуск нового воркера с автонадзором."""
        # R-PR2: завершённые задачи удаляются — иначе список монотонно растёт
        # при каждом respawn упавшего воркера.
        self._worker_tasks = [t for t in self._worker_tasks if not t.done()]
        worker_id = self._worker_seq
        self._worker_seq += 1
        task = asyncio.create_task(self._worker(worker_id))
        task.add_done_callback(self._supervise_worker)
        self._worker_tasks.append(task)
        return task

    def _supervise_worker(self, task):
        """Перезапуск упавшего воркера."""
        if not self._running:
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        logger.critical(f"Worker died unexpectedly ({exc!r}) — respawning")
        self._spawn_worker()

    async def _worker(self, worker_id: int):
        """Цикл обработки: выбирает pending-событие, обрабатывает, помечает done/error."""
        logger.info(f"Worker {worker_id} started")
        while self._running:
            # Circuit breaker check
            if self._circuit_breaker.state == CircuitState.OPEN:
                logger.warning(f"Worker {worker_id}: circuit breaker is OPEN, backing off")
                await asyncio.sleep(self._poll_interval * 10)
                continue
            
            try:
                row = await self._circuit_breaker.call(self._fetch_pending)
            except Exception as e:
                logger.error(f"Worker {worker_id}: circuit breaker prevented fetch: {e}")
                await asyncio.sleep(self._poll_interval * 5)
                continue
            
            if not row:
                await asyncio.sleep(self._poll_interval)
                continue

            try:
                await self._process_row_with_retries(row)
            except asyncio.CancelledError:
                # Shutdown: задача остаётся 'processing' — её вернёт
                # в 'pending' фоновый очиститель (R-PR11).
                raise
            except Exception as e:
                # Защитная сетка: неучтённая ошибка → задача снова доступна
                # для других воркеров сразу, не дожидаясь очистителя.
                self._errors += 1
                processor_messages_errors_total.inc()
                logger.error(
                    f"Worker {worker_id}: message {row['message_id']} "
                    f"crashed with unhandled error: {e} — requeueing"
                )
                try:
                    await self._requeue(row['id'])
                except Exception as requeue_err:
                    logger.error(
                        f"Worker {worker_id}: requeue of {row['message_id']} "
                        f"failed: {requeue_err}"
                    )

    async def _process_row_with_retries(self, row):
        """Обработка одной задачи с ретраями; финально помечает done/error."""
        msg_id = row['message_id']

        async def _try_process():
            result = await self._process_row(row)
            if result is None:
                # expired — уже помечена внутри _process_row
                return None
            if result:
                await self._mark_done(row['id'])
                self._messages_processed += 1
                processor_messages_processed_total.inc()
                logger.info(
                    f"✅ Message {msg_id} processed: "
                    f"event_id={result['event_id']}, layer={result['layer']}, "
                    f"strategy={result.get('strategy', '?')}"
                )
            else:
                await self._mark_done(row['id'])
                logger.debug(f"Message {msg_id}: duplicate or no geometry")

        try:
            await retry_with_backoff(
                _try_process,
                args=(),
                max_attempts=3,
                max_transient_attempts=8,
                label=f"Message {msg_id}",
            )
        except Exception as e:
            self._errors += 1
            processor_messages_errors_total.inc()
            await self._mark_error(row['id'], str(e))
            logger.error(f"Message {msg_id}: failed permanently: {e}")

    async def _fetch_pending(self):
        """Атомарный захват одной pending-задачи: двухфазный claim (R-PR11).

        Статус меняется на 'processing' в той же транзакции, что и
        FOR UPDATE SKIP LOCKED — после commit задача недоступна другим
        воркерам (нет «закрыл транзакцию, а статус остался pending»).
        Фильтр event_time предотвращает выборку устаревших сообщений.
        """
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                return await conn.fetchrow(
                    "UPDATE pending_events "
                    "SET status = 'processing', locked_at = now(), worker_id = $1 "
                    "WHERE id = ("
                    "    SELECT id FROM pending_events "
                    "    WHERE status = 'pending' "
                    "      AND event_time >= now() - interval '60 minutes' "
                    "      AND event_time <= now() + interval '5 minutes' "
                    "    ORDER BY created_at "
                    "    LIMIT 1 "
                    "    FOR UPDATE SKIP LOCKED"
                    ") "
                    "RETURNING id, message_id, text, event_time, photo_file_id",
                    self._worker_id,
                )

    async def _mark_done(self, row_id: int):
        """Пометить pending-событие как выполненное."""
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE pending_events "
                "SET status = 'done', processed_at = now(), "
                "    locked_at = NULL, worker_id = NULL "
                "WHERE id = $1",
                row_id,
            )

    async def _mark_error(self, row_id: int, error_msg: str):
        """Пометить pending-событие как ошибочное."""
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE pending_events "
                "SET status = 'error', error_message = $1, "
                "    locked_at = NULL, worker_id = NULL "
                "WHERE id = $2",
                error_msg, row_id,
            )

    async def _requeue(self, row_id: int):
        """Вернуть задачу в 'pending' после неучтённой ошибки воркера.

        Guard `status = 'processing'` защищает от повторного перевода задачи,
        которая в другом потоке уже была помечена 'done'/'error'.
        """
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE pending_events "
                "SET status = 'pending', locked_at = NULL, worker_id = NULL "
                "WHERE id = $1 AND status = 'processing'",
                row_id,
            )

    async def _mark_expired(self, row_id: int):
        """Пометить pending-событие как истёкшее (event_time вне окна)."""
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE pending_events "
                "SET status = 'expired', processed_at = now(), "
                "    locked_at = NULL, worker_id = NULL "
                "WHERE id = $1",
                row_id,
            )

    async def _cleanup_stale_processing(self):
        """Фоновый очиститель зависших 'processing' задач (R-PR11).

        Работает каждые _CLEANER_INTERVAL секунд и возвращает в 'pending'
        задачи, чей воркер упал без возможности requeue (SIGKILL/OOM/краш
        процесса). Падение самой таски (DB-ошибка и т.п.) не убивает цикл.
        """
        logger.info(
            f"Stale cleaner started: resetting 'processing' "
            f"older than {_STALE_PROCESSING_INTERVAL} every {_CLEANER_INTERVAL:.0f}s"
        )
        while self._running:
            try:
                status = await self.db.pool.execute(
                    "UPDATE pending_events "
                    "SET status = 'pending', locked_at = NULL, worker_id = NULL "
                    "WHERE status = 'processing' "
                    "  AND locked_at < now() - $1::interval",
                    _STALE_PROCESSING_INTERVAL,
                )
                cleaned = int(status.split()[-1]) if status else 0
                if cleaned:
                    logger.warning(f"Stale cleaner: requeued {cleaned} stuck task(s)")
            except Exception as e:
                logger.error(f"Stale cleaner iteration failed: {e}")
            await asyncio.sleep(_CLEANER_INTERVAL)

    def _on_cleaner_done(self, task):
        """Перезапуск очистителя, если он умер неожиданно."""
        if not self._running:
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        logger.critical(f"Stale cleaner died unexpectedly ({exc!r}) — respawning")
        self._cleaner_task = asyncio.create_task(self._cleanup_stale_processing())
        self._cleaner_task.add_done_callback(self._on_cleaner_done)

    async def _process_row(self, row) -> Optional[dict]:
        """Полный цикл обработки одного сообщения: токенизация, поиск geo, вставка."""
        message_id = row['message_id']
        event_time = row['event_time']
        raw_text = row['text'] or ''

        now = datetime.now(timezone.utc)
        if not (now - timedelta(minutes=60) <= event_time <= now + timedelta(minutes=5)):
            logger.warning("msg %s: event_time %s outside 60-min window — mark expired", message_id, event_time)
            await self._mark_expired(row['id'])
            self._expired += 1
            processor_messages_expired_total.inc()
            return None

        tokens = tokenize(raw_text)
        lemmas = self.morph.lemmatize_tokens(tokens)
        layer = self.layer_classifier.classify(lemmas, raw_text=raw_text)

        promotional = is_promotional(raw_text)
        if promotional or not raw_text:
            if not raw_text:
                description = 'без описания'
            else:
                # Сохраняем оригинальный текст промо-сообщения,
                # обрезая до допустимой длины для отображения.
                description = truncate_for_geo(raw_text, settings.parser.max_text_length)
            result = await self._insert_event(
                message_id=message_id, event_time=event_time,
                description=description, photo_path=None,
                layer=layer, strategy='random',
                geom_wkt=self._random_point(),
            )
            self._log_quality_metrics(
                layer=layer, strategy='random', confidence=0.0,
                geo_ids=[], geo_scores=[], geo_texts=[],
                source='promotional',
            )
            return self._enrich(result, tokens=tokens, geo_ids=[])

        try:
            entities = await asyncio.wait_for(
                self.matcher.find_geo(tokens=tokens, lemmas=lemmas, text=raw_text),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[NLP] find_geo timeout for message {message_id}")
            entities = []
        except Exception as e:
            logger.warning(f"[NLP] find_geo error for message {message_id}: {e}")
            entities = []

        geo_ids = []
        geo_scores = []
        geo_texts = []
        for ent in entities:
            if ent['geo_id'] not in geo_ids:
                geo_ids.append(ent['geo_id'])
                geo_scores.append(ent['score'])
                geo_texts.append(ent['text'])

        # R-PR10: hard cap at Top-5 to protect PostGIS process_candidates from CROSS JOIN blowup
        geo_ids = geo_ids[:5]
        geo_scores = geo_scores[:5]
        geo_texts = geo_texts[:5]

        if not geo_ids:
            result = await self._insert_event(
                message_id=message_id, event_time=event_time,
                description=raw_text, photo_path=None,
                layer=layer, strategy='random',
                geom_wkt=self._random_point(),
            )
            self._log_quality_metrics(
                layer=layer, strategy='random', confidence=0.0,
                geo_ids=[], geo_scores=[], geo_texts=[],
                source='no_geo',
            )
            return self._enrich(result, tokens=tokens, geo_ids=geo_ids)

        result = self._enrich(
            await self._insert_event_from_candidates(
                message_id=message_id, event_time=event_time,
                description=raw_text, photo_path=None,
                layer=layer, geo_ids=geo_ids, geo_scores=geo_scores,
                geo_texts=geo_texts,
            ),
            tokens=tokens, geo_ids=geo_ids,
        )
        if result:
            self._log_quality_metrics(
                layer=layer,
                strategy=result.get('strategy', 'unknown'),
                confidence=result.get('confidence', 0.0),
                geo_ids=geo_ids, geo_scores=geo_scores, geo_texts=geo_texts,
                source='geo_match',
            )
        return result

    @staticmethod
    def _enrich(result, *, tokens, geo_ids):
        """Добавление служебных полей в результат обработки."""
        if result is not None:
            result['tokens'] = len(tokens)
            result['geo_matched'] = len(geo_ids)
            result['geo_ids'] = geo_ids
        return result

    @staticmethod
    def _log_quality_metrics(
        layer: str,
        strategy: str,
        confidence: float,
        geo_ids: list,
        geo_scores: list,
        geo_texts: list,
        source: str,
    ):
        """Логирование метрик качества для калибровки NLP-пайплайна.

        Записывает в structured log:
          - layer: итоговый слой (bus/cops/traffic/pig)
          - strategy: стратегия геолокации (single_match/intersection/...)
          - confidence: score финального кандидата
          - geo_candidates: количество кандидатов
          - geo_top_scores: топ-3 скоров для анализа распределения
          - source: источник решения (geo_match/no_geo/promotional)
        """
        top_scores = geo_scores[:3] if geo_scores else []
        logger.info(
            "[quality] layer=%s strategy=%s confidence=%.3f "
            "geo_candidates=%d top_scores=%s source=%s",
            layer, strategy, confidence,
            len(geo_ids), top_scores, source,
        )

    async def _insert_event(self, *, message_id, event_time, description,
                             photo_path, layer, strategy, geom_wkt):
        """Вставка простого события (без кандидатов) в таблицу events."""
        return await self._run_insert(
            _INSERT_EVENT_SIMPLE,
            (message_id, event_time, description, photo_path, layer, strategy, geom_wkt),
            message_id=message_id,
        )

    async def _insert_event_from_candidates(self, *, message_id, event_time,
                                              description, photo_path, layer,
                                              geo_ids, geo_scores, geo_texts):
        """Вставка события с вызовом process_candidates_v2 для разрешения кандидатов."""
        scores_array = [float(s) for s in geo_scores]
        geo_cfg = settings.geo if settings and hasattr(settings, 'geo') else None
        min_score = geo_cfg.candidate_min_score if geo_cfg else 0.80
        buffer_m = geo_cfg.intersection_buffer_m if geo_cfg else 100.0
        max_scatter_m = geo_cfg.weighted_centroid_max_scatter_m if geo_cfg else 1000.0
        result = await self._run_insert(
            _INSERT_EVENT_FROM_CANDIDATES,
            (message_id, event_time, description, photo_path, layer,
             geo_ids, scores_array, geo_texts, None,
             min_score, buffer_m, max_scatter_m),
            message_id=message_id,
            work_mem='32MB',
        )
        if result is None:
            result = await self._insert_event(
                message_id=message_id, event_time=event_time,
                description=description, photo_path=photo_path,
                layer=layer, strategy='random',
                geom_wkt=self._random_point(),
            )
        return result

    async def _run_insert(self, sql, params, *, message_id, work_mem: str = None):
        """Выполнение SQL-запроса вставки события и возврат результата."""
        async with self.db.pool.acquire() as c:
            if work_mem:
                async with c.transaction():
                    await c.execute(f"SET LOCAL work_mem = '{work_mem}'")
                    row = await c.fetchrow(sql, *params)
            else:
                row = await c.fetchrow(sql, *params)
        if row is None:
            return None
        return {
            'event_id': row['id'],
            'layer': row['layer'],
            'strategy': row['strategy'],
        }

    def _random_point(self):
        """Генерация случайной точки в радиусе от центра для fallback-событий."""
        if settings and hasattr(settings, 'question_overlay'):
            qo = settings.question_overlay
            center_lat, center_lng, radius = qo.center_lat, qo.center_lon, qo.radius
        else:
            center_lat, center_lng, radius = 46.49804, 30.83135, 0.045
        r = radius * math.sqrt(random.random())
        theta = random.random() * 2 * math.pi
        return f"POINT({center_lng + r * math.cos(theta)} {center_lat + r * math.sin(theta)})"

    def _request_stop(self):
        """Установка флага остановки процессора через asyncio.Event (thread-safe)."""
        logger.info("Stop signal received — requesting graceful shutdown")
        self._running = False
        # asyncio.Event.set() через call_soon_threadsafe безопасен из
        # signal handler: сигнал может прийти в любом потоке, а event loop
        # работает в основном. Прямой self._running = False оставлен для
        # обратной совместимости с while self._running в _worker.
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(self._shutdown_event.set)
            else:
                self._shutdown_event.set()
        except RuntimeError:
            pass  # loop уже закрыт — ничего не делаем

    def _apply_memory_fallback(self):
        """Graceful degradation: gc, shrink LRU."""
        gc.collect()
        if self.morph:
            try:
                self.morph.shrink_cache(max_size=5000)
                logger.info("Morphology LRU shrunk to 5000 (memory fallback)")
            except Exception as e:
                logger.warning(f"Failed to shrink Morphology cache: {e}")

    def get_rss_mb(self) -> float:
        """Возвращает RSS процесса в МБ."""
        try:
            with open('/proc/self/status', 'r') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        return int(line.split()[1]) / 1024
        except Exception:
            pass
        return -1.0

    async def _get_pending_queue_depth(self) -> int:
        """Возвращает глубину очереди pending_events."""
        try:
            row = await self.db.pool.fetchrow(
                "SELECT count(*) AS cnt FROM pending_events WHERE status = 'pending'"
            )
            return row['cnt'] if row else 0
        except Exception:
            return -1

    @staticmethod
    def _write_heartbeat(processor: 'ProcessorBot'):
        """Записать enriched heartbeat в /tmp/processor_heartbeat."""
        try:
            rss_mb = processor.get_rss_mb()
            lru_size = processor.morph.cache_size() if hasattr(processor.morph, 'cache_size') else -1
            with open('/tmp/processor_heartbeat', 'w') as f:  # nosec B108 — container /tmp, Docker healthcheck
                f.write(json_lib.dumps({
                    'timestamp': int(datetime.now(timezone.utc).timestamp()),
                    'rss_mb': round(rss_mb, 1),
                    'lru_size': lru_size,
                }))
        except Exception:
            pass

    async def shutdown(self):
        """Корректное завершение: остановка воркеров, закрытие соединений."""
        if self._shutdown_started:
            return
        self._shutdown_started = True

        logger.info("Shutting down processor...")
        self._running = False

        tasks = [t for t in self._worker_tasks if t and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._cleaner_task and not self._cleaner_task.done():
            self._cleaner_task.cancel()
            await asyncio.gather(self._cleaner_task, return_exceptions=True)
            self._cleaner_task = None

        if self._listen_conn:
            try:
                await self._listen_conn.remove_listener("geo_updated", self._on_geo_updated)
                await self._listen_conn.close()
            except Exception:
                pass

        if self.matcher:
            await self.matcher.close()
        if self.db:
            await self.db.close()

        logger.info(
            f"Processor stopped. Processed: {self._messages_processed}, "
            f"Errors: {self._errors}"
        )


async def main():
    """Точка входа: создание, инициализация и запуск ProcessorBot."""
    processor = ProcessorBot()
    try:
        success = await processor.initialize()
        if not success:
            logger.error("Failed to initialize, exiting")
            sys.exit(1)
        await processor.run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
    finally:
        await processor.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
