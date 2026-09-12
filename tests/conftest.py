"""Pytest bootstrap.

- Puts the repo root on sys.path so `core.*` imports work.
- Provides lightweight stubs for pure-python deps that may be absent in a bare
  dev/CI box (environs, pybreaker) so the modules under test import. When the
  real packages ARE installed (full env / CI with requirements), the stubs are
  NOT used — find_spec finds the real package first. Heavy C-ext deps
  (asyncpg/rapidfuzz/pymorphy3) are NOT stubbed here; tests needing them skip.
"""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _missing(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is None
    except (ImportError, ValueError):
        return True


def _importable(name: str) -> bool:
    """Check if a module can actually be imported (not just found)."""
    try:
        __import__(name)
        return True
    except Exception:
        return False


# --- environs stub (common.settings builds Env at import) ---------------------
if _missing("environs"):
    _m = types.ModuleType("environs")

    class _Env:  # minimal surface used by common/settings.py
        def __init__(self, *a, **k):
            pass

        def read_env(self, *a, **k):
            return None

        def str(self, name, default=None):
            return default

        def bool(self, name, default=None):
            return default

        def int(self, name, default=None):
            return default

        def float(self, name, default=None):
            return default

    _m.Env = _Env
    sys.modules["environs"] = _m


# --- pybreaker stub (telegram_validation decorates with a CircuitBreaker) -----
if _missing("pybreaker"):
    _m = types.ModuleType("pybreaker")

    class _CircuitBreaker:
        def __init__(self, *a, **k):
            pass

        def __call__(self, func):  # used as a decorator -> pass-through
            return func

    class _CircuitBreakerError(Exception):
        pass

    _m.CircuitBreaker = _CircuitBreaker
    _m.CircuitBreakerError = _CircuitBreakerError
    sys.modules["pybreaker"] = _m


# --- pyrogram stub (monitoring.py imports pyrogram at module level) -----
if not _importable("pyrogram"):
    _m = types.ModuleType("pyrogram")
    _filters = types.ModuleType("pyrogram.filters")
    _types = types.ModuleType("pyrogram.types")

    class _Client:
        def __init__(self, *a, **k):
            pass
        async def start(self):
            pass
        async def stop(self):
            pass
        @property
        def is_connected(self):
            return True
        def on_message(self, *a, **k):
            def decorator(func):
                return func
            return decorator
        async def get_chat_history(self, *a, **k):
            return []
        async def get_dialogs(self):
            return []
        async def download_media(self, *a, **k):
            return None

    class _Message:
        pass

    _filters.chat = lambda *a, **k: lambda f: f
    _filters.text = None
    _filters.caption = None
    _filters.photo = None
    _types.Message = _Message

    _m.Client = _Client
    _m.filters = _filters
    _m.types = _types
    errors_mod = types.ModuleType("pyrogram.errors")
    errors_mod.AuthKeyInvalid = Exception
    errors_mod.SessionExpired = Exception
    errors_mod.RPCError = Exception
    _m.errors = errors_mod
    sys.modules["pyrogram"] = _m
    sys.modules["pyrogram.filters"] = _filters
    sys.modules["pyrogram.types"] = _types
    sys.modules["pyrogram.errors"] = errors_mod


def load_module_by_path(name: str, relpath: str):
    """Import a single module file directly, bypassing its package __init__.

    Needed for self-contained submodules (core/text_preprocessor,
    processor/word_tokenizer), которые тестируются без тяжёлых зависимостей,
    подтягиваемых их пакетами (asyncpg/rapidfuzz/pymorphy3).
    """
    path = ROOT / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import pytest


@pytest.fixture(autouse=True)
def _ensure_jwt_config():
    """Гарантирует settings.jwt != None для тестов, использующих auth."""
    from common.settings import settings
    if settings.jwt is None:
        from common.settings import JWTConfig
        settings.jwt = JWTConfig(
            secret="ci-test-jwt-secret-minimum-32-characters-long"
        )
    yield
