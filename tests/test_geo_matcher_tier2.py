"""Tests for GeoMatcher Tier-2 fixes: batch fuzzy, len-guard, NOISE-gap."""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_geo_matcher():
    """Загрузка geo_matcher с относительными импортами через стабы пакета.

    Если настоящий processor.morphology уже импортирован другим тестом —
    импортируем настоящий geo_matcher напрямую: подмена morph.Lemma = object
    на живом модуле глобально ломала Lemma(...) в последующих тестах
    (порядкозависимые TypeError: object() takes no parameters).

    Тяжёлые зависимости (pymorphy3 и т.п.) не нужны — Tier-2 функции тестируем
    изолированно; morphology/phonetic_index подставляются стабами (fallback,
    когда processor ещё не импортирован и стабы безопасны). После загрузки
    sys.modules восстанавливается, чтобы не задеть другие тесты.
    """
    if "processor.morphology" in sys.modules:
        try:
            import processor.geo_matcher as real_mod
            return real_mod
        except ImportError:
            pass  # тяжёлые зависимости недоступны — стаб-путь ниже

    names = ("processor", "processor.morphology", "processor.phonetic_index",
             "processor.word_tokenizer", "processor.geo_matcher")
    saved = {n: sys.modules.get(n) for n in names}

    pkg = types.ModuleType("processor")
    pkg.__path__ = [str(ROOT / "processor")]
    sys.modules.setdefault("processor", pkg)
    for name in ("processor.morphology", "processor.phonetic_index",
                 "processor.word_tokenizer"):
        sys.modules.setdefault(name, types.ModuleType(name))
    morph = sys.modules["processor.morphology"]
    morph.Lemma = object
    morph.Morphology = object
    idxmod = sys.modules["processor.phonetic_index"]
    idxmod.PhoneticIndex = object
    wtmod = sys.modules["processor.word_tokenizer"]
    wtmod.Token = _load_token()

    spec = importlib.util.spec_from_file_location(
        "processor.geo_matcher", ROOT / "processor" / "geo_matcher.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["processor.geo_matcher"] = mod
    spec.loader.exec_module(mod)

    for name, prev in saved.items():
        if prev is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev
    return mod


def _load_token():
    spec = importlib.util.spec_from_file_location(
        "_tok_under_test", ROOT / "processor" / "word_tokenizer.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_tok_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod.Token


gm = _load_geo_matcher()


def test_batch_fuzzy_match_returns_all_matches():
    phrases = ["туристская", "балковская", "старопортофранковская"]
    res = gm._batch_fuzzy_match(
        ["туриская", "балковснкая"], phrases, threshold=80.0
    )
    assert set(res) == {"туриская", "балковснкая"}
    assert res["туриская"][0] == "туристская"
    assert res["балковснкая"][0] == "балковская"


def test_batch_fuzzy_match_single_query():
    res = gm._batch_fuzzy_match(["киевский рынок"], ["киевский рынок"], 80.0)
    assert res["киевский рынок"][0] == "киевский рынок"


def test_batch_fuzzy_match_no_hits():
    res = gm._batch_fuzzy_match(["валерий самофалов"], ["туристская"], 90.0)
    assert res == {}


def test_typo_len_guard_long_names():
    assert gm._typo_len_guard("туристическая") == 3   # 13 chars → 25% = 3
    assert gm._typo_len_guard("балковская") == 3      # 10 chars → int(2.5) = 2 → max(3, 2) = 3


def test_typo_len_guard_short_names():
    assert gm._typo_len_guard("кирова") == 2          # 6 chars → max(2, 1) = 2
    assert gm._typo_len_guard("пушкинская") == 3      # 10 chars → 3


class _StubIndex:
    def has_stem(self, stem):
        return False

    def has_stem_anywhere(self, stem):
        return stem.isdigit()


def _tok(text):
    return gm.Token(text, 0, len(text))


def test_noise_gap_skips_ст_in_window():
    m = gm.GeoMatcher.__new__(gm.GeoMatcher)
    m._index = _StubIndex()
    toks = [_tok("11"), _tok("ст"), _tok("фонтана")]
    cands = m._candidates_sliding_window(toks, ["11", "ст", "фонта"])
    surfaces = [c[0] for c in cands]
    keys = [c[1] for c in cands]
    assert "11 фонтана" in surfaces
    assert ("11", "фонта") in keys
    assert not any("ст" in c[0].split() for c in cands)


def test_noise_gap_keeps_ordinary_words():
    m = gm.GeoMatcher.__new__(gm.GeoMatcher)
    m._index = _StubIndex()
    toks = [_tok("бригадная"), _tok("улица")]
    cands = m._candidates_sliding_window(toks, ["бригадн", "улиц"])
    keys = [c[1] for c in cands]
    assert ("бригадн", "улиц") in keys