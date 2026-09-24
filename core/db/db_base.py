"""
Base database connection handler with connection pooling.
Separates low-level database operations from business logic.
"""

import asyncio
import asyncpg
import logging
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional

try:
    from common.settings import settings
except Exception:
    settings = None

logger = logging.getLogger(__name__)


# Исключения при которых стоит делать retry
RETRYABLE_EXCEPTIONS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
    asyncpg.ConnectionRejectionError,
    OSError,  # Network issues
    ConnectionError,
)

# Исключения при которых НЕ стоит делать retry
NON_RETRYABLE_EXCEPTIONS = (
    asyncpg.SyntaxOrAccessError,
    asyncpg.InvalidColumnReferenceError,
    asyncpg.ForeignKeyViolationError,
    asyncpg.UniqueViolationError,
    RuntimeError,  # Application errors
)


def retry_db_condition(exception):
    """Определяет стоит ли делать retry для исключения."""
    if isinstance(exception, NON_RETRYABLE_EXCEPTIONS):
        return False
    return isinstance(exception, RETRYABLE_EXCEPTIONS)


async def create_pool(
    *,
    min_size: Optional[int] = None,
    max_size: Optional[int] = None,
    command_timeout: Optional[int] = None,
    statement_cache_size: int = 100,
    server_settings: Optional[Dict] = None,
    **kwargs: Any,
) -> asyncpg.Pool:
    """Единая фабрика asyncpg-пула для Database (core) и DBAdapter (parser/nlp_processor).

    Раньше два класса дублировали создание пула (kwargs в db_base vs DSN-строка
    в db_adapter). Теперь создание и параметры по умолчанию — в одном месте:
      - min_size/max_size/command_timeout по умолчанию берутся из settings.db;
      - timezone='Europe/Kiev' проставляется всегда (время событий привязано
        к Киеву, консистентно на стороне сессии БД);
      - statement_cache_size=100 — единое значение для всех сервисов.

    Дополнительные аргументы (host/port/database/user/password и пр.)
    передаются в asyncpg.create_pool как есть.
    """
    db_cfg = settings.db if settings and settings.db else None
    pool_min = min_size if min_size is not None else (db_cfg.pool_min_size if db_cfg else 5)
    pool_max = max_size if max_size is not None else (db_cfg.pool_max_size if db_cfg else 20)
    cmd_timeout = (
        command_timeout if command_timeout is not None
        else (db_cfg.command_timeout if db_cfg else 60)
    )

    ss = dict(server_settings or {})
    ss.setdefault('timezone', 'Europe/Kiev')

    return await asyncpg.create_pool(
        min_size=pool_min,
        max_size=pool_max,
        command_timeout=cmd_timeout,
        statement_cache_size=statement_cache_size,
        server_settings=ss,
        **kwargs,
    )


class Database:
    """Low-level database connection handler with connection pooling."""

    def __init__(self):
        """Инициализирует объект Database без подключения к БД."""
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self, max_retries: int = 10, retry_delay: float = 2.0, **kwargs) -> bool:
        """Create connection pool with manual retry logic."""
        for attempt in range(1, max_retries + 1):
            try:
                self.pool = await create_pool(**kwargs)
                logger.info(f"Database connection pool created on attempt {attempt}/{max_retries}")
                return True
            except RETRYABLE_EXCEPTIONS as e:
                logger.warning(f"Connection attempt {attempt}/{max_retries} failed: {e}")
                if attempt < max_retries:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error(f"Failed to connect to PostgreSQL after {max_retries} attempts")
                    raise
            except Exception as e:
                logger.error(f"Non-retryable error on attempt {attempt}: {e}")
                raise
        return False

    async def close(self) -> None:
        """Close connection pool.

        Сначала graceful close с коротким дедлайном. Если он не успевает
        (например, удерживается долгоживущее LISTEN/NOTIFY-соединение —
        тогда asyncpg ждёт его возврата в пул бесконечно), рвём все
        соединения немедленно через terminate(), чтобы не висеть при
        остановке контейнера.
        """
        if self.pool:
            try:
                await asyncio.wait_for(self.pool.close(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                self.pool.terminate()
            self.pool = None
            logger.info("Database connection pool closed")

    async def execute(self, query: str, *args, timeout: Optional[float] = None) -> str:
        """Execute SQL query and return status."""
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        
        async with self.pool.acquire() as conn:
            if timeout:
                return await asyncio.wait_for(conn.execute(query, *args), timeout=timeout)
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args, timeout: Optional[float] = None) -> List[Dict]:
        """Fetch multiple rows as dictionaries."""
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        
        async with self.pool.acquire() as conn:
            if timeout:
                records = await asyncio.wait_for(conn.fetch(query, *args), timeout=timeout)
            else:
                records = await conn.fetch(query, *args)
            return [dict(record) for record in records]

    async def fetchrow(self, query: str, *args, timeout: Optional[float] = None) -> Optional[Dict]:
        """Fetch single row as dictionary."""
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        
        async with self.pool.acquire() as conn:
            if timeout:
                record = await asyncio.wait_for(conn.fetchrow(query, *args), timeout=timeout)
            else:
                record = await conn.fetchrow(query, *args)
            return dict(record) if record else None

    async def fetchval(self, query: str, *args, timeout: Optional[float] = None) -> Any:
        """Fetch single value."""
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        
        async with self.pool.acquire() as conn:
            if timeout:
                return await asyncio.wait_for(conn.fetchval(query, *args), timeout=timeout)
            return await conn.fetchval(query, *args)

    async def executemany(self, query: str, args_list: List[tuple]) -> None:
        """Execute query with multiple parameter sets."""
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        
        async with self.pool.acquire() as conn:
            await conn.executemany(query, args_list)

    @asynccontextmanager
    async def transaction(self):
        """Async context manager yielding a connection INSIDE a real transaction.

        Usage: `async with db.transaction() as conn: await conn.execute(...)`.
        Commits on success, rolls back on exception. (Раньше возвращал просто
        pool.acquire() — это autocommit без отката, что вводило в заблуждение.)
        """
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    @property
    def is_connected(self) -> bool:
        """Check if database is connected."""
        return self.pool is not None

