#!/usr/bin/env python3
"""CI-check: Secure by Default логика TELEGRAM_WEBVIEW_VALIDATION.

H-2: заменяет scripts/ci_check_webview_validation.py, удалённый в 719f749,
из-за чего матричная джоба test:core-startup-matrix падала на каждом пайплайне.

Используется:
  - .gitlab-ci.yml  → джоба test:core-startup-matrix (parallel:matrix) —
    значения прилетают переменными окружения TEST_ENV_VALUE /
    EXPECTED_BOOL / EXPECTED_LOG_WARNING;
  - run-ci-local.sh → тот же скрипт, значения передаются argv.

Проверяет, что load_settings корректно парсит переменную (True по умолчанию,
False только при 'false'/'0') и что при dev-bypass логируется "SECURITY RISK".

M-1: common/settings.py теперь fail-fast на POSTGRES_PASSWORD — подставляем
валидный тестовый пароль, если он не пришёл из окружения.
"""
import logging
import os
import sys
import tempfile

# Аргументы: argv (run-ci-local.sh) ИЛИ env (GitLab parallel:matrix).
argv = sys.argv[1:]
test_env_value = argv[0] if len(argv) > 0 else os.environ.get("TEST_ENV_VALUE")
expected_bool_arg = argv[1] if len(argv) > 1 else os.environ.get("EXPECTED_BOOL", "")
expected_log_warning_arg = (
    argv[2] if len(argv) > 2 else os.environ.get("EXPECTED_LOG_WARNING", "")
)

if test_env_value is None:
    print("FAIL: TEST_ENV_VALUE is not set (argv or env)")
    sys.exit(1)

expected_bool = expected_bool_arg.lower() == "true"
expected_log_warning = expected_log_warning_arg.lower() == "true"

os.environ.setdefault("POSTGRES_PASSWORD", "ci-matrix-postgres-password")

with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
    env_path = f.name
    if test_env_value == "UNSET":
        f.write("# TELEGRAM_WEBVIEW_VALIDATION is intentionally unset\n")
    else:
        f.write(f"TELEGRAM_WEBVIEW_VALIDATION={test_env_value}\n")

try:
    # env var ДО импорта: module-level load_settings() в common/settings.py
    # видит значение в момент импорта модуля.
    if test_env_value == "UNSET":
        os.environ.pop("TELEGRAM_WEBVIEW_VALIDATION", None)
    else:
        os.environ["TELEGRAM_WEBVIEW_VALIDATION"] = test_env_value

    from common.settings import load_settings

    # ВАЖНО: повторно приводим состояние os.environ ПОСЛЕ импорта.
    # Import-time load_settings() (module-level settings) читает .env из CWD
    # через read_env(None), и dotenv добавляет его значения в os.environ —
    # в dev-окружении утёкший TELEGRAM_WEBVIEW_VALIDATION='0'/'false' ломал
    # бы комбинацию UNSET (ожидалось отсутствие переменной, а не значение).
    # В CI-чекaуте .env нет, но скрипт должен быть устойчив в любом CWD.
    if test_env_value == "UNSET":
        os.environ.pop("TELEGRAM_WEBVIEW_VALIDATION", None)
    else:
        os.environ["TELEGRAM_WEBVIEW_VALIDATION"] = test_env_value

    # Хендлер цепляется только вокруг целевого вызова: import-time
    # load_settings мог успеть залогировать чужой SECURITY RISK — без
    # этого фильтра has_warning загрязнялся бы не тем предупреждением.
    log_output = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            log_output.append(self.format(record))

    root = logging.getLogger()
    handler = ListHandler()
    root.addHandler(handler)
    try:
        settings_obj = load_settings(env_path=env_path, require_jwt=False)
    finally:
        root.removeHandler(handler)
    actual_bool = settings_obj.app.telegram_webview_validation
    has_warning = any(
        "SECURITY RISK" in msg and "TELEGRAM_WEBVIEW_VALIDATION" in msg
        for msg in log_output
    )

    print(f"CHECK: Expected bool={expected_bool}, Actual={actual_bool}")
    print(f"CHECK: Expected warning={expected_log_warning}, Actual={has_warning}")

    if actual_bool != expected_bool or has_warning != expected_log_warning:
        print("FAIL: Secure by Default logic is broken!")
        sys.exit(1)
    print("PASS: Logic is correct.")
except SystemExit:
    raise
except Exception as e:
    print(f"FAIL: Exception during check: {e}")
    sys.exit(1)
finally:
    if os.path.exists(env_path):
        os.remove(env_path)
