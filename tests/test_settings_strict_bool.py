"""Tests for common/settings._parse_strict_bool (Secure by Default).

Сценарии:
  None / '' / 'true' / '1' / 'abc'  → True  (валидация включена)
  'false' / '0'                     → False (dev-bypass, единственный триггер)
При 'false'/'0' парсер пишет WARNING с текстом "SECURITY RISK".
"""
import logging
import os
from unittest.mock import MagicMock

import pytest

from conftest import load_module_by_path

settings_mod = load_module_by_path("_settings_under_test", "common/settings.py")
_parse_strict_bool = settings_mod._parse_strict_bool
load_settings = settings_mod.load_settings

VAR_NAME = "TELEGRAM_WEBVIEW_VALIDATION"


@pytest.fixture(autouse=True)
def clean_environ():
    """Очистка os.environ перед каждым тестом (изоляция от .env / CI env)."""
    saved = dict(os.environ)
    os.environ.clear()
    yield
    os.environ.clear()
    os.environ.update(saved)


def _mock_env(value):
    env = MagicMock()
    env.str.return_value = value
    return env


# (env-значение, ожидаемый результат, должен ли быть WARNING)
@pytest.mark.parametrize(
    "value,expected,expect_warning",
    [
        (None, True, False),      # переменная не задана
        ("", True, False),        # пустая строка
        ("true", True, False),
        ("TRUE", True, False),
        ("1", True, False),
        ("abc", True, False),     # любое некорректное значение — True
        ("fals", True, False),    # опечатка — Secure by Default
        ("false", False, True),   # единственный триггер dev-bypass
        ("FALSE", False, True),
        ("0", False, True),
    ],
)
def test_parse_strict_bool(value, expected, expect_warning, caplog):
    with caplog.at_level(logging.WARNING):
        result = _parse_strict_bool(_mock_env(value), VAR_NAME, True)

    assert result is expected

    if expect_warning:
        assert "SECURITY RISK" in caplog.text
        assert VAR_NAME in caplog.text
    else:
        assert "SECURITY RISK" not in caplog.text


def test_parse_strict_bool_custom_default():
    """При отсутствии переменной возвращается переданный default."""
    assert _parse_strict_bool(_mock_env(None), VAR_NAME, True) is True
    assert _parse_strict_bool(_mock_env(None), VAR_NAME, False) is False


def test_parse_strict_bool_env_path(caplog):
    """End-to-end через load_settings: env-файл + реальный environs."""
    import tempfile
    from pathlib import Path

    environs = pytest.importorskip("environs")

    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / "test.env"
        # POSTGRES_PASSWORD обязателен (M-1 fail-fast), но не является предметом
        # этого теста — кладём валидный заглушечный пароль в env-файл.
        env_file.write_text(
            f"{VAR_NAME}=false\nBOT_TOKEN=x\nPOSTGRES_PASSWORD=strict-bool-test-pw\n"
        )

        with caplog.at_level(logging.WARNING):
            s = load_settings(env_path=str(env_file), require_jwt=False)

        assert s.app.telegram_webview_validation is False
        assert "SECURITY RISK" in caplog.text
        assert VAR_NAME in caplog.text


def test_load_settings_unset_env_file(caplog):
    """Без переменной в env-файле — True (Secure by Default)."""
    import tempfile
    from pathlib import Path

    pytest.importorskip("environs")

    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / "test.env"
        env_file.write_text("BOT_TOKEN=x\nPOSTGRES_PASSWORD=strict-bool-test-pw\n")

        with caplog.at_level(logging.WARNING):
            s = load_settings(env_path=str(env_file), require_jwt=False)

        assert s.app.telegram_webview_validation is True
        assert "SECURITY RISK" not in caplog.text