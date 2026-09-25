"""Тесты дедупликации повторных сообщений канала (N4c, parser/dedup.py).

Мотивация — events_export6/7: «Бугаевская перед Дальницкая блокпост» ×3,
«Окружная. ОККО…» ×2, «Орион блокпост» ×2 — каждая копия создавала отдельное
событие на карте. Дедуп на входе parser (live-хендлер + история) режет
повторы нормализованного текста в пределах окна.

Модуль parser/dedup импортируется НАПРЯМУЮ — без pyrogram/settings
(в тестовом окружении их нет); интеграционные проверки с ParserBot
— на уровне исходников (паттерн test_verifier_checks), т.к. ParserBot
требует живого Telegram-клиента и БД.
"""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# package shim (как в других тестах): parser/ без __init__-зависимостей
for pkg in ("parser",):
    if pkg not in sys.modules:
        _pkg = types.ModuleType(pkg)
        _pkg.__path__ = [str(ROOT / pkg)]
        sys.modules[pkg] = _pkg

from parser.dedup import TextDedup, dedup_key  # noqa: E402


# ===========================================================================
# dedup_key: нормализация
# ===========================================================================

def test_key_casefold_and_punctuation_stripped():
    assert dedup_key("Блокпост! У нас на карте...") == dedup_key("блокпост у нас на карте")


def test_key_ignores_emoji_and_extra_spaces():
    assert dedup_key("⛔️⛔️ народ, остановитесь!") == dedup_key("народ остановитесь")


def test_key_digits_matter():
    """«2 застава» ≠ «12 застава» — цифры часть ключа (не склеиваются)."""
    assert dedup_key("2 застава") != dedup_key("12 застава")


def test_key_empty_and_tiny_text_is_none():
    assert dedup_key("") is None
    assert dedup_key("!!!") is None
    # Ультракороткий текст: дедуп не применяется (слишком частые ключи)
    assert dedup_key("бп") is None


# ===========================================================================
# TextDedup: окно и LRU
# ===========================================================================

def test_first_occurrence_not_duplicate():
    d = TextDedup(window_seconds=1800)
    assert d.is_duplicate("Бугаевская перед Дальницкая блокпост") is False
    assert len(d) == 1


def test_repeat_within_window_is_duplicate():
    d = TextDedup(window_seconds=1800)
    d.is_duplicate("Бугаевская перед Дальницкая блокпост")
    assert d.is_duplicate("Бугаевская перед Дальницкая блокпост") is True
    assert d.is_duplicate("БУГАЕВСКАЯ перед дальницкая, блокпост!") is True


def test_repeat_after_window_is_fresh_event():
    """Повтор ПОСЛЕ окна — новое реальное событие (не дубликат)."""
    t = 1000.0
    d = TextDedup(window_seconds=1800)
    assert d.is_duplicate("Орион блокпост в обе стороны", now=t) is False
    # +29 минут — ещё дубликат, метка обновляется
    assert d.is_duplicate("Орион блокпост в обе стороны", now=t + 1700) is True
    # +35 минут от ПЕРВОЙ встречи, но +3 мин от последней — всё ещё дубликат
    assert d.is_duplicate("Орион блокпост в обе стороны", now=t + 2100) is True
    # +65 мин от последней встречи — окно истекло → свежее событие
    assert d.is_duplicate("Орион блокпост в обе стороны", now=t + 4700) is False


def test_none_key_does_not_pollute_ring():
    """Пустые/ультракороткие тексты не записываются и не считаются дубликатами."""
    d = TextDedup()
    assert d.is_duplicate("") is False
    assert d.is_duplicate("!!") is False
    assert len(d) == 0


def test_maxsize_evicts_oldest():
    d = TextDedup(window_seconds=10_000, maxsize=3)
    for i in range(5):
        d.is_duplicate(f"уникальный текст номер {i}")
    assert len(d) == 3
    # Самый старый вытеснен → снова «не дубликат»
    assert d.is_duplicate("уникальный текст номер 0") is False


def test_monotonic_time_isolated_from_wall_clock():
    """now= задаётся явно в тестах — реальное время не влияет."""
    d = TextDedup(window_seconds=60)
    assert d.is_duplicate("текст для изоляции", now=5.0) is False
    assert d.is_duplicate("текст для изоляции", now=30.0) is True


# ===========================================================================
# Реальные дубликаты из events_export6/7 (интеграционные)
# ===========================================================================

@pytest.mark.parametrize("text", [
    "Бугаевская перед Дальницкая блокпост ❗️ ❗️ ❗️",
    "Окружная. ОККО. Зеленые гниды кошмарят мужика на заправке. Не заезжаем!!!",
    "Орион блокпост ❗️ ❗️ в обе стороны на остановке.",
    "Новощепной ряд от Преображенской идут паркогниды",
])
def test_real_channel_duplicates_dropped(text):
    d = TextDedup(window_seconds=1800)
    assert d.is_duplicate(text) is False          # первая копия — событие
    for _ in range(2):                             # повторные — дубликаты
        assert d.is_duplicate(text) is True


def test_distinct_messages_not_confused():
    """Разные сообщения про один объект НЕ склеиваются."""
    d = TextDedup()
    assert d.is_duplicate("Бугаевская перед Дальницкая блокпост") is False
    assert d.is_duplicate("Бугаевская у школы менты") is False
    assert d.is_duplicate("На Бугаевская угол Мельницкая тцк блокпост") is False
    assert len(d) == 3


# ===========================================================================
# Интеграция с ParserBot (статические проверки — ParserBot требует Telegram/БД)
# ===========================================================================

def _monitoring_source() -> str:
    return (ROOT / "parser" / "monitoring.py").read_text(encoding="utf-8")


def test_monitoring_dedups_before_queue():
    """Live-хендлер: дедуп ДО постановки в очередь (дубликат не занимает слот)."""
    src = _monitoring_source()
    assert "self._dedup.is_duplicate(self._extract_text(message))" in src
    assert "parser_messages_dedup_dropped_total.inc()" in src
    # Порядок: блок дедупа встречается раньше put_nowait
    assert src.index("is_duplicate") < src.index("put_nowait")


def test_monitoring_dedups_history_load():
    """Прогрев историей тоже фильтруется и логирует количество дропов."""
    src = _monitoring_source()
    assert "duplicates dropped (dedup)" in src


def test_monitoring_dedup_disabled_switch():
    """settings.parser.dedup_window_seconds = 0 → TextDedup не создаётся."""
    src = _monitoring_source()
    assert "dedup_window_seconds" in src
    assert "if self._dedup and self._dedup.is_duplicate" in src


def test_metrics_has_dedup_counter():
    metrics = (ROOT / "common" / "metrics.py").read_text(encoding="utf-8")
    assert "parser_messages_dedup_dropped_total" in metrics
    assert metrics.count("parser_messages_dedup_dropped_total") >= 2  # real + noop
