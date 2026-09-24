"""Tests for common/text_preprocessor (pure regex/HTML, no heavy deps)."""
from conftest import load_module_by_path

tp = load_module_by_path("_tp_under_test", "common/text_preprocessor.py")


def test_strip_tail_cuts_at_marker():
    assert tp.strip_tail("ДТП на Дерибасовской сообщить детали") == "ДТП на Дерибасовской"
    assert tp.strip_tail("без маркеров тут") == "без маркеров тут"
    assert tp.strip_tail("") == ""


def test_strip_tail_cuts_structured_pin_suffix():
    """Хвост «🌐 Открыть пин на карте» структурированных пинов обрезается
    (вместе с ведущими пробелами); остальной текст не трогается."""
    text = ("📍 Полиция (пешие) 🏠 Адрес: 21, Пантелеймоновская улица, Центр "
            "📝 Описание: 4 пеших с собакой 🌐 Открыть пин на карте")
    assert tp.strip_tail(text) == (
        "📍 Полиция (пешие) 🏠 Адрес: 21, Пантелеймоновская улица, Центр "
        "📝 Описание: 4 пеших с собакой"
    )
    # Регистр и вариативные пробелы не важны:
    assert tp.strip_tail("блокпост у моста  открыть пин на карте") == "блокпост у моста"
    # Обычные сообщения, где хвоста нет, не меняются:
    assert tp.strip_tail("пин на карте отдельно не рисуем") == "пин на карте отдельно не рисуем"


def test_preprocess_preserves_case_and_punctuation():
    # description goes to the frontend → case + punctuation must survive
    assert tp.preprocess_light("Привет, мир!") == "Привет, мир!"


def test_preprocess_strips_html_and_timestamp():
    out = tp.preprocess_light("ДТП в <b>14:30</b> на углу")
    assert "14:30" not in out
    assert "<b>" not in out and "</b>" not in out
    assert "ДТП" in out and "углу" in out


def test_preprocess_removes_hash_keeps_word():
    out = tp.preprocess_light("#Дерибасовская перекрыта")
    assert "#" not in out
    assert "Дерибасовская" in out


def test_preprocess_collapses_bp_slash():
    out = tp.preprocess_light("б/п на въезде")
    assert "бп" in out
    assert "/" not in out


def test_preprocess_ua_letters_to_ru():
    assert tp.preprocess_light("сім") == "сим"   # і → и
    assert tp.preprocess_light("є") == "е"        # є → е


def test_preprocess_ua_suffix_normalisation():
    # 'ська' → 'ская' (after і→и)
    assert tp.preprocess_light("Пушкінська").lower().endswith("ская")


def test_clean_lowercases_and_strips_punctuation():
    assert tp.clean("Дерибасовская, 5!") == "дерибасовская 5"
    assert tp.clean("") == ""
