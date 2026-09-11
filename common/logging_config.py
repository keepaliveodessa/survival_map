"""
Structured JSON logging configuration (Docker-optimized version)

Модуль импортируется и парсером, и app-сервисом. Парсер не имеет aiohttp в
зависимостях — импорт `aiohttp.web` делаем опциональным, middleware
определяется только если aiohttp доступен.
"""
import logging
import json
import sys
import time
import uuid
import os
import asyncio
from datetime import datetime, timezone
from contextvars import ContextVar

try:
    from aiohttp import web
    _HAS_AIOHTTP = True
except ImportError:
    web = None  # type: ignore[assignment]
    _HAS_AIOHTTP = False


# --- HTTP metrics hook (dependency inversion) --------------------------------
# Архитектурный инвариант common-invariant: common/ не импортирует core/*.
# Раньше middleware делал `from core.metrics import ...` внутри запроса —
# это ломало инвариант и тащило core-модули в parser/processor, которые
# этот модуль импортируют. Теперь core сам регистрирует свои prometheus-
# метрики при старте через register_http_metrics(), а middleware берёт их
# из реестра. Не зарегистрировано — метрики просто пропускаются.
_http_metrics = None


def register_http_metrics(*, requests_total, request_duration_seconds) -> None:
    """Регистрация HTTP-метрик хост-приложения (вызывается из core)."""
    global _http_metrics
    _http_metrics = {
        'requests_total': requests_total,
        'request_duration_seconds': request_duration_seconds,
    }


_request_id_var: ContextVar[str] = ContextVar('request_id', default='-')


class JSONFormatter(logging.Formatter):
    """
    Format log records as JSON

    Benefits:
    - Structured logs for easier parsing
    - Compatible with log aggregation systems (ELK, Loki, etc.)
    - Machine-readable format
    """

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
            'thread': record.thread,
            'thread_name': record.threadName
        }

        # Add request ID if available (from middleware)
        if hasattr(record, 'request_id'):
            log_obj['request_id'] = record.request_id

        # Add user ID if available
        if hasattr(record, 'user_id'):
            log_obj['user_id'] = record.user_id

        # Add any extra fields
        if hasattr(record, 'extra_data'):
            log_obj.update(record.extra_data)

        # Add exception information if present
        if record.exc_info:
            log_obj['exception'] = {
                'type': record.exc_info[0].__name__ if record.exc_info[0] else None,
                'message': str(record.exc_info[1]) if record.exc_info[1] else None,
                'traceback': self.formatException(record.exc_info)
            }

        # Add stack info if present
        if record.stack_info:
            log_obj['stack_info'] = record.stack_info

        return json.dumps(log_obj, ensure_ascii=False, default=str)


class ContextLogger(logging.LoggerAdapter):
    """
    Logger adapter that adds context to all log messages

    Usage:
        logger = ContextLogger(logging.getLogger(__name__), {'request_id': '123'})
        logger.info('User logged in', extra={'user_id': 456})
    """

    def process(self, msg, kwargs):
        # Merge context into extra
        extra = kwargs.get('extra', {})
        extra.update(self.extra)
        kwargs['extra'] = extra
        return msg, kwargs


def setup_logging(
    level: int = logging.INFO,
    json_format: bool = True,
    suppress_noisy_loggers: bool = True
):
    """
    Configure application logging (Docker-optimized version)
    Uses only console output to avoid file permission issues in containers

    Args:
        level: Logging level (logging.INFO, logging.DEBUG, etc.)
        json_format: Use JSON formatter (True) or standard text format (False)
        suppress_noisy_loggers: Suppress output from noisy third-party libraries
    """
    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)

    # Set formatter
    if json_format:
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

    console_handler.setFormatter(formatter)

    # Configure root logger with console handler only (no file handlers to avoid permission issues)
    logging.root.handlers = [console_handler]
    logging.root.setLevel(level)

    # Suppress noisy loggers
    if suppress_noisy_loggers:
        logging.getLogger("pyrogram").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
        logging.getLogger("aiogram").setLevel(logging.INFO)

    logging.info("Logging configured (console-only)", extra={
        'json_format': json_format,
        'level': logging.getLevelName(level),
        'output': 'console'
    })


# ============================================
# Request ID Middleware (aiohttp-only — определяется лишь если aiohttp доступен)
# ============================================

if not _HAS_AIOHTTP:
    # Stub для окружений без aiohttp (например, parser): попытка реально
    # подключить middleware к aiohttp-приложению вернёт явную ошибку.
    async def logging_middleware(*args, **kwargs):  # type: ignore[misc]
        raise RuntimeError(
            "logging_middleware requires aiohttp, but aiohttp is not installed "
            "in this environment"
        )

else:
    # Устанавливаем LogRecordFactory ОДИН РАЗ при загрузке модуля.
    # ContextVar _request_id_var обеспечивает корректный request_id при
    # конкурентных async-запросах без глобальных перезаписей фабрики.
    # Предыдущий подход (set/restore factory на каждый запрос) создавал
    # race condition: конкурентные запросы перезаписывали чужие фабрики,
    # теряя request_id или восстанавливая устаревшую версию фабрики.
    _default_log_factory = logging.getLogRecordFactory()

    def _request_id_record_factory(*args, **kwargs):
        record = _default_log_factory(*args, **kwargs)
        record.request_id = _request_id_var.get('-')
        return record

    logging.setLogRecordFactory(_request_id_record_factory)

    @web.middleware
    async def logging_middleware(request: web.Request, handler):
        """
        Middleware to add request ID to all logs.

        Использует ContextVar для thread-safe хранения request_id —
        каждый конкурентный запрос видит свой request_id без глобальных
        блокировок и перезаписи LogRecordFactory.
        """
        # Generate unique request ID
        request_id = str(uuid.uuid4())
        request['request_id'] = request_id

        # ContextVar: устанавливаем request_id для текущего async-контекста.
        # При конкурентных запросах каждый имеет свой токен — сброс в finally
        # корректно восстанавливает значение родительского контекста.
        token = _request_id_var.set(request_id)

        # Log request
        logger = logging.getLogger('aiohttp.access')
        logger.info(
            f"{request.method} {request.path}",
            extra={
                'method': request.method,
                'path': request.path,
                'remote': request.remote,
                'user_agent': request.headers.get('User-Agent')
            }
        )

        # Time the request
        start_time = time.time()

        try:
            response = await handler(request)
            status = response.status

            # Add request ID to response headers
            response.headers['X-Request-ID'] = request_id

            return response

        except web.HTTPException as e:
            status = e.status
            raise

        except Exception as e:
            status = 500
            logger.error(
                f"Unhandled exception in {request.method} {request.path}",
                exc_info=True,
                extra={}
            )
            raise

        finally:
            duration = time.time() - start_time

            # Log response
            logger.info(
                f"{request.method} {request.path} -> {status}",
                extra={
                    'method': request.method,
                    'path': request.path,
                    'status': status,
                    'duration_ms': round(duration * 1000, 2)
                }
            )

            # Prometheus metrics (инжекция из core — см. register_http_metrics)
            if _http_metrics is not None:
                try:
                    _http_metrics['requests_total'].labels(
                        method=request.method,
                        path=request.path,
                        status=status
                    ).inc()
                    _http_metrics['request_duration_seconds'].labels(
                        method=request.method,
                        path=request.path
                    ).observe(duration)
                except Exception:
                    pass

            # Restore ContextVar to parent context value
            _request_id_var.reset(token)


# ============================================
# Helper Functions
# ============================================

def get_logger_with_context(name: str, **context) -> ContextLogger:
    """
    Get a logger with context
    
    Example:
        logger = get_logger_with_context(__name__, user_id=123)
        logger.info('User action')  # Will include user_id in all logs
    """
    base_logger = logging.getLogger(name)
    return ContextLogger(base_logger, context)


def log_with_extra(
    logger: logging.Logger,
    level: int,
    message: str,
    **extra_fields
):
    """
    Log a message with extra fields
    
    Example:
        log_with_extra(
            logger, logging.INFO, 
            'User logged in',
            user_id=123, ip='1.2.3.4'
        )
    """
    logger.log(level, message, extra={'extra_data': extra_fields})


# ============================================
# Exception Logging Decorator
# ============================================

def log_exceptions(logger: logging.Logger):
    """
    Decorator to automatically log exceptions from functions
    
    Usage:
        @log_exceptions(logger)
        async def my_function():
            ...
    """
    def decorator(func):
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(
                    f"Exception in {func.__name__}: {e}",
                    exc_info=True,
                    extra={'extra_data': {
                        'function': func.__name__,
                        'args_count': len(args),
                        'kwargs_keys': list(kwargs.keys())
                    }}
                )
                raise
        
        def sync_wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(
                    f"Exception in {func.__name__}: {e}",
                    exc_info=True,
                    extra={'extra_data': {
                        'function': func.__name__
                    }}
                )
                raise
        
        # Return appropriate wrapper
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator
