"""Регрессионные тесты приёмочных кейсов (экспорт 009events) — TASK 1-3.

Проверяет кейсы из боевого экспорта, которые легли в основу рефакторинга
NLP-пайплайна (морфологический POS-фильтр, сегментация окна по предлогам,
longest-match-first, Tier-1 length-floor, scatter 500м):

  1. «Крыжановка, по Гонтаренко... повернул на Ветеранов» → Крыжановка + Ветеранов,
     «Гонтаренко» (нет в базе) не ломает матчер;
  2. «Фонтанка 3,ездит белый бус» → «Фонтанка» как отдельный объект, «3» в окне
     не ломает матчинг;
  3. «С Академическая в сторону французского» → «Академическая» + «Французский»
     (род. падеж), оба с score >= candidate_min_score;
  4. «Мотто на Головатого блокпост» → «Атамана Головатого» (усечённая форма);
  5. «Героев Пограничников 2» / «героив прикордонникив 2» → украинская
     нормализация + падежи через Snowball-стемы.

Плюс юнит-тесты новых примитивов Morphology (is_prep / is_geo_candidate /
shrink_cache) и guard-тесты: окно не склеивает токены через предлог.

Тесты SKIP-аются без тяжёлых зависимостей (mawo_pymorphy3/rapidfuzz/
snowballstemmer) — как test_street_matcher.py.
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
csv.field_size_limit(10_000_000)

if "parser" not in sys.modules:
    _pkg = types.ModuleType("parser")
    _pkg.__path__ = [str(ROOT / "parser")]
    sys.modules["parser"] = _pkg

if "nlp_processor" not in sys.modules:
    _pkg = types.ModuleType("nlp_processor")
    _pkg.__path__ = [str(ROOT / "nlp_processor")]
    sys.modules["nlp_processor"] = _pkg

from nlp_processor.morphology import Lemma, Morphology       # noqa: E402
from nlp_processor.phonetic_index import PhoneticIndex       # noqa: E402
from nlp_processor.geo_matcher import GeoMatcher             # noqa: E402
from nlp_processor.word_tokenizer import tokenize            # noqa: E402
from common.text_preprocessor import preprocess_light, strip_tail  # noqa: E402
from common.settings import settings                     # noqa: E402


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


@pytest.fixture(scope="module")
def matcher():
    morph = Morphology()
    index = PhoneticIndex(morph)
    rows, name2id = _load_geo()
    index.build(rows)
    m = GeoMatcher(morph, index)
    m._initialized = True
    m._stopwords = set()
    m._name2id = name2id  # convenience for tests
    return m


async def _find(matcher, text):
    pre = preprocess_light(strip_tail(text or ""))
    toks = tokenize(pre)
    lemmas = matcher._morph.lemmatize_tokens(toks)
    return await matcher.find_geo(tokens=toks, lemmas=lemmas)


# ------------------------------------------------------- приёмочные кейсы 1-5

async def test_case1_kryzhanovka_veterany(matcher):
    """Кейс 1: оба топонима находятся; «Гонтаренко» (OOV) не ломает матчер."""
    ents = await _find(matcher, "Крыжановка, по Гонтаренко... повернул на Ветеранов")
    ids = {e["geo_id"] for e in ents}
    assert matcher._name2id["Крыжановка"] in ids
    assert matcher._name2id["Ветеранов"] in ids


async def test_case2_fontanka_separate_object(matcher):
    """Кейс 2: «Фонтанка» матчится; цифра «3» не склеивает мусорное окно."""
    ents = await _find(matcher, "Фонтанка 3,ездит белый бус")
    names = {e["matched_name"] for e in ents}
    assert "Фонтанка" in names
    # Число «3» не должно порождать ложных объектов
    assert all(e["text"] != "3" for e in ents)


async def test_case3_french_genitive_above_threshold(matcher):
    """Кейс 3: «французского» (род. падеж) → «Французский» с score >= 0.80.

    Регрессия: раньше Tier-1 ставил 0.78 (fuzz по surface штрафует падеж),
    и кандидат отбрасывался SQL-порогом candidate_min_score=0.80.
    """
    ents = await _find(matcher, "С Академическая в сторону французского")
    by_name = {e["matched_name"]: e for e in ents}
    assert "Академическая" in by_name
    assert "Французский" in by_name
    assert by_name["Французский"]["score"] >= settings.geo.candidate_min_score


async def test_case4_headmaster_truncated_form(matcher):
    """Кейс 4: «на Головатого» → «Атамана Головатого» (усечённая форма)."""
    ents = await _find(matcher, "Мотто на Головатого блокпост")
    assert any(e["matched_name"] == "Атамана Головатого" for e in ents)


@pytest.mark.parametrize("text", [
    "Героев Пограничников 2",
    "героив прикордонникив 2, катаеться бус с ухилянтами",
])
async def test_case5_ukrainian_normalization(matcher, text):
    """Кейс 5: укр. «героив прикордонникив» ≡ «Героев Пограничников» (стемы)."""
    ents = await _find(matcher, text)
    assert matcher._name2id["Героев Пограничников"] in {e["geo_id"] for e in ents}


# ------------------------------------------------- TASK 1: предлоги и POS-фильтр

def _candidates(matcher, text):
    pre = preprocess_light(strip_tail(text))
    toks = tokenize(pre)
    lemmas = matcher._morph.lemmatize_tokens(toks)
    clean = matcher._strip_noise(toks, lemmas)
    stems = matcher._morph.stem_tokens(clean[0])
    return matcher._candidates_sliding_window(clean[0], stems, clean[1]), lemmas


def test_window_never_spans_preposition(matcher):
    """Окно не склеивает токены через предлог («повернул на Ветеранов»)."""
    cands, _ = _candidates(matcher, "повернул на Ветеранов и поехал")
    surfaces = [c[0] for c in cands]
    assert not any("на" in s.split() for s in surfaces), surfaces


def test_window_segments_split_by_prepositions(matcher):
    """«С Академическая в сторону» → сегменты разделены; нет окна
    «академическая в сторону» через предлог."""
    cands, _ = _candidates(matcher, "С Академическая в сторону французского")
    surfaces = [c[0] for c in cands]
    assert "академическая" in surfaces
    assert "сторону французского" in surfaces
    assert not any("академическая в" in s for s in surfaces), surfaces


def test_pos_filter_blocks_verb_window(matcher):
    """Окно целиком из уверенных глаголов отбрасывается (stem-rescue не нужен)."""
    cands, _ = _candidates(matcher, "ехав ехал ехала мимо")
    surfaces = [c[0] for c in cands]
    assert not any("ехав ехал" in s for s in surfaces), surfaces


# -------------------------------------- Morphology: новые примитивы (TASK 1)

def test_is_prep_homographs():
    """Омографы «в/с/по/к» распознаются как предлоги (PREP среди разборов)."""
    m = Morphology()
    for w in ("на", "в", "с", "по", "к", "до", "от", "у", "возле", "около"):
        assert m.is_preposition(w), w
    for w in ("Фонтанка", "Ветеранов", "блокпост", "поехали"):
        assert not m.is_preposition(w), w


def test_is_geo_candidate_pos_classes():
    """Исключения/сохранения TASK 1: VERB/GRND — нет; NOUN/ADJF/NUMR/UNKN — да."""
    m = Morphology()
    assert not m.is_geo_candidate(m.lemmatize_word("поехали"))    # VERB
    assert not m.is_geo_candidate(m.lemmatize_word("гаванная"))   # GRND (OOV-тег)
    assert not m.is_geo_candidate(m.lemmatize_word("не"))         # PRCL
    assert m.is_geo_candidate(m.lemmatize_word("Фонтанка"))       # NOUN
    assert m.is_geo_candidate(m.lemmatize_word("французского"))   # ADJF
    assert m.is_geo_candidate(m.lemmatize_word("гонтаренко"))     # UNKN (OOV)
    assert m.is_geo_candidate(m.lemmatize_word("3"))              # NUMR


def test_lemma_backward_compatible():
    """Lemma совместим со старым позиционным конструктором (is_prep по умолчанию)."""
    lm = Lemma("тест", "тест", "NOUN", False)
    assert lm.is_prep is False


def test_shrink_cache_and_size():
    """R-PR4: shrink_cache урезает кэши; cache_size суммирует (для heartbeat)."""
    m = Morphology()
    for w in ("поехали", "Фонтанка", "Ветеранов", "блокпост", "гаванная"):
        m.lemmatize_word(w)
        m.stem(w)
    assert m.cache_size() > 0
    m.shrink_cache(max_size=2)
    assert m.cache_size() <= 6  # 3 кэша × cap 2


# ------------------------------------------------ TASK 3: настройки порогов

def test_scatter_threshold_is_500m():
    """Hard Constraint 5: scatter-порог weighted_centroid = 500м."""
    assert settings.geo.weighted_centroid_max_scatter_m == 500.0


def test_pos_filter_enabled_by_default():
    """TASK 1: безопасный POS-фильтр (со stem-rescue) включён по умолчанию."""
    assert settings.similarity.enable_pos_filter is True
