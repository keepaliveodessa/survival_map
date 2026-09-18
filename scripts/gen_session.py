#!/usr/bin/env python3
"""Одноразовая генерация пользовательской Telegram-сессии для парсера.

Создаёт `parser/session.session` — имя и каталог совпадают с тем, что ждёт
`parser/monitoring.py` (`Client(name="session", workdir=".../parser")`), поэтому
ручной `mv`/`chmod` больше не нужен.

`api_id`/`api_hash` и номер телефона передаются аргументами командной строки
и зашиваются внутрь `session.session`: в репозитории они не хранятся
(`*.session` — в `.gitignore`).

Запуск (в venv с установленными `kurigram` и `qrcode`):
    python scripts/gen_session.py <api_id> <api_hash>                          # вход по QR
    python scripts/gen_session.py <api_id> <api_hash> --phone +79991234567    # вход по телефону + коду
    python scripts/gen_session.py <api_id> <api_hash> --session-output /path  # кастомный путь (по умолчанию — parser/session.session)
"""

import argparse
import os
import re
import stat
import sys
from pathlib import Path

# Каталог parser/ в корне проекта (родитель scripts/) — сессия попадёт туда
# независимо от текущей рабочей директории и того, откуда вызван скрипт.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
PARSER_DIR = PROJECT_ROOT / "parser"
DEFAULT_SESSION_FILE = PARSER_DIR / "session.session"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a Telegram session file for the parser.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "api_id/api_hash получаются на https://my.telegram.org/apps.\n"
            "Примеры:\n"
            "  python scripts/gen_session.py 12345 abcdef0123456789abcdef0123456789  # QR\n"
            "  python scripts/gen_session.py 12345 abcdef0123456789 --phone +79991234567  # SMS\n"
        ),
    )
    parser.add_argument("api_id", type=str, help="Telegram api_id (целое число)")
    parser.add_argument("api_hash", type=str, help="Telegram api_hash (32-символьный hex)")
    parser.add_argument("--phone", type=str, default=None, help="Номер телефона (+X...) для входа по коду")
    parser.add_argument(
        "--session-output",
        type=str,
        default=str(DEFAULT_SESSION_FILE),
        help="Путь для сохранения session.session (по умолчанию: parser/session.session)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # api_id — целое число
    try:
        api_id = int(args.api_id)
    except ValueError:
        print("ERROR: api_id должен быть целым числом", file=sys.stderr)
        return 1

    if api_id <= 0:
        print("ERROR: api_id должен быть положительным числом", file=sys.stderr)
        return 1

    # api_hash — 32-символьный hex (предупреждение, а не ошибка)
    api_hash = args.api_hash.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        print(
            "WARNING: api_hash не похож на 32-символьный hex. "
            "Убедитесь, что значение верное.",
            file=sys.stderr,
        )

    phone = (args.phone or "").strip() if args.phone else ""
    session_output = args.session_output

    try:
        from pyrogram import Client  # модуль ставится пакетом kurigram
    except ImportError:
        print(
            "Не найден модуль pyrogram. Установите зависимости в venv:\n"
            "    python3 -m venv .venv && source .venv/bin/activate\n"
            "    pip install kurigram qrcode",
            file=sys.stderr,
        )
        return 1

    session_output_path = Path(session_output)
    output_dir = session_output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    app = Client(
        session_output_path.stem,
        api_id=api_id,
        api_hash=api_hash,
        workdir=str(output_dir),
        phone_number=phone if phone else None,
    )

    if not phone:
        print("Вход по QR: Telegram → Настройки → Устройства → "
              "«Подключить устройство» → отсканируйте QR ниже.")
    # use_qr=True показывает QR в терминале (нужен пакет qrcode); --phone
    # переключает на интерактивный ввод номера + кода (+ пароль 2FA).
    app.start(use_qr=not bool(phone))
    me = app.get_me()
    app.stop()

    # Права 600 — секрет доступа к аккаунту не должен быть читаем другими.
    try:
        os.chmod(session_output_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    handle = f"@{me.username}" if getattr(me, "username", None) else (me.first_name or "—")
    print(f"\n✅ Сессия создана: {session_output_path} (вход как {handle})")
    print("   Права 600 выставлены. Теперь можно запускать docker compose up -d.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
