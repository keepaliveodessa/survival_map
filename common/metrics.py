"""Shared Prometheus metrics for cross-service observability.

Definitions live in `common/` — the only package guaranteed to be present in
every service image (parser, nlp_processor, core). The `prometheus_client`
dependency is listed in each service's requirements.txt (parser, processor,
core); the guard remains as a defensive fallback to prevent import errors
if a service image omits it.
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

    # Parser-specific metrics (added for parser optimization P7)
    parser_messages_processed_total = Counter(
        "parser_messages_processed_total",
        "Total messages processed by parser (preprocessed + inserted)",
    )

    parser_errors_total = Counter(
        "parser_errors_total",
        "Total errors encountered by parser",
        ["component"],
    )

    parser_queue_depth = Gauge(
        "parser_queue_depth",
        "Current depth of the pending_events queue",
    )

    parser_workers_active = Gauge(
        "parser_workers_active",
        "Number of active queue workers",
    )

    parser_photos_downloaded_total = Counter(
        "parser_photos_downloaded_total",
        "Total photos successfully downloaded by parser",
    )

    parser_photos_failed_total = Counter(
        "parser_photos_failed_total",
        "Total photo downloads that failed",
    )

    parser_batch_size = Gauge(
        "parser_batch_size",
        "Current size of the batch insert buffer",
    )
else:
    class _NoopCounter:
        def labels(self, *args, **kwargs):
            return self

        def inc(self, amount=1):
            return None

    class _NoopGauge:
        def labels(self, *args, **kwargs):
            return self

        def set(self, value):
            return None

        def inc(self, amount=1):
            return None

        def dec(self, amount=1):
            return None

    layer_classification_fallback_total = _NoopCounter()
    geo_match_tier_total = _NoopCounter()
    parser_messages_processed_total = _NoopCounter()
    parser_errors_total = _NoopCounter()
    parser_queue_depth = _NoopGauge()
    parser_workers_active = _NoopGauge()
    parser_photos_downloaded_total = _NoopCounter()
    parser_photos_failed_total = _NoopCounter()
    parser_batch_size = _NoopGauge()
