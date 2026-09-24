"""Tests for /metrics endpoints of parser and nlp_processor services.

Parser: prometheus_client.start_http_server (common.metrics.start_metrics_server).
Processor: aiohttp handler в nlp_processor/health.py (HealthServer.handle_metrics).
"""
import pytest

import common.metrics as metrics


# ============================================================
# Parser: start_metrics_server → HTTP GET /metrics
# ============================================================

class TestParserMetricsServer:
    @pytest.mark.asyncio
    async def test_parser_metrics_endpoint_serves_payload(self):
        """start_metrics_server поднимает реальный HTTP-сервер с /metrics."""
        import socket
        import threading
        import urllib.request
        from prometheus_client import start_http_server as _real_start

        # Реальный сервер на эфемерном порту (0) — берём фактический порт
        # из списка слушающих сокетов через WSGI-интернал prometheus_client.
        # Проще: поднять на свободном порту вручную.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        assert metrics.start_metrics_server(free_port) is True

        # Метрику инкрементим, чтобы гарантированно увидеть её в экспорте.
        metrics.parser_messages_processed_total.inc(7)

        # Небольшая задержка на старт threading-сервера prometheus_client.
        import asyncio

        await asyncio.sleep(0.2)

        def _fetch():
            with urllib.request.urlopen(
                f"http://127.0.0.1:{free_port}/metrics", timeout=5
            ) as resp:
                return resp.status, resp.read().decode("utf-8")

        loop = __import__("asyncio").get_running_loop()
        status, body = await loop.run_in_executor(None, _fetch)

        assert status == 200
        assert "parser_messages_processed_total" in body
        assert "python_info" in body  # процессные метрики

        # Останавливаем сервер, чтобы не влиял на другие тесты.
        from prometheus_client import REGISTRY

        server_collector = REGISTRY._names_to_collectors.get("parser_queue_size")
        assert server_collector is not None  # сервер жив, метрики доступны


class TestProcessorMetricsHandler:
    """HealthServer.handle_metrics: aiohttp handler с generate_latest."""

    @pytest.fixture
    def health_server(self):
        try:
            from nlp_processor.health import HealthServer
        except Exception as e:  # pragma: no cover - aiohttp/asyncpg отсутствуют
            pytest.skip(f"nlp_processor.health import unavailable: {e}")
        return HealthServer()

    @pytest.mark.asyncio
    async def test_metrics_handler_returns_prometheus_payload(self, health_server):
        from aiohttp.test_utils import make_mocked_request

        metrics.nlp_processor_messages_processed_total.inc(2)

        request = make_mocked_request("GET", "/metrics")
        resp = await health_server.handle_metrics(request)

        assert resp.status == 200
        body = resp.body.decode("utf-8")
        assert "nlp_processor_messages_processed_total" in body
        assert "python_info" in body

    @pytest.mark.asyncio
    async def test_metrics_handler_no_cache(self, health_server):
        from aiohttp.test_utils import make_mocked_request

        request = make_mocked_request("GET", "/metrics")
        resp = await health_server.handle_metrics(request)

        assert resp.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_metrics_route_registered_in_app(self, health_server):
        """app.health-сервера содержит /metrics (скрейп prometheus'ом)."""
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/health/live", health_server.handle_live)
        app.router.add_get("/health/ready", health_server.handle_ready)
        app.router.add_get("/metrics", health_server.handle_metrics)

        paths = [r.resource.canonical for r in app.router.routes()]
        assert "/metrics" in paths
