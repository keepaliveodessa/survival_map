"""Shared Prometheus metrics for cross-service observability.

Definitions live in `common/` — the only package guaranteed to be present in
every service image (parser, nlp_processor, core). The `prometheus_client`
dependency is only guaranteed in the `core` image, so the import is guarded:
images that don't ship it (e.g. parser/nlp_processor builds without
prometheus_client) get no-op counters instead of an import error.

App-метрики parser/nlp_processor (экспортируются каждый сервисом на своём
/metrics-эндпоинте, см. parser/monitoring.py и nlp_processor/health.py):
  - parser_messages_processed_total / parser_messages_errors_total —
    поток сообщений Telegram → pending_events (базовый SLI конвейера);
  - parser_queue_size / parser_backpressure_active — состояние внутренней
    asyncio-очереди и режим backpressure (прямая запись в БД);
  - nlp_processor_messages_processed_total / nlp_processor_messages_errors_total /
    nlp_processor_messages_expired_total — результаты NLP-обработки;
  - nlp_processor_strategy_total{strategy} — распределение стратегий геометрии
    вставленных событий (доля random = доля негеолоцированных событий);
  - nlp_processor_geo_miss_total — сообщения с реальным текстом (>=3 токенов,
    не промо), но без ни одного geo-кандидата — основной сигнал пробелов
    geo-справочника (см. postgres/quality_report.sql и Grafana Data Quality).
  - nlp_processor_worker_active — активные воркеры (сравнение с concurrency);
  - nlp_processor_circuit_breaker_state — состояние CircuitBreaker
    (0=closed 1=half_open 2=open; ненулевое значение = деградация БД).
"""

try:
    from prometheus_client import Counter, Gauge
except Exception:  # pragma: no cover - optional dependency
    Counter = None
    Gauge = None


if Counter is not None:

    layer_classification_fallback_total = Counter(
        "layer_classification_fallback_total",
        "Total layer classifications by resulting layer (bus/cops/traffic/pig)",
        ["layer"],
    )

    geo_match_tier_total = Counter(
        "geo_match_tier_total",
        "Total geo-matching results by tier (tier1 stem / tier2 surface typo / none)",
        ["tier"],
    )

    # ------------------------------------------------------------------
    # Parser (parser/monitoring.py)
    # ------------------------------------------------------------------

    parser_messages_processed_total = Counter(
        "parser_messages_processed_total",
        "Total messages enqueued into pending_events",
    )

    parser_messages_errors_total = Counter(
        "parser_messages_errors_total",
        "Total message processing failures (write to pending_events)",
    )

    parser_messages_dedup_dropped_total = Counter(
        "parser_messages_dedup_dropped_total",
        "Duplicate channel messages dropped by text dedup (N4c, 30-min window)",
    )

    parser_queue_size = Gauge(
        "parser_queue_size",
        "Current internal asyncio queue size (0..65)",
    )

    parser_backpressure_active = Gauge(
        "parser_backpressure_active",
        "Backpressure mode active (queue >= 60/65, direct DB writes)",
    )

    # ------------------------------------------------------------------
    # Processor (nlp_processor/main.py)
    # ------------------------------------------------------------------

    nlp_processor_messages_processed_total = Counter(
        "nlp_processor_messages_processed_total",
        "Total pending_events tasks processed into events",
    )

    nlp_processor_messages_errors_total = Counter(
        "nlp_processor_messages_errors_total",
        "Total pending_events tasks failed permanently",
    )

    nlp_processor_messages_expired_total = Counter(
        "nlp_processor_messages_expired_total",
        "Total pending_events tasks expired (event_time outside window)",
    )

    nlp_processor_strategy_total = Counter(
        "nlp_processor_strategy_total",
        "Total inserted events by geometry strategy (random/single_match/...)",
        ["strategy"],
    )

    nlp_processor_geo_miss_total = Counter(
        "nlp_processor_geo_miss_total",
        "Messages with real text (>=3 tokens, non-promotional) but zero geo matches "
        "— primary signal for geo dictionary gaps",
    )

    nlp_processor_structured_total = Counter(
        "nlp_processor_structured_total",
        "Structured pins (📍 + Адрес:) by fast-path result "
        "(matched_address = улица из адреса; fallback_general = матч общим путем; "
        "no_match = не геолоцирован)",
        ["result"],
    )

    nlp_processor_worker_active = Gauge(
        "nlp_processor_worker_active",
        "Currently running NLP worker tasks",
    )

    nlp_processor_circuit_breaker_state = Gauge(
        "nlp_processor_circuit_breaker_state",
        "CircuitBreaker state: 0=closed 1=half_open 2=open",
    )

    def start_metrics_server(port: int = 9100) -> bool:
        """Поднять HTTP-сервер Prometheus (GET /metrics) на указанном порту.

        Возвращает True при успехе, False при ошибке (обычно порт занят).
        Сознательно НЕ роняет вызывающий сервис: метрики — вспомогательная
        подсистема, отсутствие /metrics не должно останавливать приём событий.
        Порт публикуется только в docker-сети (expose в compose, не ports).
        """
        try:
            from prometheus_client import start_http_server

            start_http_server(port)
            return True
        except OSError:
            return False

else:
    class _NoopCounter:
        def labels(self, *args, **kwargs):
            return self

        def inc(self, amount=1):
            return None

    class _NoopGauge:
        def set(self, value):
            return None

    layer_classification_fallback_total = _NoopCounter()
    geo_match_tier_total = _NoopCounter()

    parser_messages_processed_total = _NoopCounter()
    parser_messages_errors_total = _NoopCounter()
    parser_messages_dedup_dropped_total = _NoopCounter()
    parser_queue_size = _NoopGauge()
    parser_backpressure_active = _NoopGauge()

    nlp_processor_messages_processed_total = _NoopCounter()
    nlp_processor_messages_errors_total = _NoopCounter()
    nlp_processor_messages_expired_total = _NoopCounter()
    nlp_processor_strategy_total = _NoopCounter()
    nlp_processor_geo_miss_total = _NoopCounter()
    nlp_processor_structured_total = _NoopCounter()
    nlp_processor_worker_active = _NoopGauge()
    nlp_processor_circuit_breaker_state = _NoopGauge()

    def start_metrics_server(port: int = 9100) -> bool:
        """No-op заглушка (prometheus_client недоступен): сервер не поднимается."""
        return False
