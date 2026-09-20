"""Tests for common/settings.py — _resolve_jwt_secret, load_settings, dataclasses."""
import os
from unittest.mock import patch, MagicMock

import pytest

from conftest import load_module_by_path

settings_mod = load_module_by_path("_settings_under_test", "common/settings.py")
_resolve_jwt_secret = settings_mod._resolve_jwt_secret
_resolve_postgres_password = settings_mod._resolve_postgres_password
load_settings = settings_mod.load_settings
Settings = settings_mod.Settings
DatabaseConfig = settings_mod.DatabaseConfig
BotConfig = settings_mod.BotConfig
AppConfig = settings_mod.AppConfig


# ============================================================
# _resolve_jwt_secret
# ============================================================

class TestResolveJwtSecret:
    def test_valid_secret(self):
        env = MagicMock()
        env.str.return_value = "a" * 32
        assert _resolve_jwt_secret(env) == "a" * 32

    def test_empty_secret_raises(self):
        env = MagicMock()
        env.str.return_value = ""
        with pytest.raises(RuntimeError, match="JWT_SECRET is required"):
            _resolve_jwt_secret(env)

    def test_none_secret_raises(self):
        env = MagicMock()
        env.str.return_value = None
        with pytest.raises(RuntimeError, match="JWT_SECRET is required"):
            _resolve_jwt_secret(env)

    def test_too_short_raises(self):
        env = MagicMock()
        env.str.return_value = "short"
        with pytest.raises(RuntimeError, match="must be >= 32 chars"):
            _resolve_jwt_secret(env)

    def test_insecure_default_your_secret_key_raises(self):
        env = MagicMock()
        env.str.return_value = "your-secret-key"
        with pytest.raises(RuntimeError, match="placeholder"):
            _resolve_jwt_secret(env)

    def test_insecure_default_secret_raises(self):
        env = MagicMock()
        env.str.return_value = "secret"
        with pytest.raises(RuntimeError, match="placeholder"):
            _resolve_jwt_secret(env)

    def test_insecure_default_changeme_raises(self):
        env = MagicMock()
        env.str.return_value = "changeme"
        with pytest.raises(RuntimeError, match="placeholder"):
            _resolve_jwt_secret(env)

    def test_insecure_starts_with_your_secret_raises(self):
        env = MagicMock()
        env.str.return_value = "your-secret-key-change-in-production-min-32-chars"
        with pytest.raises(RuntimeError, match="placeholder"):
            _resolve_jwt_secret(env)


# ============================================================
# DatabaseConfig defaults
# ============================================================

class TestDatabaseConfigDefaults:
    def test_defaults(self):
        db = DatabaseConfig()
        assert db.host == "postgres"
        assert db.port == 5432
        assert db.database == "postgres"
        assert db.user == "postgres"
        assert db.password == ""
        assert db.pool_min_size == 1
        assert db.pool_max_size == 10
        assert db.command_timeout == 30


# ============================================================
# _resolve_postgres_password — безусловный fail-fast (M-1)
# ============================================================

class TestResolvePostgresPassword:
    """Безусловная проверка: без ENVIRONMENT-развилки — сервис не стартует
    с отсутствующим/слабым паролем (по образцу _resolve_jwt_secret, R-C8)."""

    def test_valid_password(self):
        env = MagicMock()
        env.str.return_value = "s3cure-passw0rd"
        assert _resolve_postgres_password(env) == "s3cure-passw0rd"

    def test_missing_raises(self):
        env = MagicMock()
        env.str.return_value = None
        with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD is required"):
            _resolve_postgres_password(env)

    def test_empty_raises(self):
        env = MagicMock()
        env.str.return_value = ""
        with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD is required"):
            _resolve_postgres_password(env)

    @pytest.mark.parametrize(
        "password",
        ["postgres", "password", "123456", "admin", "root", "changeme", "change-me", "default"],
    )
    def test_insecure_defaults_raise(self, password):
        env = MagicMock()
        env.str.return_value = password
        with pytest.raises(RuntimeError, match="insecure default"):
            _resolve_postgres_password(env)

    def test_insecure_default_case_insensitive(self):
        env = MagicMock()
        env.str.return_value = "Postgres"
        with pytest.raises(RuntimeError, match="insecure default"):
            _resolve_postgres_password(env)

    def test_too_short_raises(self):
        env = MagicMock()
        env.str.return_value = "short"
        with pytest.raises(RuntimeError, match="too short"):
            _resolve_postgres_password(env)

    def test_exactly_8_chars_ok(self):
        env = MagicMock()
        env.str.return_value = "12345678"
        assert _resolve_postgres_password(env) == "12345678"

    def test_no_enviroment_escape_hatch(self):
        """ENVIRONMENT=development больше НЕ отключает проверки (regression M-1)."""
        env = MagicMock()
        env.str.return_value = "postgres"
        with patch.dict(os.environ, {"ENVIRONMENT": "development", "ENV": "development"}):
            with pytest.raises(RuntimeError, match="insecure default"):
                _resolve_postgres_password(env)


class TestLoadSettingsPassword:
    def test_load_settings_missing_password_raises(self):
        """Отсутствие POSTGRES_PASSWORD в env → ValueError (обёрнутый RuntimeError)."""
        mock_env = MagicMock()
        mock_env.str.side_effect = lambda key, default=None: {
            "BOT_TOKEN": "123456:ABC",
        }.get(key, default)

        with patch.object(settings_mod, "Env", return_value=mock_env):
            with pytest.raises(ValueError, match="Configuration error"):
                load_settings(require_jwt=False)

    def test_load_settings_password_from_env(self):
        """Пароль берётся из env, а не из dataclass-дефолта."""
        mock_env = MagicMock()
        mock_env.str.side_effect = lambda key, default=None: {
            "BOT_TOKEN": "123456:ABC",
            "POSTGRES_PASSWORD": "from-env-password",
        }.get(key, default)

        with patch.object(settings_mod, "Env", return_value=mock_env):
            s = load_settings(require_jwt=False)
        assert s.db.password == "from-env-password"


# ============================================================
# load_settings — mocked env
# ============================================================

class TestLoadSettings:
    def test_load_settings_with_mocked_env(self):
        mock_env = MagicMock()
        mock_env.str.side_effect = lambda key, default=None: {
            "BOT_TOKEN": "123456:ABC",
            "WEBAPP_URL": "https://example.com",
            "REDIRECT_URL": "https://t.me/bot",
            "CHANNEL_ID": "-1002050105527",
            "POSTGRES_USER": "postgres",
            "POSTGRES_PASSWORD": "mocked-env-password",
            "POSTGRES_DB": "postgres",
        }.get(key, default)
        mock_env.bool.return_value = True
        mock_env.int.return_value = 1080
        mock_env.str.return_value = "socks5"

        with patch.object(settings_mod, "Env", return_value=mock_env):
            s = load_settings(require_jwt=False)

        assert s.bot.token == "123456:ABC"
        assert s.bot.webapp_url == "https://example.com"
        assert s.bot.redirect_url == "https://t.me/bot"
        assert s.db.password == "mocked-env-password"

    def test_load_settings_postgres_password_default_removed(self):
        """M-1 regression: дефолт 'postgres' удалён — отсутствие пароля = ошибка."""
        mock_env = MagicMock()
        mock_env.str.side_effect = lambda key, default=None: {
            "BOT_TOKEN": "123456:ABC",
            "POSTGRES_USER": "postgres",
            "POSTGRES_DB": "postgres",
        }.get(key, default)
        mock_env.bool.return_value = True
        mock_env.int.return_value = 1080
        mock_env.str.return_value = "socks5"

        with patch.object(settings_mod, "Env", return_value=mock_env):
            with pytest.raises(ValueError, match="Configuration error"):
                load_settings(require_jwt=False)

    def test_load_settings_channel_id_fallback(self):
        """CHANNEL_ID has hardcoded fallback in load_settings."""
        mock_env = MagicMock()
        mock_env.str.side_effect = lambda key, default=None: {
            "BOT_TOKEN": "123456:ABC",
            "POSTGRES_PASSWORD": "mocked-env-password",
            "POSTGRES_USER": "postgres",
            "POSTGRES_DB": "postgres",
        }.get(key, default)
        mock_env.bool.return_value = True
        mock_env.int.return_value = 1080
        mock_env.str.return_value = "socks5"

        with patch.object(settings_mod, "Env", return_value=mock_env):
            s = load_settings(require_jwt=False)

        assert s.bot.channel_id == "-1002050105527"

    def test_load_settings_requires_jwt_secret(self):
        mock_env = MagicMock()
        mock_env.str.return_value = None
        mock_env.bool.return_value = True

        with patch.object(settings_mod, "Env", return_value=mock_env):
            with pytest.raises(ValueError, match="Configuration error"):
                load_settings(require_jwt=True)

    def test_load_settings_jwt_optional_when_not_required(self):
        mock_env = MagicMock()
        mock_env.str.side_effect = lambda key, default=None: (
            "dummy_password_for_test" if key == "POSTGRES_PASSWORD" else default
        )
        mock_env.bool.return_value = True

        with patch.object(settings_mod, "Env", return_value=mock_env):
            s = load_settings(require_jwt=False)
        assert s.jwt is None

# ============================================================
# Settings dataclass instantiation
# ============================================================

class TestSettingsDataclass:
    def test_minimal_settings(self):
        s = Settings(
            app=AppConfig(),
            db=DatabaseConfig(),
            bot=BotConfig(token="t", channel_id="-1001"),
        )
        assert s.app.host == "0.0.0.0"
        assert s.app.port == 8080
        assert s.bot.token == "t"
        assert s.bot.channel_id == "-1001"
