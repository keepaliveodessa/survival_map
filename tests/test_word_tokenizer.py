"""Tests for nlp_processor/word_tokenizer.tokenize (pure, no heavy deps)."""
from conftest import load_module_by_path

wt = load_module_by_path("_wt_under_test", "nlp_processor/word_tokenizer.py")
tokenize = wt.tokenize


def texts(toks):
    return [t.text for t in toks]


def test_empty_and_blank():
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_basic_split_and_positions():
    s = "Дерибасовская улица"
    toks = tokenize(s)
    assert texts(toks) == ["Дерибасовская", "улица"]
    # non-fused tokens map back to the source span exactly
    for t in toks:
        assert s[t.start:t.stop] == t.text


def test_hyphen_separates_two_streets():
    assert texts(tokenize("Градоначальницкая-Олейника")) == [
        "Градоначальницкая",
        "Олейника",
    ]


def test_punctuation_and_slash_are_separators():
    assert texts(tokenize("ДТП, на/у (Пушкинской)!")) == [
        "ДТП",
        "на",
        "у",
        "Пушкинской",
    ]


def test_digit_ya_fusion():
    assert texts(tokenize("5 я Люстдорфская")) == ["5я", "Люстдорфская"]
    assert texts(tokenize("7-я")) == ["7я"]
    assert texts(tokenize("1-я станция")) == ["1я", "станция"]


def test_ordinal_suffix_fusions():
    assert texts(tokenize("7го км")) == ["7го", "км"]
    assert texts(tokenize("7-го км")) == ["7го", "км"]
    assert texts(tokenize("5-й станции")) == ["5й", "станции"]
    assert texts(tokenize("3-е Фонтана")) == ["3е", "Фонтана"]
    assert texts(tokenize("10-м Апреля")) == ["10м", "Апреля"]


def test_plain_digit_followed_by_word_not_fused():
    assert texts(tokenize("10 минут")) == ["10", "минут"]
    assert texts(tokenize("7 станция")) == ["7", "станция"]
    assert texts(tokenize("11 ст Фонтана")) == ["11", "ст", "Фонтана"]


def test_digits_kept_inside_words():
    assert texts(tokenize("h1 патруль")) == ["h1", "патруль"]
