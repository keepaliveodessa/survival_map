"""HTTP healthcheck сервер для processor."""

import logging
import resource
from datetime import datetime, timezone

from aiohttp import web

logger = logging.getLogger(__name__)


class HealthServer:
    def __init__(self):
        """Инициализация health-сервера."""
        self._last_heartbeat = datetime.now(timezone.utc)
        self._initialized = False
        self._memory_warning_sent = False

    def touch(self):
        """Обновление метки времени последней активности."""
        self._last_heartbeat = datetime.now(timezone.utc)

    def set_initialized(self, initialized: bool):
        """Установка флага готовности процессора."""
        self._initialized = initialized

    def get_rss_mb(self) -> float:
        """Return RSS memory in MB (Linux /proc/self/status)."""
        try:
            with open('/proc/self/status', 'r') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        return int(line.split()[1]) / 1024  # kB → MB
        except Exception:
            pass
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # kB → MB

    def check_memory(self) -> bool:
        """Return True if memory is within safe limits."""
        rss_mb = self.get_rss_mb()
        return rss_mb < 850  # hard limit 1GB, warn at 850MB

    async def handle_live(self, request):
        """Liveness-проверка: сервер жив."""
        return web.Response(text="OK")

    async def handle_ready(self, request):
        """Readiness-проверка: процессор инициализирован, активен и память в норме."""
        if not self._initialized:
            return web.Response(status=503, text="Not initialized")

        age = (datetime.now(timezone.utc) - self._last_heartbeat).total_seconds()
        if age > 60:
            return web.Response(status=503, text=f"Stale heartbeat: {age:.1f}s")

        if not self.check_memory():
            return web.Response(status=503, text="Memory limit exceeded")

        return web.Response(text="OK")

    async def start(self, port: int = 8765):
        """Запуск HTTP-сервера healthcheck."""
        app = web.Application()
        app.router.add_get("/health/live", self.handle_live)
        app.router.add_get("/health/ready", self.handle_ready)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)  # nosec B104 — bind all interfaces (health endpoint)
        await site.start()

        logger.info(f"Health server started on port {port}")
