"""Тесты адресного fast-path N4b (structured_parser + match_phrase).

Кейсы заголовков/адресов — реальные структурированные пины из
events_export{,2,3,4}.csv. Проверяем:
  1. parse_structured: детект формата, извлечение title/address/description,
     приоритет сегментов (улица раньше района), зачистка мусора
     (дома, индексы, обёртки ЖК), негативы (обычные тексты → None).
  2. match_phrase: прицельный матч сегмента теми же порогами
     (Пантелеймоновская 1.0, опечатка «Аркадьевскмй» ловится Tier 2 при
     наличии объекта в справочнике — пока отсутствует, поэтому матч на
     «Аркадия» из соседнего сегмента).
  3. Приоритет улицы над районом: «Кильцева улица … Аркадия» с известной
     улицей должен дать улицу, а не POI «Аркадия».
"""

import asyncio
import csv
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("mawo_pymorphy3")
pytest.importorskip("rapidfuzz")

ROOT = Path(__file__).resolve().parent.parent
csv.field_size_limit(10_000_000)

for pkg in ("parser", "nlp_processor"):
    if pkg not in sys.modules:
        _pkg = types.ModuleType(pkg)
        _pkg.__path__ = [str(ROOT / pkg)]
        sys.modules[pkg] = _pkg

from nlp_processor.morphology import Morphology              # noqa: E402
from nlp_processor.phonetic_index import PhoneticIndex       # noqa: E402
from nlp_processor.geo_matcher import GeoMatcher              # noqa: E402
from nlp_processor.structured_parser import (                 # noqa: E402
    parse_structured, address_segments,
)
from nlp_processor import main as nlp_main                    # noqa: E402
from common.settings import settings as _settings             # noqa: E402


@pytest.fixture(scope="module")
def matcher():
    morph = Morphology()
    index = PhoneticIndex(morph)
    rows, name2id = [], {}
    with open(ROOT / "postgres/data/geo.csv", encoding="utf-8") as f:
        rd = csv.reader(f)
        next(rd)
        gid = 0
        for r in rd:
            if not r or not r[0].strip():
                continue
            gid += 1
            names = r[0].split("|")
            rows.append({"id": gid, "names": names})
            name2id.setdefault(names[0], gid)
    index.build(rows)
    m = GeoMatcher(morph, index)
    m._initialized = True
    m._stopwords = set()
    m._morph = morph
    m._name2id = name2id
    return m


# ===========================================================================
# 1. parse_structured
# ===========================================================================

PIN_FULL = ("📍 Полиция (пешие) 🏠 Адрес: 21, Пантелеймоновская улица, Центр "
            "📝 Описание: 4 пеших с собакой")


def test_parse_structured_full_pin():
    sm = parse_structured(PIN_FULL)
    assert sm is not None
    assert sm.title == "Полиция (пешие)"
    assert sm.address == "21, Пантелеймоновская улица, Центр"
    assert sm.description == "4 пеших с собакой"


def test_parse_segments_street_first_and_junk_dropped():
    sm = parse_structured(
        "📍 Блокпост 🏠 Адрес: 86, Люстдорфская дорога, 4-й квартал, 65080 "
        "📝 Описание: В составе могут быть и ТЦК и полиция"
    )
    assert sm is not None
    # Улица с типом дороги — первая; дом «86» и индекс «65080» выкинуты;
    # «4-й квартал» — содержательный сегмент без типа улицы.
    assert sm.address_segments[0] == "Люстдорфская дорога"
    assert "86" not in sm.address_segments
    assert "65080" not in sm.address_segments
    assert "4-й квартал" in sm.address_segments


def test_parse_strips_jk_wrappers():
    """Обёртки ЖК снимаются до имени: «ОК ЖСТ «Морське»» → «Морське»."""
    sm = parse_structured(
        "📍 Черный транспорт 🏠 Адрес: Кильцева улица, ОК ЖСТ «Морське», "
        "Аркадия, 65062 📝 Описание: Черный бус или легковой авто ТЦК"
    )
    assert sm is not None
    assert "Морське" in sm.address_segments
    assert not any("ЖСТ" in s for s in sm.address_segments)
    assert "65062" not in sm.address_segments


def test_parse_house_frac_and_letter():
    """Дома с дробью и корпусом: «4/6», «22/1», «21а» — мусор."""
    segs = address_segments("4/6, Аркадьевский переулок, 22/1, 21а, Бугаевка")
    assert "Аркадьевский переулок" in segs
    assert "Бугаевка" in segs
    assert not any(s.startswith(("4/", "22/", "21")) for s in segs)


@pytest.mark.parametrize("text", [
    "По Ришельевская в сторону ЖД едет полиция медленно",
    "Адрес заправки не помню, но полиция там стоит",   # «Адрес» без 🏠+«Адрес:»
    "блокпост ❗️ ❗️ у моста",
    "",
    None,
])
def test_parse_negative_non_structured(text):
    assert parse_structured(text) is None


def test_parse_without_description():
    sm = parse_structured("📍 Тцк (пешие) 🏠 Адрес: АТБ-Маркет, улица Василия Стуса")
    assert sm is not None
    assert sm.description == ""
    assert sm.address_segments[0] == "улица Василия Стуса"


# ===========================================================================
# 2. match_phrase: прицельный матч теми же порогами
# ===========================================================================

async def test_match_phrase_street_exact(matcher):
    ent = await matcher.match_phrase("Пантелеймоновская улица")
    assert ent is not None
    assert ent["geo_id"] == matcher._name2id["Пантелеймоновская"]
    assert ent["score"] >= 0.80
    assert "_span" not in ent  # служебные поля вычищены


async def test_match_phrase_house_prefix_not_matched(matcher):
    """«86, Люстдорфская дорога» после зачистки — улица матчится confidently."""
    ent = await matcher.match_phrase("Люстдорфская дорога")
    assert ent is not None and ent["score"] >= 0.80
    assert ent["geo_id"] == matcher._name2id["Люстдорфская"]


async def test_match_phrase_unknown_phrase_returns_none_or_low(matcher):
    """Незнакомый сегмент («Кильцева улица» — нет в справочнике) не даёт
    уверенного матча — fast-path корректно уйдёт в fallback."""
    ent = await matcher.match_phrase("Кильцева улица")
    assert ent is None or ent["score"] < 0.80


async def test_match_phrase_empty_and_not_initialized():
    m = GeoMatcher(Morphology(), PhoneticIndex(Morphology()))
    m._initialized = False
    assert await m.match_phrase("Пантелеймоновская улица") is None


# ===========================================================================
# 3. Приоритет улицы над районом (сквозной, на реальном пине)
# ===========================================================================

async def test_fastpath_street_beats_district(matcher):
    """Пин «… улица Василия Стуса, Дальние Мельницы»: улица — первый сегмент,
    матч уверенный (Стуса 1.0) — fast-path вернёт улицу, а не POI/район
    из соседних сегментов (так раньше «Средний Фонтан» побеждал «Ралли»)."""
    pin = ("📍 Тцк (пешие) 🏠 Адрес: АТБ-Маркет, улица Василия Стуса, "
           "Дальние Мельницы 📝 Описание: ТЦК")
    sm = parse_structured(pin)
    assert sm.address_segments[0] == "улица Василия Стуса"
    ent = await matcher.match_phrase(sm.address_segments[0])
    assert ent["geo_id"] == matcher._name2id["Стуса"]
    assert ent["score"] >= 0.80


# ===========================================================================
# 4. Хук в _process_row: kill-switch + fallback-семантика
# ===========================================================================

def _bare_bot(matcher):
    """NlpProcessorBot без __init__ (тяжёлые подсистемы подменены харнессом)."""
    bot = object.__new__(nlp_main.NlpProcessorBot)
    bot.matcher = matcher
    return bot


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_hook_fastpath_off_is_general_path(matcher, monkeypatch):
    """Kill-switch: structured_fastpath=False → хук вызывает общий find_geo
    на ПОЛНОМ тексте (тем же вызовом, что старый код) — поведение байт-в-байт."""
    monkeypatch.setattr(_settings.geo, "structured_fastpath", False)
    calls = []

    async def _fake_find_geo(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(matcher, "find_geo", _fake_find_geo)
    bot = _bare_bot(matcher)
    pin = ("📍 Полиция (пешие) 🏠 Адрес: 21, Пантелеймоновская улица, Центр "
           "📝 Описание: 4 пеших с собакой")
    from nlp_processor.word_tokenizer import tokenize
    from common.text_preprocessor import preprocess_light, strip_tail
    pre = preprocess_light(strip_tail(pin))
    toks = tokenize(pre)
    lemmas = matcher._morph.lemmatize_tokens(toks)
    result = _run(bot._find_geo_with_fastpath(pre, toks, lemmas, 1))
    assert result == []
    assert len(calls) == 1
    # Общий путь получил полный текст и те же токены:
    assert calls[0][1]["text"] == pre
    assert calls[0][1]["tokens"] is toks


def test_hook_fastpath_matched_address_single_candidate(matcher, monkeypatch):
    """Fast-path ON + уверенный матч адреса → ЕДИНСТВЕННЫЙ кандидат (улица),
    общий find_geo по ПОЛНОМУ тексту не вызывается.

    Spy-обёртка вокруг ОРИГИНАЛЬНОГО метода класса (не инстанс-атрибута —
    фикстура module-scoped и могла быть подменена предыдущим тестом):
    match_phrase внутри делегирует в find_geo БЕЗ text=, а общий путь
    (и fallback) всегда передаёт text= — по нему и отличаем."""
    monkeypatch.setattr(_settings.geo, "structured_fastpath", True)
    general_calls = []
    original = GeoMatcher.find_geo  # unbound: вызываем original(matcher, ...)

    async def _spy_find_geo(*args, **kwargs):
        if "text" in kwargs:
            general_calls.append(kwargs)
        return await original(matcher, *args, **kwargs)

    monkeypatch.setattr(matcher, "find_geo", _spy_find_geo)
    bot = _bare_bot(matcher)
    pin = ("📍 Полиция (пешие) 🏠 Адрес: 21, Пантелеймоновская улица, Центр "
           "📝 Описание: 4 пеших с собакой")
    from nlp_processor.word_tokenizer import tokenize
    from common.text_preprocessor import preprocess_light, strip_tail
    pre = preprocess_light(strip_tail(pin))
    toks = tokenize(pre)
    lemmas = matcher._morph.lemmatize_tokens(toks)
    ents = _run(bot._find_geo_with_fastpath(pre, toks, lemmas, 2))
    assert len(ents) == 1
    assert ents[0]["geo_id"] == matcher._name2id["Пантелеймоновская"]
    assert general_calls == []  # общий путь не понадобился


def test_hook_fastpath_fallback_on_no_match(matcher, monkeypatch):
    """Fast-path ON, адрес весь неизвестен справочнику → fallback на общий
    путь (find_geo по полному тексту) — поведение идентично старому."""
    monkeypatch.setattr(_settings.geo, "structured_fastpath", True)
    general_calls = []

    async def _fake_find_geo(*args, **kwargs):
        general_calls.append(kwargs)
        return [{"geo_id": 999, "score": 0.9, "matched_name": "X",
                 "text": "x", "source": "stem_exact"}]

    monkeypatch.setattr(matcher, "find_geo", _fake_find_geo)
    bot = _bare_bot(matcher)
    pin = ("📍 Блокпост 🏠 Адрес: Кильцева улица, ОК ЖСТ «Морське», Аркадия, "
           "65062 📝 Описание: Черный бус")
    from nlp_processor.word_tokenizer import tokenize
    from common.text_preprocessor import preprocess_light, strip_tail
    pre = preprocess_light(strip_tail(pin))
    toks = tokenize(pre)
    lemmas = matcher._morph.lemmatize_tokens(toks)
    ents = _run(bot._find_geo_with_fastpath(pre, toks, lemmas, 3))
    assert ents and ents[0]["geo_id"] == 999
    assert len(general_calls) == 1  # fallback состоял в общий путь
