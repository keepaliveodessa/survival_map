"""Pytest bootstrap.

- Puts the repo root on sys.path so `core.*` imports work.
- Provides lightweight stubs for pure-python deps that may be absent in a bare
  dev/CI box (environs, pybreaker) so the modules under test import. When the
  real packages ARE installed (full env / CI with requirements), the stubs are
  NOT used — find_spec finds the real package first. Heavy C-ext deps
  (asyncpg/rapidfuzz/pymorphy3) are NOT stubbed here; tests needing them skip.
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

# common/settings.py теперь fail-fast на POSTGRES_PASSWORD (M-1). Форсируем
# валидный тестовый пароль ДО импорта любых модулей, чтобы коллекция тестов
# не зависела от содержимого локального .env (в CI переменная и так задана).
os.environ["POSTGRES_PASSWORD"] = "ci-test-postgres-password"

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _missing(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is None
    except (ImportError, ValueError):
        return True


# --- environs stub (common.settings builds Env at import) ---------------------
if _missing("environs"):
    _m = types.ModuleType("environs")

    class _Env:  # minimal surface used by common/settings.py
        def __init__(self, *a, **k):
            pass

        def read_env(self, *a, **k):
            return None

        def str(self, name, default=None):
            # POSTGRES_PASSWORD нужен при импорте common.settings (fail-fast):
            # отдаём валидный заглушечный пароль вместо default=None.
            if name == "POSTGRES_PASSWORD":
                return "ci-stub-postgres-password"
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


def load_module_by_path(name: str, relpath: str):
    """Import a single module file directly, bypassing its package __init__.

    Needed for self-contained submodules (core/text_preprocessor,
    nlp_processor/word_tokenizer), которые тестируются без тяжёлых зависимостей,
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
