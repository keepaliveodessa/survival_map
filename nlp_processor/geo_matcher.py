"""GeoMatcher — поиск по единому справочнику geo (улицы + нас.пункты + POI).

Два тира:
  Tier 1 [Stem exact] — точное совпадение кортежа стемов из PhoneticIndex.
  Tier 2 [Surface typo] — rapidfuzz по сырым алиасам как корректор опечаток.

Порядок приоритета типов: settlement (village/town) > street > остальные.
"""

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from rapidfuzz import fuzz
from rapidfuzz import process as rf_process

from .morphology import Lemma, Morphology
from .phonetic_index import PhoneticIndex
from .word_tokenizer import Token, tokenize
from common.text_preprocessor import clean

if TYPE_CHECKING:
    from .phonetic_index import PhoneticEntry

try:
    from common.settings import settings
except Exception:
    settings = None

try:
    from common.metrics import geo_match_tier_total
except Exception:
    geo_match_tier_total = None

logger = logging.getLogger(__name__)

Candidate = Tuple[str, Tuple[str, ...], int, int, int, bool, bool]


def _fuzzy_match(query: str, phrases: list, threshold: float):
    """Нечёткий поиск по списку фраз с пороговым значением схожести."""
    try:
        return rf_process.extractOne(
            query, phrases, scorer=fuzz.WRatio, score_cutoff=threshold,
        )
    except Exception:
        return None


def _batch_fuzzy_match(queries: list, phrases: list, threshold: float,
                       max_queries: int = 20):
    """Batch fuzzy matching: один IPC на всё сообщение.

    rapidfuzz.process.extract(queries=list, ...) в 3.9.x молча возвращает
    пустой список (баг версии), поэтому каждый запрос матчим отдельным
    extractOne — иначе Tier 2 мёртв на проде (ProcessPoolExecutor).

    max_queries: лимит кандидатов для Tier 2 — длинные сообщения с 50+
    токенами создают O(n*m) взрыв, блокируя процессор на минуты.
    """
    results = {}
    for q in queries[:max_queries]:
        try:
            match = rf_process.extractOne(
                q, phrases, scorer=fuzz.WRatio, score_cutoff=threshold,
            )
            if match:
                results[q] = match
        except Exception as e:
            logger.warning(f"Batch fuzzy match failed for {q!r}: {e}")
    return results


def _typo_len_guard(surface: str) -> int:
    """Допустимая разница длин surface↔кандидат для Tier 2 (орфо-корректор).

    Старый max(2, 20%) резал реальные опечатки длинных имён
    («Туристическая»→«Туристская», diff=3). Для поверхностей >=10 символов
    допуск расширен до max(3, 25%); короткие остаются на max(2, 20%).
    """
    if len(surface) >= 10:
        return max(3, int(0.25 * len(surface)))
    return max(2, int(0.2 * len(surface)))


def _typo_threshold_percent() -> float:
    """Tier-2 порог в процентах (0-100) из settings; fallback 90.0.

    Единая точка для _link_span и find_geo — раньше выражение с одним и тем
    же fallback дублировалось в обоих путях.
    """
    if (settings and settings.similarity
            and getattr(settings.similarity, 'surface_typo_threshold', None) is not None):
        return settings.similarity.surface_typo_threshold * 100
    return 90.0

_LOC_PREPS: frozenset = frozenset({
    'на', 'по', 'в', 'у', 'до',
    'від', 'біля',
    'около', 'возле', 'вдоль',
})

# Шумовые служебные токены («ст.», «ул.», «г.» + суффиксы порядковых).
# Пропускаемые слайдинг-окном: «11 ст. Фонтана» → ключ (11, фонтана).
#
# REG-фикс (events_export, «Новая дорога стали менты…» → random): описательные
# прилагательные (большой/малый/старый/новый + падежи) УДАЛЕНЫ из шума — они
# являются частью официальных имён объектов («Новая дорога», «Малая Арнаутская»,
# «Большая Дерибасовская»), и вырезание ломало Tier 1: окно «новая дорога» не
# генерировалось вовсе, а Tier 2 по хвосту «дорога» резался prefix-guard'ом
# (д ≠ н). Побочный эффект (мусорное окно «большого Фонтана») безопасен:
# Tier 1 мимо, Tier 2 не проходит prefix-guard/fuzzy-порог, а «Фонтана»
# матчится соседним окном как раньше.
_NOISE_TOKENS: frozenset = frozenset({
    'ст', 'ул', 'вул', 'пр', 'пер', 'ш', 'им', 'г', 'го', 'й', 'ій', 'йй',
    'первого', 'второго', 'третьего',
})

# Родовые head-слова гео-имён: часто встречаются в тексте сами по себе
# («дорога на 7-й», «улица перекрыта») и НЕ должны rescu-иться subset-матчем
# до полного имени («южная дорога», «Улица Толбухина»). Сравнение — по stem,
# но все формы здесь в именительном падеже, а surface уже lowercase.
_GENERIC_HEAD_WORDS: frozenset = frozenset({
    'улица', 'улицю', 'дорога', 'дорогу', 'дороги',
    'площадь', 'площадью', 'парк', 'парка', 'сквер', 'сквера',
    'мост', 'моста', 'переулок', 'переулка', 'проспект', 'проспекта',
    'рынок', 'рынка', 'вокзал', 'вокзала', 'станция', 'станции',
    'бульвар', 'бульвара', 'набережная', 'застава', 'заставы',
})

# Типы в порядке приоритета: settlement выше street
class GeoMatcher:
    """Поиск по geo таблице: кандидаты → geo_id через surface/lemma индекс."""

    def __init__(self, morph: Morphology, index: PhoneticIndex) -> None:
        """Инициализация матчера с морфологией и фонетическим индексом."""
        self._morph = morph
        self._index = index
        self._initialized = False
        self._stopwords: Set[str] = set()
        # geo_id → type (street/village/town/...): пробрасывается в кандидатов,
        # чтобы pre-filter (midpoint/type_hint) мог работать.
        self._geo_types: Dict[int, str] = {}
        self._executor: Optional[ProcessPoolExecutor] = None

    async def initialize(self, pg_pool) -> bool:
        """Загрузка geo-данных, построение индекса, инициализация стоп-слов."""
        try:
            async with pg_pool.acquire() as conn:
                geo_rows = await conn.fetch(
                    "SELECT id, names, type FROM geo WHERE geom IS NOT NULL"
                )
                sw_rows = await conn.fetch("SELECT word FROM stopwords")

            await asyncio.to_thread(self._index.build, geo_rows)
            self._geo_types = {row['id']: row['type'] for row in geo_rows if row.get('type')}
            self._stopwords = {row['word'].strip().lower() for row in sw_rows if row['word']}
            self._executor = ProcessPoolExecutor(max_workers=4)
            logger.info(f"[Geo] Loaded {len(self._stopwords)} stopwords, {len(geo_rows)} objects, ProcessPoolExecutor initialized")
            self._initialized = True
            return True
        except Exception as exc:
            logger.error(f"[Geo] Init failed: {exc}")
            return False

    async def reindex_all(self, pg_pool) -> int:
        """Полная перестройка индекса geo-объектов."""
        try:
            async with pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, names, type FROM geo WHERE geom IS NOT NULL"
                )
            self._geo_types = {row['id']: row['type'] for row in rows if row.get('type')}
            count = await asyncio.to_thread(self._index.build, rows)
            logger.info(f"[Geo] Reindexed {count} variants")
            return count
        except Exception as exc:
            logger.error(f"[Geo] reindex_all failed: {exc}")
            return 0

    async def reindex_geo(self, pg_pool, geo_id: int) -> None:
        """Обновление индекса для одного geo-объекта по ID."""
        try:
            async with pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, names, type FROM geo WHERE id = $1 AND geom IS NOT NULL",
                    geo_id,
                )
            row_dict = dict(row) if row else None
            if row_dict:
                self._geo_types[geo_id] = row_dict.get('type') or ''
            else:
                self._geo_types.pop(geo_id, None)  # объект удалён/geom NULL
            await asyncio.to_thread(
                self._index.replace_street, geo_id, row_dict
            )
        except Exception as exc:
            logger.error(f"[Geo] reindex_geo({geo_id}) failed: {exc}")

    async def close(self) -> None:
        """Завершение работы: остановка пула потоков."""
        if self._executor:
            self._executor.shutdown(wait=True)
            logger.info("[Geo] ProcessPoolExecutor shutdown")
        self._executor = None

    def _punctuation_set(self) -> Set[str]:
        """Набор символов пунктуации для фильтрации токенов."""
        if settings and settings.similarity:
            return set(getattr(settings.similarity, 'punctuation_tokens', ()))
        return {'#', '/', ',', '.', '(', ')', '!', '?', '-', '«', '»', '"', ':', ';'}

    def _is_short_settlement(self, entry: "PhoneticEntry") -> bool:
        """Guard Tier 2: короткие settlement-имена (<=6 символов) не матчатся.

        Короткие топонимы («Малое», «Петрово») частотны по всей области —
        distant-спаривание через орфо-корректор даёт пин за десятки км.
        Длинные имена остаются — их анти-list (SQL) отсекает по дистанции.
        """
        return (
            self._geo_types.get(entry.street_id) in ('village', 'town')
            and len(entry.canonical_name or '') <= 6
        )

    def _accept_typo_match(
        self, surface: str, cand: str, idx: int,
        s_meta: List["PhoneticEntry"],
    ) -> Optional["PhoneticEntry"]:
        """Общие guard'ы Tier-2 для _link_span и батч-пути find_geo.

        prefix-guard (первый символ + первые 3 символа), length-guard
        (_typo_len_guard) и short-settlement guard. Возвращает entry индекса,
        если матч принят, иначе None — вызывающий строит свой result-dict
        (у путей разный набор служебных полей).
        """
        if not (surface[0] == cand[0]
                and surface[:3] == cand[:3]
                and abs(len(cand) - len(surface)) <= _typo_len_guard(surface)):
            return None
        entry = s_meta[idx]
        if self._is_short_settlement(entry):
            return None
        return entry

    def _strip_noise(self, tokens: List[Token], lemmas: List[Lemma]) -> Tuple[List[Token], List[Lemma]]:
        """Удаление шумовых (пунктуационных) токенов из последовательности."""
        if len(tokens) != len(lemmas):
            n = min(len(tokens), len(lemmas))
            tokens, lemmas = tokens[:n], lemmas[:n]
        punct = self._punctuation_set()
        keep_t, keep_l = [], []
        for t, l in zip(tokens, lemmas):
            surface = (t.text or '').strip()
            if not surface or all(ch in punct for ch in surface):
                continue
            keep_t.append(t)
            keep_l.append(l)
        return keep_t, keep_l

    # POS-теги pymorphy3, допустимые для гео-кандидатов (TASK 1).
    # Канонический источник истины — morphology._NON_GEO_POS (дубликат здесь:
    # тесты грузят geo_matcher со стабом morphology без этого символа).
    # VERB, INFN, ADVB, INTJ, PRCL, CONJ, PRED, COMP, GRND — исключаются.
    # Отличие от старого набора: PRTF/PRTS СОХРАНЕНЫ (могут быть частью
    # топонима), PREP убран (предлоги вырезаются раньше как границы
    # сегментов, а не как «не-кандидаты»). NPRO/UNKN/пустой POS пропускаем —
    # pymorphy3 тегает OOV-пропера как NPRO/GRND («Гаванная»→GRND),
    # а UNKN — гарантированный OOV.
    _BLOCKED_POS = frozenset({
        'VERB', 'INFN', 'ADVB', 'INTJ', 'PRCL', 'CONJ', 'PRED', 'COMP', 'GRND',
    })

    def _candidates_sliding_window(
        self, clean_tokens: List[Token], clean_stems: List[str],
        clean_lemmas: Optional[List['Lemma']] = None,
        max_window: Optional[int] = None,
    ) -> List[Candidate]:
        """Генерация N-грамм с сегментацией по предлогам (TASK 1) и POS-guard.

        Сегментация: предлоги (lemma.is_prep) вырезаются из последовательности,
        окно НЕ склеивает токены через предлог — «повернул на Ветеранов» даёт
        сегменты [повернул], [Ветеранов], и пара «Ветеранов блокпост» не
        склеивается через предлог. Токен сразу после предлога помечается
        is_anchored (prepositional boost, как раньше через prev_text).

        Если lemmas не переданы (compat/тесты) — старое поведение без
        сегментации, якорь по prev_text из _LOC_PREPS.

        POS-guard (TASK 1): окна, ВСЕ токены которых уверенно опознаны как
        не-топоним (VERB/ADVB/...), отбрасываются — НО окно со стемом,
        присутствующим в индексе (stem-rescue), сохраняется: pymorphy3 тегает
        OOV-пропера как GRND/NPRO, и стем-совпадение надёжнее POS-гипотезы.
        """
        if max_window is None:
            max_window = (
                settings.similarity.max_sliding_window
                if settings and settings.similarity else 3
            )
        # NOISE-gap: шумовые токены («ст.», «ул.», «г.», суффиксы порядковых)
        # выкидываются ДО генерации окон — «11 ст. Фонтана» даёт окна как
        # «11 Фонтана», и Tier 1 попадает в ключ справочника напрямую.
        sig_tokens: List[Token] = []
        sig_stems: List[str] = []
        sig_lemmas: List['Lemma'] = []
        for i, (t, s) in enumerate(zip(clean_tokens, clean_stems)):
            if (t.text or '').strip().lower() in _NOISE_TOKENS:
                continue
            sig_tokens.append(t)
            sig_stems.append(s)
            if clean_lemmas is not None and i < len(clean_lemmas):
                sig_lemmas.append(clean_lemmas[i])
        clean_tokens, clean_stems = sig_tokens, sig_stems
        if clean_lemmas is not None:
            clean_lemmas = sig_lemmas

        pos_enabled = (
            settings.similarity.enable_pos_filter
            if settings and settings.similarity else True
        )
        if not clean_tokens:
            return []

        # Сегментация по предлогам (TASK 1): сегмент = макс. последовательность
        # токенов без предлогов. seg_id[i] — номер сегмента токена i;
        # anchored[i] — токен стоит сразу после предлога.
        seg_of = [0] * len(clean_tokens)
        anchored = [False] * len(clean_tokens)
        if clean_lemmas is not None and len(clean_lemmas) == len(clean_tokens):
            seg = 0
            prev_is_prep = False
            for i, lemma in enumerate(clean_lemmas):
                if lemma.is_prep:
                    seg += 1
                    prev_is_prep = True
                    continue
                seg_of[i] = seg
                anchored[i] = prev_is_prep
                prev_is_prep = False
        else:
            clean_lemmas = None  # длины не сошлись — POS-логика отключается

        out = []
        seen: Set[Tuple[int, int]] = set()
        n = len(clean_tokens)
        for start_i in range(n):
            # Предлог не начинает окно (он граница, а не кандидат).
            if clean_lemmas is not None and clean_lemmas[start_i].is_prep:
                continue
            current_stem = clean_stems[start_i]
            prev_text = clean_tokens[start_i - 1].text.lower() if start_i > 0 else ''
            is_anchored = anchored[start_i] or prev_text in _LOC_PREPS

            # Предфильтр-якорь: пропускаем позицию, если её одиночный токен не
            # может быть началом матча. Помимо точного стема учитываем:
            #  - has_stem_anywhere: стем входит в ЛЮБОЙ (в т.ч. многословный)
            #    ключ индекса — иначе "Застава 2" в начале сообщения терялось
            #    (стем 'застав' есть только в паре ('2', 'застав'));
            #  - длину поверхности >= 5: кандидат для Tier 2 (орфо-корректор),
            #    который матчит по сырому surface, а не по стему — опечатка в
            #    начале сообщения иначе не доходила до Tier 2 вовсе.
            if (not is_anchored and current_stem
                    and not self._index.has_stem(current_stem)
                    and not self._index.has_stem_anywhere(current_stem)
                    and len(clean_tokens[start_i].text) < 5):
                continue

            # Окна растут ВНУТРИ сегмента: end не переходит через предлог
            # и не ЗАКАНЧИВАЕТСЯ на предлоге (окно не содержит предлог вовсе).
            seg = seg_of[start_i]
            max_end = start_i
            while max_end < n - 1 \
                    and seg_of[max_end + 1] == seg \
                    and not (clean_lemmas is not None and clean_lemmas[max_end + 1].is_prep) \
                    and max_end - start_i + 1 < max_window:
                max_end += 1

            for end_i in range(start_i, max_end + 1):
                if (start_i, end_i) in seen:
                    continue
                seen.add((start_i, end_i))
                slice_t = clean_tokens[start_i:end_i + 1]
                slice_s = clean_stems[start_i:end_i + 1]
                surface_text = ' '.join(t.text.lower() for t in slice_t)
                stem_tuple = tuple(s for s in slice_s if s)
                # POS-guard (TASK 1): окно из ТОЧНО опознанных не-топонимов
                # (VERB, ADVB, GRND...) отбрасывается, ЕСЛИ ни один его стем
                # не присутствует в индексе (stem-rescue) — иначе теряли бы
                # OOV-пропера вроде «героив» (GRND), матчащиеся по стему.
                if pos_enabled and clean_lemmas is not None:
                    slice_l = clean_lemmas[start_i:end_i + 1]
                    if slice_l and all(
                        lemma.pos in self._BLOCKED_POS
                        for lemma in slice_l if lemma.pos
                    ) and not any(
                        s and (self._index.has_stem(s)
                               or self._index.has_stem_anywhere(s))
                        for s in stem_tuple
                    ):
                        continue
                size = end_i - start_i + 1
                out.append((surface_text, stem_tuple, start_i, end_i, size,
                            False, is_anchored, seg))
        return out

    async def _link_span_tier1(self, surface: str, stems: Tuple[str, ...], span: Tuple[int, int]) -> Optional[Dict]:
        """Только Tier 1: точный стем-матч. Tier-2 вынесен в batch."""
        if not surface or not stems:
            return None

        hit = self._index.query_stem_tuple(stems)
        source = 'stem_exact'
        if not hit and len(stems) >= 2:
            hit = self._index.query_stem_tuple_sorted(stems)
            source = 'stem_reorder'

        # Subset-rescue (REG-фикс events_export id 1, «Д.донского/ Ромашковая»):
        # аббревиатура перед фамилией даёт однословное окно с уникальным стемом
        # фамильной части («донского» → 'донск'), у которого нет собственного
        # ключа, но есть ключ-надмножество ('дмитр','донск'). Раньше окно
        # молча умирало → событие в random.
        # Guards против ложных срабатываний:
        #  1) длина поверхности >= 4 (одно-двухбуквенные окна не рескьюим);
        #  2) алиас-кандидат не содержит родовых head-слов (улица/дорога/парк…)
        #     и слов, встречающихся в 2+ алиасах — иначе «дорога на 7-й»
        #     заматчилась бы на «южную дорогу», «улица» — на «Улицу Толбухина»;
        #  3) частичный поверхностный матч: fuzz.partial_ratio >= 95
        #     (surface — подстрока полного имени: «донского» ⊂ «дмитрия донского»).
        if not hit and len(stems) == 1 and len(surface) >= 4:
            rescue: List["PhoneticEntry"] = []
            for key in self._index.keys_with_stem(stems[0]):
                if len(key) >= 2:
                    rescue.extend(self._index.query_stem_tuple(key))
            rescue = [
                e for e in rescue
                if not any(
                    w in _GENERIC_HEAD_WORDS
                    or self._index.alias_object_count(w) >= 2
                    for w in e.variant_text.split()
                )
                and fuzz.partial_ratio(surface, e.variant_text) >= 95
            ]
            if rescue:
                hit = rescue
                source = 'stem_subset'

        if hit:
            # REG-фикс (events_export, «6 элемент» → score 0.696): выбор hit[0]
            # игнорировал остальные алиасы ТОГО ЖЕ объекта: для «6 элемент»
            # exact-хиты — ('6','элемент') и ('шестой','элемент'), и побеждал
            # «Шестой элемент» с ratio 0.64 вместо 1.0 по идентичному алиасу.
            # Теперь surface-близость решает всегда: уникальные алиасы одного
            # объекта не конфликтуют (max по ratio возвращает их общий geo_id),
            # для разных объектов логика не изменилась.
            best = max(
                hit,
                # ratio — главный критерий (partial favoreет КОРОТКИЕ кандидаты:
                # «Гаваи» — почти-подстрока «гаванной» — иначе обгонял бы
                # «Гаванную», ломая test_gavannaya_recall); partial — только
                # tiebreak для subset-rescue (равные ratio у разных алиасов).
                key=lambda e: (
                    fuzz.ratio(surface, e.variant_text),
                    fuzz.partial_ratio(surface, e.variant_text),
                ),
            )

            # Скоринг: для exact-матчей ratio = 1.0 — поведение исходного кода
            # не меняется. Для subset-rescue surface — подмножество полного
            # имени («донского» vs «дмитрия донского»): partial_ratio = 100,
            # но полный рейтинг честнее — score = max(ratio, 0.85 * partial):
            # «донского» → 0.85 (проходит порог 0.80 в process_candidates_v2),
            # при этом явная неполнота совпадения отражена в score.
            score = max(
                fuzz.ratio(surface, best.variant_text) / 100.0,
                0.85 * fuzz.partial_ratio(surface, best.variant_text) / 100.0,
            )
            # Парадигм-буст (REG: acceptance кейс 3, «французского» → 0.78):
            # если surface — известная словоформа того же объекта, fuzz-штраф
            # за падеж не отражает качество распознавания — матч уверенный.
            # Два пути:
            #  1) форма есть в парадигм-индексе объекта (проперы, R-PR30);
            #  2) surface и алиас — формы одной лексемы по pymorphy (словарные
            #     имена-прилагательные вроде «Французский», которых парадигма
            #     не генерирует из-за отсутствия Geox-тега).
            if score < 1.0 and (
                self._index.is_known_surface_for_object(surface, best.street_id)
                or self._is_same_lexeme(surface, best.variant_text)
            ):
                score = 1.0
            return {
                'geo_id': best.street_id,
                'score': score,
                'matched_name': best.canonical_name,
                'text': surface,
                'source': source,
                '_span': span,
            }
        return None

    def _is_same_lexeme(self, surface: str, variant: str) -> bool:
        """True, если surface и variant — формы одной лексемы (pymorphy).

        Для OOV-проперов pymorphy возвращает саму словоформу как normal_form —
        сравнение честно даёт False и буст не применяется (там работает
        парадигм-индекс). Служебный guard для парадигм-буста Tier 1.
        """
        try:
            n1 = self._morph.lemmatize_word(surface).normal_form
            n2 = self._morph.lemmatize_word(variant).normal_form
            return bool(n1) and n1 == n2
        except Exception:
            return False

    async def _link_span(self, surface: str, stems: Tuple[str, ...], span: Tuple[int, int]) -> Optional[Dict]:
        """Поиск geo-объекта по тексту: Tier 1 (стемы) → Tier 2 (опечатки).

        NOTE: в find_geo() Tier 2 выполняется батчем (tier2_queries), этот метод
        используется только извне/тестами. Порог и guard'ы общие
        (_typo_threshold_percent / _accept_typo_match) — пути не разъезжаются.
        """
        if not surface:
            return None

        result = await self._link_span_tier1(surface, stems, span)
        if result:
            return result

        # Tier 2: орфо-корректор по surface (те же guard'ы, что и в батч-пути).
        typo_thresh = _typo_threshold_percent()
        s_phrases, s_meta = self._index.surface_phrases()
        if s_phrases and len(surface) >= 5:
            if self._executor:
                try:
                    loop = asyncio.get_running_loop()
                    s_match = await asyncio.wait_for(
                        loop.run_in_executor(
                            self._executor,
                            _fuzzy_match,
                            surface,
                            s_phrases,
                            typo_thresh
                        ),
                        timeout=5.0,
                    )
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"[Geo] Parallel fuzzy match timeout/failed: {e}, falling back to sync")
                    s_match = _fuzzy_match(surface, s_phrases, typo_thresh)
            else:
                s_match = _fuzzy_match(surface, s_phrases, typo_thresh)

            if s_match:
                cand, score, idx = s_match
                entry = self._accept_typo_match(surface, cand, idx, s_meta)
                if entry is not None:
                    return {
                        'geo_id': entry.street_id,
                        'score': score / 100.0,
                        'matched_name': entry.canonical_name,
                        'text': surface,
                        'source': 'surface_typo',
                        '_span': span,
                    }
        return None

    def _classify_geo_tier(self, best_by_geo: Dict[int, Dict]) -> str:
        """Map a find_geo result set to a match tier for observability."""
        if not best_by_geo:
            return "none"
        sources = {r.get("source") for r in best_by_geo.values()}
        if sources & {"stem_exact", "stem_reorder"}:
            return "tier1"
        if "surface_typo" in sources:
            return "tier2"
        return "none"

    def _finalize(self, best_by_geo: Dict[int, Dict]) -> List[Dict]:
        """Дедупликация, сортировка и возврат top-K найденных geo-объектов.

        Longest-match-first (TASK 2): под-спан, вложенный в более длинный матч,
        отбрасывается — НО только внутри одного сегмента (токены разных
        сегментов разделены предлогом и не могут быть частью одного имени).
        Итог сортируется по score, при равенстве — длинный матч выше.
        """
        top_k = (
            settings.similarity.max_entities if settings and settings.similarity else 5
        )
        kept: List[Dict] = []
        for r in sorted(best_by_geo.values(),
                        key=lambda x: (x['_span'][1] - x['_span'][0], x['score']),
                        reverse=True):
            s, e = r['_span']
            seg = r.get('_segment', -1)
            if any(k.get('_segment', -1) == seg
                   and ks <= s and e <= ke and (ke - ks) > (e - s)
                   for k in kept for (ks, ke) in (k['_span'],)):
                continue
            kept.append(r)

        results = sorted(
            kept,
            key=lambda x: (x['score'], x['_span'][1] - x['_span'][0]),
            reverse=True,
        )[:top_k]
        for r in results:
            r.pop('_span', None)
            r.pop('_segment', None)
            r['type'] = self._geo_types.get(r['geo_id'], '')
        source_stats = {}
        for r in results:
            source_stats[r['source']] = source_stats.get(r['source'], 0) + 1
        logger.debug(
            f"[Geo] Found {len(results)} (sources={source_stats}): "
            f"{[(r['matched_name'], round(r['score'], 2), r['source']) for r in results]}"
        )
        return results

    async def match_phrase(self, phrase: str) -> Optional[Dict]:
        """Прицельный матч одного фрагмента текста (адресный fast-path, N4b).

        Делегирует в find_geo на тексте фрагмента и возвращает ЛУЧШИЙ результат
        (find_geo уже отсортировал: score desc, затем длинный матч выше).
        ТЕ ЖЕ примитивы и пороги (Tier 1/2, POS-filter, stopwords, longest-match,
        subset-rescue) — второй реализации матчинга нет, поведение идентично
        общему пути на том же тексте.

        Возвращает dict {geo_id, score, matched_name, text, source, type} или
        None. Порог фильтрует ВЫЗЫВАЮЩИЙ (0.80, как candidate_min_score).
        """
        if not self._initialized or not phrase:
            return None
        cleaned = clean(phrase)
        if not cleaned:
            return None
        tokens = tokenize(cleaned)
        if not tokens:
            return None
        lemmas = self._morph.lemmatize_tokens(tokens)
        results = await self.find_geo(tokens=tokens, lemmas=lemmas)
        if not results:
            return None
        best = results[0]
        best.setdefault('type', self._geo_types.get(best['geo_id'], ''))
        return best

    async def find_geo(
        self,
        tokens: List[Token],
        lemmas: List[Lemma],
        text: Optional[str] = None,
    ) -> List[Dict]:
        """Поиск по geo таблице. Возвращает List[Dict] с geo_id/score/matched_name.

        text — ПОЛНЫЙ исходный текст сообщения: передаётся семантической модели
        для валидации кандидатов «серой зоны» (0.70–0.85). Если не задан —
        семантический фильтр не вызывается (режим совместимости/тестов).

        Результаты сортируются: сначала settlement (village/town), потом street.
        """
        if not self._initialized:
            logger.warning("[Geo] Not initialized")
            return []
        if self._index.is_empty:
            logger.warning("[Geo] Index is empty")
            return []
        if not tokens or not lemmas:
            return []

        clean_tokens, clean_lemmas = self._strip_noise(tokens, lemmas)
        if not clean_tokens:
            return []

        clean_stems = self._morph.stem_tokens(clean_tokens)
        candidates = self._candidates_sliding_window(clean_tokens, clean_stems, clean_lemmas)
        if not candidates:
            return []

        boost = (
            settings.similarity.prepositional_boost
            if settings and settings.similarity else 0.05
        )

        best_by_geo: Dict[int, Dict] = {}
        tier2_queries = []
        tier2_meta = []

        s_phrases, s_meta = self._index.surface_phrases()

        # Tier-1 length-floor (TASK 2): чем длиннее совпавшая n-грамма, тем
        # меньше штраф поверх fuzz-скора — стем-матч уже нормализовал падеж,
        # а fuzz по surface не должен опускать верный матч ниже
        # candidate_min_score. Считается один раз вне цикла.
        max_w = (
            settings.similarity.max_sliding_window
            if settings and settings.similarity else 3
        )
        for surface, stem_tuple, start_i, end_i, _size, _gap, is_anchored, seg in candidates:
            if surface in self._stopwords:
                continue

            result = await self._link_span_tier1(surface, stem_tuple, (start_i, end_i))
            if result:
                result['_anchored'] = is_anchored
                result['_segment'] = seg
                # BUG FIX: Removed score floor — it artificially inflated stem-match
                # scores, preventing proper calibration. The fuzz.ratio score already
                # reflects surface similarity; flooring it masked poor matches.
                gid = result['geo_id']
                existing = best_by_geo.get(gid)
                if existing is None or result['score'] > existing['score']:
                    best_by_geo[gid] = result
            else:
                if s_phrases and len(surface) >= 5:
                    tier2_queries.append(surface)
                    tier2_meta.append({
                        'surface': surface,
                        'span': (start_i, end_i),
                        'is_anchored': is_anchored,
                        'segment': seg,
                    })

        if tier2_queries:
            typo_thresh = _typo_threshold_percent()
            # BUG FIX: Deduplicate tier2_queries AND tier2_meta together.
            # Previously only tier2_queries was deduplicated, causing index
            # misalignment — meta[i] referred to wrong surface/span.
            seen_surfaces: Set[str] = set()
            deduped_queries: List[str] = []
            deduped_meta: List[dict] = []
            for q, m in zip(tier2_queries, tier2_meta):
                if q not in seen_surfaces:
                    seen_surfaces.add(q)
                    deduped_queries.append(q)
                    deduped_meta.append(m)
            tier2_queries = deduped_queries
            tier2_meta = deduped_meta

            if self._executor:
                loop = asyncio.get_running_loop()
                try:
                    batch_results = await asyncio.wait_for(
                        loop.run_in_executor(
                            self._executor,
                            _batch_fuzzy_match,
                            tier2_queries,
                            s_phrases,
                            typo_thresh,
                        ),
                        timeout=10.0,
                    )
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"[Geo] Batch fuzzy match timeout/failed: {e}, skipping tier2")
                    batch_results = {}
            else:
                # Без пула потоков (тесты, деградация, сбой инициализации
                # executor) — синхронный Tier 2 с лимитом.
                batch_results = {}
                for surface in tier2_queries[:10]:
                    m = _fuzzy_match(surface, s_phrases, typo_thresh)
                    if m:
                        batch_results[surface] = m

            for i, surface in enumerate(tier2_queries):
                if surface in batch_results:
                    match, score, idx = batch_results[surface]
                    entry = self._accept_typo_match(surface, match, idx, s_meta)
                    if entry is not None:
                        meta = tier2_meta[i]
                        result = {
                            'geo_id': entry.street_id,
                            'score': score / 100.0,
                            'matched_name': entry.canonical_name,
                            'text': surface,
                            'source': 'surface_typo',
                            '_span': meta['span'],
                            '_anchored': meta['is_anchored'],
                            '_segment': meta.get('segment', -1),
                        }
                        gid = result['geo_id']
                        existing = best_by_geo.get(gid)
                        if existing is None or result['score'] > existing['score']:
                            best_by_geo[gid] = result

        # Prepositional boost: applies only when score < 0.85 (grey zone).
        # BUG FIX: Previously boost applied to ALL anchored candidates,
        # including those already >= 0.85, inflating high-confidence matches.
        # Now boost only helps borderline candidates, capped at 0.85.
        for r in best_by_geo.values():
            if r.pop('_anchored', False):
                if r['score'] < 0.85:
                    r['score'] = min(0.85, r['score'] + boost)

        if geo_match_tier_total is not None:
            geo_match_tier_total.labels(self._classify_geo_tier(best_by_geo)).inc()

        return self._finalize(best_by_geo)
