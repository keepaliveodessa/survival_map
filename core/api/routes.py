"""API routes setup"""
from aiohttp import web

from core.api.events import (
    get_events_handler,
    get_events_status_handler,
    post_events_updates_handler,
    get_events_snapshot_handler,
    get_geo_handler,
    get_data_status_handler
)

from core.api.health import (
    health_live_handler,
    health_ready_handler,
    health_detailed_handler
)

from core.api.config import (
    get_config_handler
)

from core.api.websocket import websocket_handler

from core.api.auth import (
    validate_init_handler,
    get_validation_config_handler,
    refresh_token_handler
)

from core.api.media import setup_media_routes
from core.api.metrics import setup_metrics_routes



def setup_routes(app: web.Application):
    """Setup all API and static routes"""

    # Health check endpoints (no auth required)
    app.router.add_get('/health', health_live_handler)
    app.router.add_get('/health/live', health_live_handler)
    app.router.add_get('/health/ready', health_ready_handler)
    app.router.add_get('/health/detailed', health_detailed_handler)

    # Authentication API
    app.router.add_get('/api/validation-config', get_validation_config_handler)
    app.router.add_post('/api/validation-config', get_validation_config_handler)  # legacy compat
    app.router.add_post('/api/validate-init', validate_init_handler)
    app.router.add_post('/api/auth/refresh', refresh_token_handler)

    # Events API
    app.router.add_post('/api/events', get_events_handler)  # POST для инкрементальных обновлений
    app.router.add_get('/api/events', get_events_snapshot_handler)
    app.router.add_get('/api/events/status', get_events_status_handler)
    app.router.add_post('/api/events/updates', post_events_updates_handler)
    app.router.add_get('/api/geo', get_geo_handler)
    app.router.add_get('/api/data_status', get_data_status_handler)

    # Configuration API (POST для безопасности — не логируются параметры)
    app.router.add_post('/api/config', get_config_handler)

    # WebSocket route
    app.router.add_get('/ws', websocket_handler)

    # Media routes (photos)
    setup_media_routes(app)

    # Prometheus metrics export (M-5)
    setup_metrics_routes(app)

    # Static routes удалены — обслуживаются через nginx