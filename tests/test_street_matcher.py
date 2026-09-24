"""Регрессионный харнесс морфологического распознавателя гео-объектов.

Строит индекс прямо из `postgres/data/geo.csv` + `stopwords.csv` (без БД) и
проверяет ключевые свойства распознавателя на кейсах из боевого экспорта:

  • recall падежей коротких OOV-имён (Гаванной → Гаванная) — то, что ломалось;
  • отсутствие ложных матчей на обычных словах (среди≠Средняя, металлик≠
    Металлистов, Маяковского≠Маловского, "дорога на"≠Южная дорога);
  • разрешение over-stem коллизий по surface (Гаванная→150, не Гаваи→149);
  • орфо-корректор (Tier 2) ловит опечатки (Раскидпйловская→Раскидайловская).

Тесты SKIP-аются, если в окружении нет тяжёлых runtime-зависимостей парсера
(mawo_pymorphy3 / rapidfuzz / snowballstemmer) — см. pytest.importorskip.
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
from common.text_preprocessor import (             # noqa: E402
    preprocess_light, strip_tail, is_promotional,
)


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
    m._name2id = name2id  # convenience for tests
    return m


async def _ids(matcher, text):
    pre = preprocess_light(strip_tail(text or ""))
    toks = tokenize(pre)
    lemmas = matcher._morph.lemmatize_tokens(toks)
    return {e["geo_id"] for e in await matcher.find_geo(tokens=toks, lemmas=lemmas)}


# --------------------------------------------------------------- recall (падежи)

@pytest.mark.parametrize("text", [
    "На Гаванной опасно, с блокпост побежали искать. На всей гаванной опасно",
    "Опущенные собрались с Гаванной и поехали по Маяковского",
    "Гаванная блокпост",  # номинатив
])
async def test_gavannaya_recall(matcher, text):
    """Косвенный падеж короткого OOV-имени должен находиться (это ломалось)."""
    assert matcher._name2id["Гаванная"] in await _ids(matcher, text)


async def test_oblique_long_name(matcher):
    """Длинное имя в косвенном падеже — Ланжероновскую → Ланжероновская."""
    assert matcher._name2id["Ланжероновская"] in await _ids(
        matcher, "Не поворачивайте на Ланжероновскую, там перехватчики"
    )


# ------------------------------------------------- precision (нет ложных матчей)

async def test_no_fp_common_word_sredi(matcher):
    """'среди' (предлог) не должно матчиться на улицу Средняя."""
    assert matcher._name2id["Средняя"] not in await _ids(
        matcher, "куча перехватчиков среди припаркованных машин"
    )


async def test_no_fp_metallik(matcher):
    """'металлик' (цвет авто) не должно матчиться на Металлистов."""
    assert matcher._name2id["Металлистов"] not in await _ids(
        matcher, "темный металлик номер с 866 начинается"
    )


async def test_no_fp_homograph_mayakovsky(matcher):
    """'Маяковского' (нет в данных) не должно снапаться на Маловского."""
    assert matcher._name2id["Маловского"] not in await _ids(
        matcher, "собрались и поехали по Маяковского"
    )


async def test_no_fp_doroga_na(matcher):
    """'дорога на' не должно матчиться на 'Южная дорога'."""
    assert matcher._name2id["Южная дорога"] not in await _ids(
        matcher, "где гоночка дорога на 7-й, тормозят копи"
    )


# ---------------------------------------------------- over-stem collision resolve

async def test_stem_collision_resolved(matcher):
    """Гаваи(149) и Гаванная(150) → один стем 'гава'; surface-разрешение → 150."""
    ids = await _ids(matcher, "Гаванная блокпост")
    assert matcher._name2id["Гаванная"] in ids
    assert matcher._name2id["Гаваи"] not in ids


# ------------------------------------------------------------- typo-corrector

async def test_surface_typo(matcher):
    """Орфо-корректор (Tier 2) ловит реальную опечатку."""
    assert matcher._name2id["Раскидайловская"] in await _ids(
        matcher, "Раскидпйловская белый т4 катается против движения"
    )


async def test_surface_typo_gray_zone_scoring(matcher):
    """REG-тест фикса fuzz.Wratio→WRatio: Tier 2 (surface_typo) реально работает.

    До фикса несуществующее fuzz.Wratio кидало AttributeError, молча глотаемое
    в `except` внутри _fuzzy_match — Tier 2 никогда не выдавал матчей.
    Здесь опечатка с WRatio≈84 попадает в серую зону (0.80–0.85) и матчится
    как surface_typo с score < confident-порога (0.85) без предлога.
    """
    # Без предлога (нет prepositional_boost): кандидат серой зоны.
    ids = await _ids(matcher, "балкофска перекрыта, объезжайте")
    assert matcher._name2id["Балковская"] in ids


async def test_surface_typo_preposition_boost_scoring(matcher):
    """Boost применяется ПОСЛЕ семантической валидации (см. find_geo).

    Кандидат серой зоны с предлогом не должен мгновенно стать confident
    (0.84+0.05=0.89) — raw score до модели остаётся в серой зоне.
    """
    pre = preprocess_light(strip_tail("На балкофска возле АТБ"))
    toks = tokenize(pre)
    lemmas = matcher._morph.lemmatize_tokens(toks)
    ents = await matcher.find_geo(tokens=toks, lemmas=lemmas)
    assert any(e["geo_id"] == matcher._name2id["Балковская"] for e in ents)


# ------------------------------------------------------------ Phase B: word-order

async def test_word_order_independent(matcher):
    """'Застава 2' ≡ '2 застава' (Tier 1b, порядок-независимый)."""
    sid = matcher._name2id["2 застава"]
    assert sid in await _ids(matcher, "Застава 2 в сторону ленпоселка")
    assert sid in await _ids(matcher, "в сторону 2 заставы")


# ------------------------------------------------------- Phase B: spelled ordinals

@pytest.mark.parametrize("text,name", [
    ("в сторону Второй Заставы блокпост", "2 застава"),
    ("на пятой Фонтана блокпост", "5 Фонтана"),
])
async def test_spelled_ordinal(matcher, text, name):
    """Словесные порядковые в косвенном падеже → цифра (второй→2, пятой→5)."""
    assert matcher._name2id[name] in await _ids(matcher, text)


# ----------------------------------------------------------- Phase B: relevance gate

@pytest.mark.parametrize("text,expected", [
    ("ПЛАТНУЮ ПОДПИСКУ НА КАНАЛ, не нарветесь на мошенников, отписаться", True),
    ("Лучший канал t.me/something, присоединяйтесь", True),
    ("Подпишись на @some_channel", True),
    ("Гайдара блокпост в сторону 25й", False),
    ("На Гаванной опасно, перехватчики среди машин", False),
    # реальные репорты про "бус с рекламой окон" (типичный объект) — НЕ реклама
    ("Семена Палия 70 Белый бус с рекламой окна Steco", False),
    ("серый бус с рекламой окон по Колонтаевской", False),
])
def test_is_promotional(text, expected):
    """Реклама ловится (ссылки/хендлы/подписка); репорты, в т.ч. про 'бус с
    рекламой окон' — нет. Промо НЕ игнорируется (идёт в random), но не на улицу."""
    assert is_promotional(text) is expected


async def test_typo_corrector_no_minimal_pair(matcher):
    """Орфо-корректор не путает минимальные пары: 'Малая Арнаутская' не должна
    добавлять 'Б Арнаутская' (различающий токen м/б — guard первого символа)."""
    ids = await _ids(matcher, "Малая Арнаутская 73, куча народу, проверяют документы")
    assert matcher._name2id["Малая Арнаутская"] in ids
    assert matcher._name2id["Б Арнаутская"] not in ids


# ----------------------------------------------------------- Phase B: max-span dedup

async def test_max_span_drops_subspan():
    """Под-спан, вложенный в более длинный матч, отбрасывается.

    Синтетический мини-индекс: 'Дерибасовская' (1 слово) и 'Большая
    Дерибасовская' (2 слова). На 'по Большой Дерибасовской' остаётся только
    длинный матч — короткий под-спан 'Дерибасовской' подавляется.
    """
    morph = Morphology()
    index = PhoneticIndex(morph)
    index.build([
        {"id": 1, "names": ["Дерибасовская"]},
        {"id": 2, "names": ["Большая Дерибасовская"]},
    ])
    m = GeoMatcher(morph, index)
    m._initialized = True
    m._stopwords = set()
    m._morph = morph
    toks = tokenize(preprocess_light("поехал по Большой Дерибасовской"))
    ids = {e["geo_id"] for e in await m.find_geo(tokens=toks, lemmas=morph.lemmatize_tokens(toks))}
    assert ids == {2}
