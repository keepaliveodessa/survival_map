"""Regression-тесты GeoMatcher на промахах из боевого экспорта events_export.csv.

Каждый тест ссылается на конкретное событие экспорта (id в таблице events) и
на корень бага, который его породил. Индекс строится из postgres/data/geo.csv
(тот же харнесс, что test_street_matcher.py — без БД).

Покрытые корни:
  1. _NOISE_TOKENS вырезал «новая» → окно «новая дорога» не генерировалось
     (события id 31, 32, 66) — прилагательные являются частью официальных имён.
  2. Дедуп стем-ключей в PhoneticIndex выбрасывал все алиасы-омонимы одного
     объекта, кроме первого: «6 элемент» брал ratio «Шестой элемент» → 0.696
     ниже порога 0.80 (событие id 26).
  3. Аббревиатура «Д.донского» давала однословное окно «донского» без
     собственного стем-ключа — молчаливый промах (событие id 1). Спасается
     subset-rescue с guard'ами (head-слова, object_count, partial_ratio).
  4. «Новая дорга» (опечатка) — Tier 2 не срабатывал из-за п.1 (событие id 13).
  5. FP-гварды: rescue не должен матчить родовые слова («дорога», «улица»),
     а изменение выбора best не должно ломать sibling-алиасы («Гаваи»/«Гаванная»).

Тесты SKIP-аются без runtime-зависимостей (mawo_pymorphy3/rapidfuzz/snowballstemmer).
"""

import asyncio
import csv
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("mawo_pymorphy3")
pytest.importorskip("rapidfuzz")
pytest.importorskip("snowballstemmer")

ROOT = Path(__file__).resolve().parent.parent
csv.field_size_limit(10_000_000)  # geo.csv: очень длинные WKT-поля

# parser/__init__.py тянет asyncpg/kurigram; подменяем пакет стабом с __path__,
# чтобы относительные импорты сабмодулей резолвились без __init__.
if "parser" not in sys.modules:
    _pkg = types.ModuleType("parser")
    _pkg.__path__ = [str(ROOT / "parser")]
    sys.modules["parser"] = _pkg

if "nlp_processor" not in sys.modules:
    _pkg = types.ModuleType("nlp_processor")
    _pkg.__path__ = [str(ROOT / "nlp_processor")]
    sys.modules["nlp_processor"] = _pkg

from nlp_processor.morphology import Morphology              # noqa: E402
from nlp_processor.phonetic_index import PhoneticIndex       # noqa: E402
from nlp_processor.geo_matcher import GeoMatcher              # noqa: E402
from nlp_processor.word_tokenizer import tokenize             # noqa: E402
from common.text_preprocessor import preprocess_light, strip_tail  # noqa: E402


def _load_geo():
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
    return rows, name2id


def _load_stopwords():
    stop = set()
    with open(ROOT / "postgres/data/stopwords.csv", encoding="utf-8") as f:
        rd = csv.reader(f)
        next(rd)
        for r in rd:
            if r and r[0].strip():
                stop.add(r[0].strip().lower())
    return stop


@pytest.fixture(scope="module")
def matcher():
    morph = Morphology()
    index = PhoneticIndex(morph)
    rows, name2id = _load_geo()
    index.build(rows)
    m = GeoMatcher(morph, index)
    m._initialized = True
    m._stopwords = _load_stopwords()
    m._morph = morph
    m._name2id = name2id
    return m


async def _ids(matcher, text):
    pre = preprocess_light(strip_tail(text or ""))
    toks = tokenize(pre)
    lemmas = matcher._morph.lemmatize_tokens(toks)
    return {e["geo_id"] for e in await matcher.find_geo(tokens=toks, lemmas=lemmas)}


async def _entities(matcher, text):
    pre = preprocess_light(strip_tail(text or ""))
    toks = tokenize(pre)
    lemmas = matcher._morph.lemmatize_tokens(toks)
    return await matcher.find_geo(tokens=toks, lemmas=lemmas)


# ===========================================================================
# Корень 1: _NOISE_TOKENS (события id 31, 32, 66: «Новая дорога …» → random)
# ===========================================================================

@pytest.mark.parametrize("text", [
    # id 31: «Новая дорога стали менты проверка документов, останавливают выборочно»
    "Новая дорога стали менты проверка документов, останавливают выборочно",
    # id 66: «Новая дорога блокпост ❗️ ❗️»
    "Новая дорога блокпост ❗️ ❗️",
    # id 32: «Новая дорога стоя мусора и останавливают выборочно»
    "Новая дорога стоя мусора и останавливают выборочно",
])
async def test_novaya_doroga_recall(matcher, text):
    """«Новая дорога» — официальное имя объекта; «новая» не шум, а часть имени."""
    assert matcher._name2id["новая дорога"] in await _ids(matcher, text)


async def test_novaya_doroga_confident_score(matcher):
    """Скор точного стем-матча не деградировал от фиксов (1.0, stem_exact)."""
    ents = await _entities(matcher, "Новая дорога стали менты, проверяют выборочно")
    target = [e for e in ents if e["geo_id"] == matcher._name2id["новая дорога"]]
    assert target and target[0]["score"] >= 0.9 and target[0]["source"] == "stem_exact"


# ===========================================================================
# Корень 2: дедуп стем-омонимов алиасов (событие id 26: «6 элемент …» → random)
# ===========================================================================

async def test_shestoy_element_digit_alias(matcher):
    """«6 элемент» должен матчить объект по идентичному алиасу (score >= 0.9).

    До фикса: алиасы «Шестой элемент» и «6 элемент» коллапсировали в один
    стем-ключ ('6','элемент') (словесные порядковые → цифра), в индексе
    оставался только первый, и точный «6 элемент» получал ratio 0.64.
    """
    ents = await _entities(matcher, "6 элемент, в сторону города блокпост ❗️ ❗️")
    target = [e for e in ents if e["geo_id"] == matcher._name2id["Шестой элемент"]]
    assert target, "«6 элемент» не сматчился вовсе"
    assert target[0]["score"] >= 0.9


async def test_shestoy_element_verbal_alias_still_works(matcher):
    """Словесная форма «Шестой элемент» не пострадала от фикса дедупа."""
    assert matcher._name2id["Шестой элемент"] in await _ids(
        matcher, "возле Шестого элемента пробка"
    )


# ===========================================================================
# Корень 3: subset-rescue для аббревиатуры (событие id 1: «Д.донского/ Ромашковая»)
# ===========================================================================

async def test_abbreviated_surname_rescue(matcher):
    """«Д.донского» → однословное окно «донского» рескьюится до полного имени."""
    ents = await _entities(matcher, "Д.донского/ Ромашковая разворачивают блокпост ❗️")
    target = [e for e in ents if e["geo_id"] == matcher._name2id["Дмитрия Донского"]]
    assert target, "«Д.донского» не сматчился"
    assert target[0]["source"] == "stem_subset"
    # Скор rescue проходит порог confident (0.80), но ниже exact (0.9)
    assert 0.80 <= target[0]["score"] < 0.9


async def test_full_surname_still_exact(matcher):
    """Полное «Дмитрия Донского» матчится как раньше (exact, высокий скор)."""
    ents = await _entities(matcher, "Дмитрия донского, возле белого дома стоит полиция")
    target = [e for e in ents if e["geo_id"] == matcher._name2id["Дмитрия Донского"]]
    assert target and target[0]["source"] == "stem_exact" and target[0]["score"] >= 0.9


# ===========================================================================
# Корень 4: опечатка «дорга» — Tier 2 (событие id 13) — работал бы без п.1
# ===========================================================================

async def test_novaya_doroga_typo_tier2(matcher):
    """«Новая дорга» (опечатка) → Tier 2 по хвосту «дорга» → «новая дорога»."""
    assert matcher._name2id["новая дорога"] in await _ids(
        matcher, "Новая дорга блокпост ❗️, только что стали"
    )


# ===========================================================================
# Верифицированные в экспорте правильные кейсы — не сломать фиксами
# ===========================================================================

async def test_bugayevska_typo_still_matches(matcher):
    """id 72: «Бугаевска за ЖД переездом» — Tier 2 ловит опечатку (0.947 в экспорте)."""
    ents = await _entities(matcher, "Бугаевска за ЖД переездом блокпост ❗️ ❗️! Обе стороны")
    target = [e for e in ents if e["geo_id"] == matcher._name2id["Бугаёвская"]]
    assert target and target[0]["score"] >= 0.9


async def test_rishelievskaya_and_zhd_both_matched(matcher):
    """id 14: «По Ришельевская в сторону ЖД» — ДВА матча (улица + вокзал)."""
    ids = await _ids(matcher, "По Ришельевская в сторону ЖД едет полиция медленно")
    assert matcher._name2id["Ришельевская"] in ids
    assert matcher._name2id["железнодорожный вокзал"] in ids


async def test_multi_street_message(matcher):
    """id 68: «…за Кактусом в между Ромашковая и Левкойная» — все 4 кандидата."""
    ids = await _ids(
        matcher, "Дмитрий Донского за Кактусом в между Ромашковая и Левкойная блокпост"
    )
    for name in ("Ромашковая", "Левкойная", "Дмитрия Донского", "Кактус"):
        assert matcher._name2id[name] in ids, name


# ===========================================================================
# FP-гварды subset-rescue (регресс-защита самих фиксов)
# ===========================================================================

@pytest.mark.parametrize("text", [
    "где гоночка дорога на 7-й, тормозят копи",   # «дорога» → «южная дорога»?
    "улица перекрыта вся",                        # «улица» → «Улица Толбухина»?
    "рынок утром качают",                         # «рынок» → «Новый Рынок»?
    "парк гуляли ночью",                          # «парк» → парковые объекты?
])
async def test_rescue_no_fp_generic_words(matcher, text):
    """Родовые head-слова не должны вытягивать объекты через subset-rescue."""
    ids = await _ids(matcher, text)
    for name in ("Южная дорога", "Улица Толбухина", "Новый Рынок"):
        assert matcher._name2id.get(name) not in ids, f"{name} в {text!r}"


async def test_rescue_no_fp_mayakovsky(matcher):
    """«Маяковского» (нет в справочнике) не должен снапаться на «Маловского»
    через subset/typo-пути."""
    assert matcher._name2id["Маловского"] not in await _ids(
        matcher, "собрались и поехали по Маяковского"
    )


async def test_rescue_no_fp_gavai_vs_gavannaya(matcher):
    """Sibling-алиасы: «на Гаванной» → Гаванная (150), не Гаваи (149).

    Регресс-защита изменения критерия выбора best в _link_span_tier1.
    """
    ids = await _ids(
        matcher, "На Гаванной опасно, с блокпост побежали искать. На всей гаванной опасно"
    )
    assert matcher._name2id["Гаванная"] in ids
    assert matcher._name2id.get("Гаваи") not in ids
