"""Парсер структурированных сообщений (адресный fast-path, N4b).

Форма добавления события генерирует пины вида:
    📍 <тип события> 🏠 Адрес: <адрес> 📝 Описание: <текст> 🌐 Открыть пин на карте
(хвост «🌐 …» срезается в parser через strip_tail; сюда он уже не доходит).

Адрес — иерархия через запятую: [дом,] улица/переулок/дорога, [ЖС/район,] [индекс].
Проблема общего матчера: адрес обрабатывается как обычный текст и побеждает
последнее знакомое слово (район/POI), а улица с домом теряется:
    «Кильцева улица … Аркадия» → пин на Аркадию, не на улицу.

Fast-path (фаза 3 в nlp_processor/main.py) берёт САМЫЙ СПЕЦИФИЧНЫЙ сегмент
адреса (с типом улицы) и матчит его прицельно; описание и тип события в
выбор гео-кандидатов НЕ входят.

Модуль чистый (без I/O) — легко тестируется; детект формата дешёвый
(два подстрочных поиска), вызывается до любого NLP только если fast-path включён.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

# Детект шаблона: оба маркера обязательны (🏠 + Адрес:), иначе это свободный
# текст со словом «адрес» — fast-path не применяется (старое поведение).
_ADDRESS_MARKER_RE = re.compile(r'🏠\s*Адрес\s*:', re.IGNORECASE)

# Заголовок: текст между 📍/📌 и 🏠 (тип события, для логов/диагностики).
_TITLE_RE = re.compile(r'(?:📍|📌)\s*([^🏠]+?)\s*🏠', re.IGNORECASE)

# Адрес: текст после «Адрес:» до 📝 (описание) или конца строки.
_ADDRESS_RE = re.compile(r'Адрес\s*:\s*([^📝]+)', re.IGNORECASE)

# Типы улиц в сегменте адреса → сегмент специфичен (приоритет матча).
_STREET_TYPE_RE = re.compile(
    r'(?:улиц|ул\.?|переул|проспект|дорог|бульвар|шосс|набережн|линия)\w*',
    re.IGNORECASE,
)

# Мусорные сегменты: номер дома, индекс, служебные обёртки ЖК.
_HOUSE_RE = re.compile(r'^\s*\d+\s*(?:/\s*\d+)?\s*(?:[а-яё]\)?\.?)?\s*$', re.IGNORECASE)
_POSTCODE_RE = re.compile(r'^\s*\d{5,6}\s*$')
# Служебные обёртки в начале сегмента: «ОК ЖСТ «Морське»», «Жилой комплекс
# «Ильичевский Рив’ера»» — снимаются итеративно (см. _strip_wrapper), пока
# остаётся содержательный хвост.
_JK_WRAPPER_RE = re.compile(
    r'^(?:ОК|ЖК|ЖСТ|КЖС|ТСН|Жилой|Житловий|комплекс|К|жк)\b[\s«»"\'\-]*',
    re.IGNORECASE,
)
_EDGE_QUOTE_RE = re.compile(r'^[\s«»"\']+|[\s«»"\']+$')

# Минимальная длина содержательного сегмента (короткие «В», «Г» — обёртки корпусов).
_MIN_SEGMENT_LEN = 4

# Максимум рассматриваемых сегментов (защита от аномально длинных адресов).
_MAX_SEGMENTS = 8


@dataclass
class StructuredMessage:
    """Разобранное структурированное сообщение."""
    title: str                       # тип события (📍 … до 🏠), '' если нет
    address: str                     # сырой адрес (после «Адрес:»)
    description: str                 # описание (после 📝), '' если нет
    address_segments: List[str] = field(default_factory=list)
    # Сегменты адреса в порядке ПРИОРИТЕТА МАТЧА: сначала содержательные
    # сегменты с типом улицы (улица/переулок/дорога…), затем остальные
    # содержательные (районы, POI). Мусор (дома, индексы, обёртки) выкинут.


def parse_structured(text: str) -> Optional[StructuredMessage]:
    """Распознать структурированный пин; None → обычное сообщение.

    None означает «путь fast-path не применять, обработать как раньше».
    """
    if not text:
        return None
    if not _ADDRESS_MARKER_RE.search(text):
        return None

    addr_match = _ADDRESS_RE.search(text)
    if not addr_match:
        return None
    address = addr_match.group(1).strip()
    if not address:
        return None

    title_match = _TITLE_RE.search(text)
    title = title_match.group(1).strip() if title_match else ''

    desc_match = re.search(r'📝\s*Описание\s*:\s*(.*)$', text, re.IGNORECASE | re.DOTALL)
    description = desc_match.group(1).strip() if desc_match else ''

    return StructuredMessage(
        title=title,
        address=address,
        description=description,
        address_segments=address_segments(address),
    )


def _strip_wrapper(segment: str) -> str:
    """Снять служебные обёртки ЖК/корпусов: «ОК ЖСТ «Морське»» → «Морське»,
    «Жилой комплекс «Ильичевский Рив’ера»» → «Ильичевский Рив’ера».

    Итеративно: обёртки бывают вложенными («ОК ЖСТ», «ЖК «…»»); на каждом
    шаге снимаем ведущие слова-обёртки и кавычки/пробелы с обоих краёв.
    """
    seg = segment
    for _ in range(4):  # глубина вложенности обёрток ограничена
        seg = _EDGE_QUOTE_RE.sub('', seg).strip()
        stripped = _JK_WRAPPER_RE.sub('', seg).strip()
        if stripped == seg:
            break
        seg = stripped
    return _EDGE_QUOTE_RE.sub('', seg).strip()


def address_segments(address: str) -> List[str]:
    """Сегменты адреса в порядке приоритета матча.

    Сначала — содержательные сегменты с типом улицы (самые специфичные),
    затем — остальные содержательные (районы, POI). Дома, индексы и
    служебные обёртки («ОК ЖСТ», «Жилой комплекс», кавычки, корпуса «В»)
    отбрасываются или зачищаются до имени.
    """
    raw = [s.strip().strip('«»"\'').strip() for s in address.split(',')]
    cleaned: List[str] = []
    for seg in raw:
        if not seg or len(seg) < _MIN_SEGMENT_LEN:
            continue
        if _HOUSE_RE.match(seg) or _POSTCODE_RE.match(seg):
            continue
        unwrapped = _strip_wrapper(seg)
        if not unwrapped or len(unwrapped) < _MIN_SEGMENT_LEN:
            continue
        cleaned.append(unwrapped)

    with_street_type = [s for s in cleaned if _STREET_TYPE_RE.search(s)]
    without = [s for s in cleaned if s not in with_street_type]
    return (with_street_type + without)[:_MAX_SEGMENTS]
