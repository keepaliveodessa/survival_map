"""Prometheus /metrics endpoint for the core service.

M-5: prometheus_client объявлен в requirements, счётчики регистрируются при
импорте core.metrics и инжектируются в common.logging_config, но эндпоинт не
был зарегистрирован — метрики «писались в никуда», а GET /metrics снаружи
отдавал SPA-fallback nginx (index.html).

Security (важно):
  - Эндпоинт ДОЛЖЕН быть исключён из SPA-fallback nginx (`location = /metrics
    { return 404; }`) — иначе карточка с инфраструктурной телеметрией уедет в
    интернет через try_files $uri /index.html.
  - В режиме валидации (TELEGRAM_WEBVIEW_VALIDATION=true) закрыт JWT-middleware
    (не входит в PUBLIC_ENDPOINTS). В dev-bypass доступен без авторизации —
    приемлемо: порт 8080 не публикуется наружу (только expose в docker-сети).
  - При скрейпе prometheus'ом из backend-сети обращайтесь к core:8080
    напрямую, не через nginx.
  - Только GET (405 на остальные методы — стандартное поведение aiohttp).

Trade-off осознанный: /metrics сам попадает в http_requests_total (один GET
на скрейп — не шумный). Эндпоинт публично известный и не раскрывает секретов —
обычная практика для /metrics.
"""

import logging

from aiohttp import web
from prometheus_client import REGISTRY, generate_latest

logger = logging.getLogger(__name__)

METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


async def get_metrics_handler(request: web.Request) -> web.Response:
    """Экспортировать все зарегистрированные Prometheus-метрики.

    Использует глобальный REGISTRY prometheus_client: в него автоматически
    попадают метрики из core/metrics.py (ws_*, http_*), процессные метрики
    (python_info, process_*), а также любые Counter/Histogram, объявленные
    позже в этом же процессе.
    """
    try:
        payload = generate_latest(REGISTRY)
    except Exception as e:  # pragma: no cover - защитная сетка
        logger.error(f"Failed to generate metrics: {e}", exc_info=True)
        return web.json_response({'error': 'metrics generation failed'}, status=500)

    return web.Response(
        body=payload,
        headers={
            'Content-Type': METRICS_CONTENT_TYPE,
            # Метрики меняются на каждый скрейп — кэшировать бессмысленно.
            'Cache-Control': 'no-store',
        },
    )


def setup_metrics_routes(app: web.Application) -> None:
    """Зарегистрировать /metrics (до/после setup_routes — порядок не важен)."""
    app.router.add_get('/metrics', get_metrics_handler)
