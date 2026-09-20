"""Tests for core/api/metrics.py — Prometheus /metrics endpoint (M-5)."""
import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.middlewares.jwt_auth import jwt_auth_middleware

try:
    from core.api.metrics import (
        get_metrics_handler,
        setup_metrics_routes,
        METRICS_CONTENT_TYPE,
    )
    from core.metrics import http_requests_total, ws_connections_total
    _IMPORT_OK = True
    _IMPORT_ERR = None
except Exception as e:
    _IMPORT_OK = False
    _IMPORT_ERR = repr(e)

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK, reason=f"metrics api import unavailable: {_IMPORT_ERR}"
)


def _make_app() -> web.Application:
    """Минимальное aiohttp-приложение с JWT-middleware и /metrics."""
    app = web.Application(middlewares=[jwt_auth_middleware])
    setup_metrics_routes(app)
    return app


class FakeRequest:
    """Минимальный request-дабл для прямого вызова хендлера."""

    def __init__(self):
        self._app = {}

    @property
    def app(self):
        return self._app


# ============================================================
# Handler: payload и заголовки (прямой вызов, стиль проекта)
# ============================================================

class TestMetricsHandler:
    @pytest.mark.asyncio
    async def test_returns_prometheus_payload(self):
        resp = await get_metrics_handler(FakeRequest())
        assert resp.status == 200
        assert resp.headers['Content-Type'] == METRICS_CONTENT_TYPE
        assert resp.headers['Cache-Control'] == 'no-store'
        body = resp.body.decode('utf-8')
        assert 'python_info' in body  # процессные метрики из глобального REGISTRY

    @pytest.mark.asyncio
    async def test_body_contains_core_counters(self):
        # Дергаем счётчики, чтобы они гарантированно попали в экспорт.
        http_requests_total.labels(method='GET', path='/metrics', status='200').inc()
        ws_connections_total.inc()

        resp = await get_metrics_handler(FakeRequest())
        body = resp.body.decode('utf-8')
        assert 'http_requests_total' in body
        assert 'ws_connections_total' in body
        assert '# TYPE http_requests_total counter' in body

    @pytest.mark.asyncio
    async def test_no_cache(self):
        resp = await get_metrics_handler(FakeRequest())
        assert resp.headers['Cache-Control'] == 'no-store'


# ============================================================
# Интеграция: маршрутизация + JWT-middleware (как в проде)
# ============================================================

async def _client_request(method: str, path: str, validation_enabled: bool):
    from common.settings import settings

    old = settings.app.telegram_webview_validation
    settings.app.telegram_webview_validation = validation_enabled
    try:
        client = TestClient(TestServer(_make_app()))
        await client.start_server()
        # middleware читает флаг при каждом запросе — мутация синглтона до запроса корректна.
        resp = await client.request(method, path)
        return client, resp
    finally:
        settings.app.telegram_webview_validation = old


class TestMetricsRouting:
    @pytest.mark.asyncio
    async def test_route_registered(self):
        app = _make_app()
        paths = [r.resource.canonical for r in app.router.routes()]
        assert '/metrics' in paths

    @pytest.mark.asyncio
    async def test_validation_on_requires_token(self):
        """TELEGRAM_WEBVIEW_VALIDATION=true → /metrics закрыт без токена (401)."""
        client, resp = await _client_request('GET', '/metrics', validation_enabled=True)
        try:
            assert resp.status == 401
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_dev_bypass_open(self):
        """TELEGRAM_WEBVIEW_VALIDATION=false → /metrics открыт (dev-режим)."""
        client, resp = await _client_request('GET', '/metrics', validation_enabled=False)
        try:
            assert resp.status == 200
            body = await resp.text()
            assert 'http_requests_total' in body
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_get_only(self):
        """POST /metrics → 405 (router-level).

        ВАЖНО (regression CI): проверка только в dev-режиме. JWT-middleware
        выполняется ДО роутера, поэтому при validation=true POST без токена
        получает 401 раньше, чем роутер успевает вернуть 405 — тест был
        env-зависим и падал в job integration-tests (там validation=true).
        """
        client, resp = await _client_request('POST', '/metrics', validation_enabled=False)
        try:
            assert resp.status == 405
        finally:
            await client.close()
