"""Morphology — централизованная работа с mawo_pymorphy3.

Один MorphAnalyzer на процесс (DAWG-словарь ~15-20 МБ RAM, инициализация
неэкономная). Используется geo_matcher для лемматизации alias-индекса и
n-грамм, layer_classifier для лемматизации ключевых слов и токенов сообщения.

`Lemma` dataclass — единая единица между токенизацией и финальной
обработкой (matcher, classifier). Содержит исходную форму, нормальную форму
и POS-теги (включая распознавание имён собственных через pymorphy3 Geox/Name/Surn).

`ORDINAL_MAP` — порядковые числительные в нормальной форме → арабская цифра.
Покрывает станции Фонтана (1-16) и Люстдорфской (1-10) с запасом до 20.
Конвертирует "пятый" → "5", чтобы "на пятой Фонтана" находило alias "5 ст Фонтана".
"""

import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterable, List, Optional, Protocol

import mawo_pymorphy3 as pymorphy3
import snowballstemmer

# Цифра + короткий буквенный суффикс без разделителя: "5я", "25й", "25ой",
# "10ст"(анция) → нормализуем к самой цифре, чтобы "25й/25-я/25 Чапаевская"
# и "10ст Фонтана/10 Фонтана" сводились к общему ключу. До 2 букв, чтобы не
# зацепить настоящие слова.
_DIGIT_ORDINAL_RE = re.compile(r'^(\d+)[а-яё]{1,2}$', re.IGNORECASE)


ORDINAL_MAP = {
    'первый': '1',
    'второй': '2',
    'третий': '3',
    'четвёртый': '4', 'четвертый': '4',
    'пятый': '5',
    'шестой': '6',
    'седьмой': '7',
    'восьмой': '8',
    'девятый': '9',
    'десятый': '10',
    'одиннадцатый': '11',
    'двенадцатый': '12',
    'тринадцатый': '13',
    'четырнадцатый': '14',
    'пятнадцатый': '15',
    'шестнадцатый': '16',
    'семнадцатый': '17',
    'восемнадцатый': '18',
    'девятнадцатый': '19',
    'двадцатый': '20',
    'двадцать первый': '21',
    'двадцать второй': '22',
    'двадцать третий': '23',
    'двадцать четвёртый': '24', 'двадцать четвертый': '24',
    'двадцать пятый': '25',
    'двадцать шестой': '26',
    'двадцать седьмой': '27',
    'двадцать восьмой': '28',
    'двадцать девятый': '29',
    'тридцатый': '30',
}

# Грамматические теги pymorphy3, указывающие на имя собственное / топоним.
_PROPER_NOUN_TAGS = frozenset({'Name', 'Surn', 'Patr', 'Geox', 'Orgn'})

# POS-теги, которые pymorphy3 УВЕРЕННО опознаёт как НЕ-топоним (TASK 1):
# VERB, INFN, ADVB, INTJ, PRCL, CONJ, PRED, COMP, GRND — исключаются из матчинга.
# СОХРАНЯЮТСЯ: NOUN, ADJF, ADJS, NUMR, PRTF, PRTS, NPRO + всё uncertain (UNKN,
# пустой POS) — pymorphy3 ошибается на OOV-проперах (Гаванная→GRND, героив→GRND),
# поэтому «подозрительный» тег сам по себе не выбрасывает токен: geo_matcher
# дополнительно защищает окна, чьи стемы присутствуют в индексе (stem-rescue).
_NON_GEO_POS = frozenset({
    'VERB', 'INFN', 'ADVB', 'INTJ', 'PRCL', 'CONJ', 'PRED', 'COMP', 'GRND',
})


@dataclass
class Lemma:
    """Лемма с грамматической разметкой."""
    surface: str         # исходная словоформа
    normal_form: str     # нормальная форма (или цифра для порядкового числительного)
    pos: str             # NOUN, ADJF, VERB, PREP, ...
    is_proper: bool      # имя собственное / топоним
    is_prep: bool = False  # предлог (хотя бы один разбор PREP) — граница окна


class _HasText(Protocol):
    """Утиная типизация: любой объект с .text — Token из word_tokenizer или эквивалент."""
    text: str


class Morphology:
    """Обёртка над mawo_pymorphy3 с распознаванием порядковых числительных."""

    # Размер LRU-кэша лемматизации. Слова с message-частотой ~15 уник./сообщ.
    # → ~150 сообщений/sec при ширине стрима → кэш-hit ~80% на повторах
    # топонимов и common-words. Увеличено до 20K для high-throughput сценариев.
    # ~20K записей × ~100 bytes = ~2MB RAM.
    _LEMMA_CACHE_MAX = 20000
    # Фраз обычно меньше (~1000 алиасов × несколько вариантов), но lemma_for_phrase
    # вызывается в _build_alias_index при каждом reindex_all → выигрыш ощутим.
    _PHRASE_CACHE_MAX = 2000
    # Кэш стемминга — отдельный от лемм (Snowball дешевле pymorphy, но кэш
    # снимает повторную работу на потоке однотипных топонимов).
    # Увеличено до 20K для улучшения hit-rate при высокой нагрузке.
    _STEM_CACHE_MAX = 20000

    def __init__(self) -> None:
        """Инициализация pymorphy3 анализатора и Snowball-стеммера."""
        self._morph = pymorphy3.MorphAnalyzer()
        # Snowball (русский) — суффиксный стеммер. В ОТЛИЧИЕ от pymorphy он
        # OOV-устойчив: имена улиц — несловарные пропера, и угадыватель pymorphy
        # на них врёт ("Гаванная"→"гаваннать"), а стеммер даёт стабильный стем
        # ("гаванная"/"гаванной"→"гава"). Поэтому матч улиц строится на стемах,
        # а pymorphy остаётся для слоёв и порядковых числительных (там слова
        # словарные и анализ надёжен).
        self._stemmer = snowballstemmer.stemmer('russian')
        # Стемы порядковых → цифра. Snowball сводит "первый/первой/первого"→"перв",
        # поэтому стем нивелирует падеж/род, а pymorphy на словесных порядковых
        # непоследователен ("второй" не тегается Anum). Однословные ключи —
        # многословные ("двадцать первый") стеммятся как фраза некорректно.
        self._ordinal_stems = {
            self._stemmer.stemWord(k): v
            for k, v in ORDINAL_MAP.items() if ' ' not in k
        }
        # OrderedDict как LRU: O(1) вытеснение через popitem(last=False),
        # O(1) обновление позиции через move_to_end. Кэшируем по нижнему
        # регистру — pymorphy3 не различает Малой/малой/МАЛОЙ.
        self._lemma_cache: "OrderedDict[str, Lemma]" = OrderedDict()
        self._phrase_cache: "OrderedDict[str, str]" = OrderedDict()
        self._stem_cache: "OrderedDict[str, str]" = OrderedDict()

    @property
    def analyzer(self):
        """Сырой MorphAnalyzer (для legacy потребителей вроде layer_classifier)."""
        return self._morph

    def lemmatize_word(self, word: str) -> Lemma:
        """Леммa слова. Цифры возвращаются как есть; порядковые → арабские.

        LRU-кэш: повторные слова возвращаются мгновенно без вызова pymorphy3
        (который ~50µs/слово). На реальном корпусе hit-rate ~70-85%.
        """
        if not word:
            return Lemma('', '', '', False)

        # Cache lookup. Кэшируем по lowercase ключу.
        key = word.lower()
        cached = self._lemma_cache.get(key)
        if cached is not None:
            self._lemma_cache.move_to_end(key)
            # Surface берём от исходного слова — регистр может отличаться
            return Lemma(word, cached.normal_form, cached.pos,
                         cached.is_proper, cached.is_prep)

        if word.isdigit():
            result = Lemma(word, word, 'NUMR', False, False)
            self._cache_store(key, result)
            return result

        m = _DIGIT_ORDINAL_RE.match(word)
        if m:
            result = Lemma(word, m.group(1), 'NUMR', False, False)
            self._cache_store(key, result)
            return result

        parses = self._morph.parse(word)
        if not parses:
            result = Lemma(word, key, '', False, False)
            self._cache_store(key, result)
            return result

        best = parses[0]
        pos = str(best.tag.POS) if best.tag.POS else ''
        normal = best.normal_form

        # Порядковое числительное любого рода/падежа/числа → арабская цифра
        if 'Anum' in best.tag:
            digit = ORDINAL_MAP.get(normal)
            if digit:
                result = Lemma(word, digit, 'NUMR', False, False)
                self._cache_store(key, result)
                return result

        is_proper = any(tag in best.tag for tag in _PROPER_NOUN_TAGS)
        # Предлог (TASK 1): PREP среди ЛЮБЫХ разборов — надёжный маркер («в», «с»,
        # «по» имеют омонимичные NOUN-разборы как аббревиатуры, но PREP-разбор
        # всегда присутствует). Используется как граница скользящего окна.
        is_prep = any(
            p.tag.POS is not None and str(p.tag.POS) == 'PREP' for p in parses
        )
        result = Lemma(word, normal, pos, is_proper, is_prep)
        self._cache_store(key, result)
        return result

    def _cache_store(self, key: str, lemma: Lemma) -> None:
        """LRU-вставка с вытеснением при превышении лимита."""
        self._lemma_cache[key] = lemma
        while len(self._lemma_cache) > self._LEMMA_CACHE_MAX:
            self._lemma_cache.popitem(last=False)

    def lemmatize_tokens(self, tokens: Iterable[_HasText]) -> List[Lemma]:
        """Лемматизирует последовательность токенов (объекты с .text)."""
        return [self.lemmatize_word(t.text) for t in tokens]

    def is_preposition(self, word: str) -> bool:
        """Является ли слово предлогом (TASK 1: граница скользящего окна).

        Надёжно и для омографов: «в»/«с»/«по» имеют омонимичные NOUN-разборы
        (аббревиатуры), но PREP-разбор присутствует всегда. Кэш — через
        lemmatize_word (LRU), отдельного кэша не нужно.
        """
        if not word:
            return False
        return self.lemmatize_word(word).is_prep

    @staticmethod
    def is_geo_candidate(lemma: Lemma) -> bool:
        """Может ли лемма быть частью топонима (TASK 1: POS-фильтрация).

        Исключаются только токены, которые pymorphy3 уверенно опознал как
        не-топоним (VERB/INFN/ADVB/INTJ/PRCL/CONJ/PRED/COMP/GRND).
        UNKN/пустой POS (OOV-пропера) и разрешённые теги (NOUN, ADJF, ADJS,
        NUMR, PRTF, PRTS, NPRO) — кандидаты. Предлоги отсекаются отдельно
        (is_prep) как границы окна, а не как «не-кандидаты».
        """
        return lemma.pos not in _NON_GEO_POS

    def shrink_cache(self, max_size: int = 5000) -> None:
        """Урезание LRU-кэшей до max_size записей (R-PR4 memory fallback)."""
        caps = (
            (self._lemma_cache, max_size),
            (self._phrase_cache, min(max_size, self._PHRASE_CACHE_MAX)),
            (self._stem_cache, max_size),
        )
        for cache, cap in caps:
            while len(cache) > cap:
                cache.popitem(last=False)

    def cache_size(self) -> int:
        """Суммарный размер всех LRU-кэшей (для heartbeat)."""
        return len(self._lemma_cache) + len(self._phrase_cache) + len(self._stem_cache)

    def lemmatize_words(self, words: Iterable[str]) -> List[Lemma]:
        """Лемматизирует последовательность строк."""
        return [self.lemmatize_word(w) for w in words if w]

    def lemma_for_phrase(self, text: str) -> str:
        """Single-shot лемматизация фразы (split → лемма каждого → join).

        Используется geo_matcher для канонизации alias-имени в индексе,
        когда отдельная токенизация избыточна (alias уже чистый, без пунктуации).

        Phrase-level LRU cache (2000): reindex_all обрабатывает ~1000 алиасов;
        при reload без cache каждый раз заново лемматизируется. С кешем —
        instant hit на повторных вызовах.
        """
        if not text:
            return ''

        cached = self._phrase_cache.get(text)
        if cached is not None:
            self._phrase_cache.move_to_end(text)
            return cached

        result = ' '.join(
            self.lemmatize_word(w).normal_form
            for w in text.split() if w
        )
        self._phrase_cache[text] = result
        while len(self._phrase_cache) > self._PHRASE_CACHE_MAX:
            self._phrase_cache.popitem(last=False)
        return result

    # ------------------------------------------------------------------ stemming

    def stem(self, word: str) -> str:
        """Match-ключ токена: Snowball-стем, OOV-устойчивый.

        Цифры и порядковые числительные → арабская цифра (как в lemmatize_word),
        чтобы "5 Фонтана" / "пятой фонтана" / "5я фонтана" сводились к общему
        ключу "5". Всё остальное — суффиксный стем (одинаковый для всех падежей
        одного имени: гаванная/гаванной/гаванную → "гава"). LRU-кэш.
        """
        if not word:
            return ''

        key = word.lower()
        cached = self._stem_cache.get(key)
        if cached is not None:
            self._stem_cache.move_to_end(key)
            return cached

        # Цифры/порядковые проходят через надёжную pymorphy-ветку lemmatize_word
        # (она уже маппит "5я"→"5", "10"→"10", часть порядковых через Anum-тег).
        lemma = self.lemmatize_word(word)
        if lemma.pos == 'NUMR' and lemma.normal_form.isdigit():
            result = lemma.normal_form
        else:
            snow = self._stemmer.stemWord(key)
            # Словесные порядковые в любом падеже/роде ("второй/пятой/десятого")
            # → цифра через стем-карту, чтобы "Второй Заставы"/"пятой Фонтана"
            # сводились к "2 застава"/"5 фонтан".
            result = self._ordinal_stems.get(snow, snow)

        self._stem_cache[key] = result
        while len(self._stem_cache) > self._STEM_CACHE_MAX:
            self._stem_cache.popitem(last=False)
        return result

    def stem_tokens(self, tokens: Iterable[_HasText]) -> List[str]:
        """Стеммировать последовательность токенов (объекты с .text)."""
        return [self.stem(t.text) for t in tokens]
