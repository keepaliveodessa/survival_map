"""PhoneticIndex — индекс geo-объектов: стем-индекс (распознавание) + surface (опечатки).

Архитектура (после перехода на морфологический распознаватель):
  • для каждого алиаса geo-объекта строим:
    - **стем-кортеж** (Snowball по каждому токену) — ключ точного распознавания;
    - **сырую поверхностную форму** — для орфо-корректора (typo, Tier 2).
  • Tier 1 (распознавание) — точный lookup по стем-кортежу: O(1), детерминирован,
    инвариантен к падежу. Гаванной≡Гаванная, потому что стем один ("гава").
  • Tier 2 (опечатки) — rapidfuzz по сырым алиасам, только как корректор
    орфографии (высокий cutoff + length-guard), НЕ как основной матч.

Стем устойчив к OOV-проперам (имена объектов несловарны), где pymorphy-лемматизация
врёт. Класс называется PhoneticIndex для обратной совместимости (используется в
geo_matcher.py и тестах под этим именем).
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .morphology import Morphology
from common.text_preprocessor import clean
from .word_tokenizer import tokenize

try:
    from common.settings import settings
except Exception:
    settings = None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhoneticEntry:
    """Запись индекса: geo-объект + одно из его каноничных названий + строка-вариант.

    Оптимизация памяти: frozen=True позволяет Python оптимизировать хранение
    и снижает memory footprint по сравнению с обычными dataclass.
    """
    __slots__ = ('street_id', 'canonical_name', 'variant_text')
    street_id: int
    canonical_name: str   # первое значение из geo.names — для UI/логов
    variant_text: str     # стем-строка или сырой алиас (lowercase, single-space)


class PhoneticIndex:
    """Индекс geo-объектов: стем-кортежи (распознавание) + surface-фразы (опечатки).

    Полная пересборка через `build(rows)`; точечная замена одной улицы через
    `replace_street(street_id, row)`. Оба метода — sync; вызываются через
    `asyncio.to_thread` если нужен off-loop запуск. Все структуры меняются
    атомарно через single-shot assignment в конце build/replace.
    """

    def __init__(self, morph: Morphology) -> None:
        self._morph = morph

        # Стем-индекс: кортеж стемов токенов алиаса → записи (Tier 1, exact).
        self._stem_index: Dict[Tuple[str, ...], List[PhoneticEntry]] = {}
        # Порядок-независимый индекс многословных имён (ключ = sorted стем-кортеж):
        # "Застава 2" ≡ "2 застава". Только len>=2; запрашивается при промахе exact.
        self._stem_index_sorted: Dict[Tuple[str, ...], List[PhoneticEntry]] = {}
        # Surface-фразы — сырые алиасы (lowercase, clean()) для Tier 2 (опечатки).
        self._surface_phrases: List[str] = []
        self._surface_phrase_meta: List[PhoneticEntry] = []
        # Множество ВСЕХ отдельных стемов, встречающихся в любом ключе индекса
        # (в т.ч. внутри многословных: "застав" из ('2', 'застав')). Нужен для
        # предфильтра слайдинг-окна — см. has_stem_anywhere().
        self._all_stems: set = set()

    # ----------------------------------------------------------- helpers

    def _stem_tuple_for_name(self, name: str) -> Tuple[str, ...]:
        """Кортеж стемов имени улицы — ключ распознавания."""
        cleaned = clean(name)
        if not cleaned:
            return ()
        tokens = tokenize(cleaned)
        if not tokens:
            return ()
        return tuple(s for s in self._morph.stem_tokens(tokens) if s)

    # POS-теги, разрешённые для генерации падежных форм.
    _PARADIGM_POS = frozenset({'NOUN', 'ADJF', 'ADJS'})
    # pymorphy3-теги, указывающие на имя собственное / топоним.
    _PROPER_TAGS = frozenset({'Geox', 'Name', 'Surn', 'Patr', 'Orgn'})

    def _generate_paradigms(self, name: str) -> List[str]:
        """Генерация падежных форм имени для surface-индекса (Tier 2).

        Генерация ТОЛЬКО если хотя бы один токен имени — пропер (Geox/Name).
        Иначе «Средняя» (прилагательное) генерирует «среднего/среднее» →
        false positive на «среди среди машин».

        Для каждого токена-пропера генерируются все падежные формы через
        pymorphy3. Это позволяет ловить родительный падеж (Балковского →
        Балковская) без ручных алиасов в geo.csv.

        Возвращает список surface-фраз (lowercase, clean): базовая + падежные
        формы каждого слова. Дубликаты отсекаются вызывающим кодом.
        """
        cleaned = clean(name)
        if not cleaned:
            return []
        words = cleaned.split()
        if not words:
            return []

        # Проверяем: есть ли хотя бы один пропер-токен в имени?
        has_proper = False
        for word in words:
            try:
                parses = self._morph._morph.parse(word)
                if parses:
                    tag_str = str(parses[0].tag)
                    if any(tag in tag_str for tag in self._PROPER_TAGS):
                        has_proper = True
                        break
            except Exception:
                pass

        # Если ни один токен не является пропером — не генерируем парадигму,
        # чтобы не создавать false positives от прилагательных/глаголов.
        if not has_proper:
            return []

        # Для каждого слова — собираем множество форм (нормальная + все падежи)
        word_forms: List[List[str]] = []
        for word in words:
            forms = {word.lower()}
            try:
                parses = self._morph._morph.parse(word)
                for p in parses:
                    if not hasattr(p, 'tag') or not p.tag:
                        continue
                    pos = str(p.tag.POS) if p.tag.POS else ''
                    if pos not in self._PARADIGM_POS:
                        continue
                    for form in p.lexeme:
                        if form.word:
                            forms.add(form.word.lower())
            except Exception:
                pass
            word_forms.append(sorted(forms))

        # Генерируем все комбинации падежных форм по словам
        result = []
        product = [[]]
        for wf in word_forms:
            capped = wf[:8]
            product = [comb + [f] for comb in product for f in capped]
            if len(product) > 200:
                product = [[' '.join(forms[:1]) for forms in word_forms]]
                break

        for combo in product:
            phrase = ' '.join(combo).strip()
            if phrase and phrase not in result:
                result.append(phrase)

        return result[:50]

    def _entries_for_street(self, street_id: int, names: List[str]) -> Tuple[
        List[Tuple[Tuple[str, ...], PhoneticEntry]],  # (stem_tuple, entry)
        List[Tuple[str, PhoneticEntry]],              # (surface_phrase, entry)
    ]:
        """Собрать индексные записи для одной улицы (дубликаты внутри отсекаются).

        Для каждого алиаса генерируются падежные формы (paradigm), которые
        добавляются как дополнительные surface-фразы в Tier 2 индекс.
        """
        if not names:
            return [], []
        canonical = names[0]

        stem_pairs: List[Tuple[Tuple[str, ...], PhoneticEntry]] = []
        surface_pairs: List[Tuple[str, PhoneticEntry]] = []
        seen_stem: set = set()
        seen_surface: set = set()

        for name in names:
            surface_phrase = clean(name).strip()
            stem_tuple = self._stem_tuple_for_name(name)
            # variant_text = сырой surface алиаса (не стем): нужен для разрешения
            # over-stem коллизий по поверхностной близости (Гаваи/Гаванная→"гава").
            if stem_tuple and stem_tuple not in seen_stem:
                seen_stem.add(stem_tuple)
                stem_pairs.append(
                    (stem_tuple, PhoneticEntry(street_id, canonical, surface_phrase))
                )

            if surface_phrase and surface_phrase not in seen_surface:
                seen_surface.add(surface_phrase)
                surface_pairs.append(
                    (surface_phrase, PhoneticEntry(street_id, canonical, surface_phrase))
                )

            # Paradigm: генерируем падежные формы и добавляем как surface-фразы
            for paradigm in self._generate_paradigms(name):
                if paradigm not in seen_surface:
                    seen_surface.add(paradigm)
                    surface_pairs.append(
                        (paradigm, PhoneticEntry(street_id, canonical, paradigm))
                    )

        return stem_pairs, surface_pairs

    # ---------------------------------------------------------------- build

    def build(self, rows) -> int:
        """Полная пересборка индексов из строк `geo`.

        rows — iterable of dict-like с ключами 'id' и 'names' (из таблицы geo). Возвращает
        количество surface-фраз.
        """
        new_stem_index: Dict[Tuple[str, ...], List[PhoneticEntry]] = {}
        new_stem_index_sorted: Dict[Tuple[str, ...], List[PhoneticEntry]] = {}
        new_surface_phrases: List[str] = []
        new_surface_meta: List[PhoneticEntry] = []

        street_count = 0
        for row in rows:
            street_id = row['id']
            names = row['names'] or []
            street_count += 1
            st_pairs, sp_pairs = self._entries_for_street(street_id, names)
            for stem_tup, entry in st_pairs:
                new_stem_index.setdefault(stem_tup, []).append(entry)
                if len(stem_tup) >= 2:
                    new_stem_index_sorted.setdefault(
                        tuple(sorted(stem_tup)), []).append(entry)
            for surface, entry in sp_pairs:
                new_surface_phrases.append(surface)
                new_surface_meta.append(entry)

        # Atomic swap.
        self._stem_index = new_stem_index
        self._stem_index_sorted = new_stem_index_sorted
        self._surface_phrases = new_surface_phrases
        self._surface_phrase_meta = new_surface_meta
        all_stems = set()
        for key in new_stem_index:
            all_stems.update(key)
        self._all_stems = all_stems

        logger.info(
            f"[PhoneticIndex] built: {len(new_surface_phrases)} surface phrases, "
            f"{len(new_stem_index)} stem tuples from {street_count} objects"
        )
        return len(new_surface_phrases)

    def replace_street(self, street_id: int, row: Optional[dict]) -> None:
        """Точечно заменить все записи одной улицы. row=None → улица удалена."""
        def _purge(idx):
            out = {t: [e for e in ents if e.street_id != street_id]
                   for t, ents in idx.items()}
            return {t: ents for t, ents in out.items() if ents}

        new_stem_index = _purge(self._stem_index)
        new_stem_index_sorted = _purge(self._stem_index_sorted)

        new_surface_phrases: List[str] = []
        new_surface_meta: List[PhoneticEntry] = []
        for surface, entry in zip(self._surface_phrases, self._surface_phrase_meta):
            if entry.street_id != street_id:
                new_surface_phrases.append(surface)
                new_surface_meta.append(entry)

        if row:
            names = row['names'] or []
            st_pairs, sp_pairs = self._entries_for_street(street_id, names)
            for stem_tup, entry in st_pairs:
                new_stem_index.setdefault(stem_tup, []).append(entry)
                if len(stem_tup) >= 2:
                    new_stem_index_sorted.setdefault(
                        tuple(sorted(stem_tup)), []).append(entry)
            for surface, entry in sp_pairs:
                new_surface_phrases.append(surface)
                new_surface_meta.append(entry)

        self._stem_index = new_stem_index
        self._stem_index_sorted = new_stem_index_sorted
        self._surface_phrases = new_surface_phrases
        self._surface_phrase_meta = new_surface_meta
        all_stems = set()
        for key in new_stem_index:
            all_stems.update(key)
        self._all_stems = all_stems
        logger.info(f"[PhoneticIndex] reindexed street {street_id}")

    # ---------------------------------------------------------------- queries

    def query_stem_tuple(self, stems: Tuple[str, ...]) -> List[PhoneticEntry]:
        """Точное совпадение по кортежу стемов (Tier 1)."""
        if not stems:
            return []
        return list(self._stem_index.get(stems, ()))

    def query_stem_tuple_sorted(self, stems: Tuple[str, ...]) -> List[PhoneticEntry]:
        """Совпадение многословного имени без учёта порядка слов (Tier 1b).

        Только для len>=2 ("Застава 2" ≡ "2 застава"). Запрашивать после промаха
        exact-порядка, чтобы не подменять точный матч.
        """
        if len(stems) < 2:
            return []
        return list(self._stem_index_sorted.get(tuple(sorted(stems)), ()))

    def surface_phrases(self) -> Tuple[List[str], List[PhoneticEntry]]:
        """Параллельные списки сырых алиасов для rapidfuzz Tier 2 (опечатки)."""
        return self._surface_phrases, self._surface_phrase_meta

    @property
    def is_empty(self) -> bool:
        return not self._stem_index and not self._surface_phrases

    def has_stem(self, stem: str) -> bool:
        """Проверить наличие стема в индексе (для предфильтрации)."""
        key = (stem,)
        return key in self._stem_index

    def has_stem_anywhere(self, stem: str) -> bool:
        """Стем встречается в составе ЛЮБОГО ключа индекса (в т.ч. многословного).

        Для предфильтра слайдинг-окна: первый токен многословного имени
        ("застава" из "2 застава") не имеет собственного ключа ('застав',), но
        входит в ключ ('2', 'застав') — позиция всё равно должна генерировать
        кандидатов, иначе имя в начале сообщения теряется.
        """
        return stem in self._all_stems
