"""Regression: gauge processor_circuit_breaker_state updates in the run loop.

Пойманный в рантайме баг: в ProcessorBot.run() гейдж обновлялся через
self._CIRCUIT_STATE_PROM (несуществующий атрибут инстанса) вместо модульной
константы — процессор падал сразу после старта health-сервера
(Fatal error: 'ProcessorBot' object has no attribute '_CIRCUIT_STATE_PROM').

Конструктор ProcessorBot не используется (Morphology/ProcessPoolExecutor
тяжёлые и могут зависать в песочнице): инстанс создаётся через object.__new__
с минимальным набором атрибутов для run(). Импорт processor.main требует
полных зависимостей — в пустом dev-venv пропускается; в CI (integration-tests)
ловит подобные ошибки атрибутов до выката.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import processor.main as pm
    _IMPORT_OK = True
    _IMPORT_ERR = None
except Exception as _e:  # pragma: no cover - тяжёлые зависимости отсутствуют
    _IMPORT_OK = False
    _IMPORT_ERR = repr(_e)

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK, reason=f"processor.main import unavailable: {_IMPORT_ERR}"
)


def _bare_bot() -> "pm.ProcessorBot":
    """ProcessorBot без __init__ — только то, что нужно run()."""
    bot = object.__new__(pm.ProcessorBot)
    bot._running = True
    bot._shutdown_event = asyncio.Event()
    bot._shutdown_started = False
    bot._worker_tasks = []
    bot._worker_concurrency = 0  # воркеры не спавним — их цикл не тестируем здесь
    bot._circuit_breaker = pm.CircuitBreaker(failure_threshold=5, timeout=60.0)
    bot.health_server = MagicMock()
    bot.health_server.touch = MagicMock()
    bot.health_server.check_memory = MagicMock(return_value=True)
    bot.health_server.start = AsyncMock()
    bot.db = None  # cleaner-таск: AttributeError внутри try → просто лог
    bot.morph = None
    return bot


def _run_with_fast_sleep(bot, iterations: int = 2):
    """Патчит asyncio.sleep так, чтобы циклы завершались мгновенно, НО
    продолжали отдавать управление event loop'у (await real_sleep(0)).

    Важно: инстант-корутина без реальной точки приостановки НЕ передаёт
    управление циклу — фоновый cleaner крутится впритык и голодает loop
    (таймеры wait_for не срабатывают, тест зависает).
    """
    real_sleep = asyncio.sleep
    sleeps = []

    async def _fast_sleep(_):
        sleeps.append(1)
        if len(sleeps) >= iterations:
            bot._running = False  # останавливает и cleaner, и воркеры
            bot._shutdown_event.set()
        await real_sleep(0)  # yield to event loop

    return sleeps, _fast_sleep


class TestCircuitBreakerGauge:
    def test_module_mapping_is_module_level_constant(self):
        """_CIRCUIT_STATE_PROM — модульная константа с корректными значениями."""
        assert pm._CIRCUIT_STATE_PROM[pm.CircuitState.CLOSED] == 0
        assert pm._CIRCUIT_STATE_PROM[pm.CircuitState.HALF_OPEN] == 1
        assert pm._CIRCUIT_STATE_PROM[pm.CircuitState.OPEN] == 2

    @pytest.mark.asyncio
    async def test_run_loop_sets_gauge_without_attribute_error(self):
        """run() в первом цикле обновляет гейдж и не падает на маппинге.

        Регрессия: раньше обращение шло к self._CIRCUIT_STATE_PROM —
        AttributeError убивал процессор сразу после старта.
        """
        from prometheus_client import REGISTRY

        bot = _bare_bot()
        sleeps, fast_sleep = _run_with_fast_sleep(bot)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", fast_sleep)
            await asyncio.wait_for(bot.run(), timeout=10)

        assert len(sleeps) >= 2
        value = REGISTRY.get_sample_value("processor_circuit_breaker_state")
        assert value == 0.0  # CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_run_loop_reflects_open_state(self):
        """OPEN-состояние breaker'а попадает в гейдж как 2."""
        from prometheus_client import REGISTRY

        bot = _bare_bot()
        bot._circuit_breaker.state = pm.CircuitState.OPEN

        sleeps, fast_sleep = _run_with_fast_sleep(bot)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", fast_sleep)
            await asyncio.wait_for(bot.run(), timeout=10)

        value = REGISTRY.get_sample_value("processor_circuit_breaker_state")
        assert value == 2.0
