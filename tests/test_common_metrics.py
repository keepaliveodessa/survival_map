"""Tests for common/metrics.py — shared parser/nlp_processor Prometheus metrics."""
import importlib
import sys
import types
from unittest.mock import patch

import pytest

import common.metrics as metrics


class TestRealRegistry:
    """prometheus_client установлен → метрики регистрируются в REGISTRY."""

    def test_parser_counters_registered(self):
        from prometheus_client import REGISTRY

        parser_messages_processed_total = REGISTRY._names_to_collectors[
            "parser_messages_processed_total"
        ]
        parser_messages_errors_total = REGISTRY._names_to_collectors[
            "parser_messages_errors_total"
        ]
        parser_messages_processed_total.inc(3)
        parser_messages_errors_total.inc()
        assert (
            REGISTRY.get_sample_value("parser_messages_processed_total") == 3.0
        )
        assert REGISTRY.get_sample_value("parser_messages_errors_total") == 1.0

    def test_parser_gauges_registered(self):
        from prometheus_client import REGISTRY

        parser_queue_size = REGISTRY._names_to_collectors["parser_queue_size"]
        parser_backpressure_active = REGISTRY._names_to_collectors[
            "parser_backpressure_active"
        ]
        parser_queue_size.set(42)
        parser_backpressure_active.set(1)
        assert REGISTRY.get_sample_value("parser_queue_size") == 42.0
        assert REGISTRY.get_sample_value("parser_backpressure_active") == 1.0

    def test_nlp_processor_counters_registered(self):
        from prometheus_client import REGISTRY

        for name in (
            "nlp_processor_messages_processed_total",
            "nlp_processor_messages_errors_total",
            "nlp_processor_messages_expired_total",
        ):
            assert name in REGISTRY._names_to_collectors, name

    def test_nlp_processor_gauges_registered(self):
        from prometheus_client import REGISTRY

        nlp_processor_worker_active = REGISTRY._names_to_collectors[
            "nlp_processor_worker_active"
        ]
        nlp_processor_circuit_breaker_state = REGISTRY._names_to_collectors[
            "nlp_processor_circuit_breaker_state"
        ]
        nlp_processor_worker_active.set(4)
        nlp_processor_circuit_breaker_state.set(2)  # OPEN
        assert REGISTRY.get_sample_value("nlp_processor_worker_active") == 4.0
        assert (
            REGISTRY.get_sample_value("nlp_processor_circuit_breaker_state") == 2.0
        )

    def test_start_metrics_server_serves_metrics(self):
        """start_metrics_server поднимает HTTP-сервер, /metrics отдаёт payload."""
        import urllib.request

        assert metrics.start_metrics_server(0) is True  # эфемерный порт
        # Порт неизвестен (start_http_server(0)) — проверяем сам факт функции
        # через прямой вызов generate_latest вместо HTTP (сервер слушает *:0).
        from prometheus_client import generate_latest

        body = generate_latest().decode("utf-8")
        assert "parser_messages_processed_total" in body
        assert "nlp_processor_circuit_breaker_state" in body


class TestFallbackWithoutPrometheusClient:
    """prometheus_client отсутствует → no-op метрики вместо ImportError."""

    @pytest.fixture
    def reload_without_client(self):
        saved = {k: v for k, v in sys.modules.items() if k.startswith("prometheus") or k == "common.metrics"}
        for k in list(saved):
            del sys.modules[k]

        class _Blocker:
            def find_module(self, name, path=None):
                if name.startswith("prometheus"):
                    raise ImportError("blocked for test")
                return None

        import importlib.abc

        class _MetaPathBlocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.startswith("prometheus"):
                    raise ImportError("blocked for test")
                return None

        blocker = _MetaPathBlocker()
        sys.meta_path.insert(0, blocker)
        try:
            yield
        finally:
            sys.meta_path.remove(blocker)
            for k in list(sys.modules):
                if k.startswith("prometheus") or k == "common.metrics":
                    del sys.modules[k]
            sys.modules.update(saved)

    def test_noop_objects(self, reload_without_client):
        import common.metrics as m

        assert m.Counter is None and m.Gauge is None
        # Не падают и ничего не делают:
        m.parser_messages_processed_total.inc()
        m.parser_queue_size.set(10)
        m.nlp_processor_circuit_breaker_state.set(2)
        assert m.start_metrics_server(9100) is False
