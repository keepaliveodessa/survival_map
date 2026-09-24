"""Предобработка текста сообщений parser.

Две стадии:
  • preprocess_light — мягкая очистка, СОХРАНЯЕТ регистр и пунктуацию. Нужна,
    т.к. её результат уходит на фронтенд как description (там регистр/emoji/
    пунктуация должны остаться); токенайзер пунктуацию всё равно отбрасывает.
  • clean — агрессивная нормализация, lowercase + без пунктуации. Применяется к
    alias-именам при сборке phonetic-индекса и для канонизации фрагментов.

Конвейер обработки сообщения:
  1. strip_tail   — отбросить служебный хвост;
  2. preprocess_light — для description + токенизации/морфологии;
  3. clean(name)  — применяется в phonetic_index при сборке вариантов улицы.

`strip_tail` остаётся неизменным — он не зависит от регистра/пунктуации.
"""

import html
import re
from typing import Optional

# Маркеры служебного хвоста: всё начиная с самого раннего из них отбрасывается.
# Раньше '|' тоже был маркером, но он конфликтует с alias-separator в БД
# («улица|переулок» — synonym, ломалось при появлении в тексте). Удалён.
_TAIL_MARKERS = ('сообщить', 'подписаться')

# Хвост структурированных пинов: «🌐 Открыть пин на карте» (генерируется формой
# добавления события). Токены «открыть/пин/карте» — мусор для NLP: «пин» давал
# Tier-2 шум по справочнику, «карте» разбавлял окна. Резать как обычный хвост —
# тогда обрезка бесплатно работает и в описании на фронте (description).
# Эмодзи опционален (варианты рендера), якорь $ гарантирует: режется только
# суффикс в самом конце текста, середина сообщения не затрагивается.
_STRUCT_TAIL_RE = re.compile(
    r'\s*(?:🌐\s*)?Открыть\s+пин\s+на\s+карте\s*$', re.IGNORECASE
)

# HH:MM с разделителем ':' или '.', часы 0-23, минуты 00-59.
# Удаляется до замены пунктуации, иначе '14:30' распалось бы на '14' и '30'.
_TIME_RE = re.compile(r'\b([01]?\d|2[0-3])[:.][0-5]\d\b')
# «б/п», «б\п», «б / п» → «бп»: токенайзер режет по слэшу на отдельные слова,
# из-за чего аббревиатура блокпоста не совпадала с layer-keyword «бп» (traffic).
# Схлопываем до старта токенизации.
_BP_SLASH_RE = re.compile(r'\bб\s*[/\\]\s*п\b', re.IGNORECASE)
# Хэштег-теги (#Name, ##keyword): авторы канала помечают ими слова, но тег —
# НЕ доверенный якорь: тегированное слово не обязательно название объекта.
# Символ '#' удаляется здесь, до токенизации/матчинга — само слово остаётся
# обычным фуззи-кандидатом, но никакого «доверия тегу» больше нет.
_HASH_RE = re.compile(r'#')
_TAG_RE = re.compile(r'<[^>]+>')
_NON_ALNUM_RE = re.compile(r'[^a-zA-Zа-яА-ЯёЁ0-9]')
_SPACES_RE = re.compile(r'\s+')

# Emoji и пиктограммы. При поиске названий улиц/сущностей это шум: они
# попадают в токены, ломают лемматизацию и смещают границы фраз. Убираются
# ТОЛЬКО на этапе матчинга (см. strip_emoji) — в description, уходящем на
# фронтенд, emoji СОХРАНЯЮТСЯ. Диапазоны покрывают основные emoji-блоки
# Unicode плюс служебные модификаторы (variation selectors, ZWJ, keycap).
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # Misc Symbols/Pictographs … Symbols & Pictographs Ext-A
    "\U0001F000-\U0001F02F"  # Mahjong / Domino
    "\U0001F0A0-\U0001F0FF"  # Playing cards
    "\U00002600-\U000027BF"  # Misc symbols + Dingbats
    "\U00002B00-\U00002BFF"  # Misc Symbols and Arrows
    "\U00002190-\U000021FF"  # Arrows
    "\U0000FE00-\U0000FE0F"  # Variation Selectors
    "\U0001F1E6-\U0001F1FF"  # Regional indicators (флаги)
    "\U0000200D"             # Zero Width Joiner
    "\U000020E3"             # Combining Enclosing Keycap
    "\U00002122\U00002139"   # ™ ℹ
    "]+",
    flags=re.UNICODE,
)
# Украинские буквы → русские: і,ї → и; є → е; ё → е.
_UA_TABLE = str.maketrans('іїєёІЇЄЁ', 'ииееИИЕЕ')

# Украинские окончания → русские эквиваленты (G6). Применяется ПОСЛЕ _UA_TABLE,
# чтобы дополнительно нормализовать прилагательные/существительные:
#   «Балкивська» → «Балковская», «Дерибасівський» → «Дерибасовский»,
#   «Пушкінської» → «Пушкинской».
# Регекспы case-insensitive чтобы покрыть Title Case.
_UA_SUFFIX_FIXES = [
    (re.compile(r'івська\b', re.IGNORECASE), 'овская'),
    (re.compile(r'івський\b', re.IGNORECASE), 'овский'),
    (re.compile(r'івської\b', re.IGNORECASE), 'овской'),
    (re.compile(r'івською\b', re.IGNORECASE), 'овской'),
    (re.compile(r'івському\b', re.IGNORECASE), 'овскому'),
    (re.compile(r'ська\b', re.IGNORECASE), 'ская'),
    (re.compile(r'ський\b', re.IGNORECASE), 'ский'),
    (re.compile(r'ської\b', re.IGNORECASE), 'ской'),
    (re.compile(r'ською\b', re.IGNORECASE), 'ской'),
    (re.compile(r'ському\b', re.IGNORECASE), 'скому'),
    (re.compile(r'цька\b', re.IGNORECASE), 'цкая'),
    (re.compile(r'цький\b', re.IGNORECASE), 'цкий'),
]


# Высокоточные маркеры рекламы/спама в КОНТЕНТЕ (проверять ПОСЛЕ strip_tail —
# футеры "подписаться"/"сообщить" уже срезаны). Реальные репорты ссылок/хендлов/
# призывов к подписке почти не содержат. ВАЖНО: слова "реклам" в маркерах НЕТ —
# реальные репорты часто описывают "бус с рекламой окон" (типичный объект), их
# глушить нельзя; они должны матчиться к улице как обычно.
_PROMO_RE = re.compile(
    r'(https?://|www\.|t\.me/|@[A-Za-z][\w]{3,}'
    r'|платн\w*\s+подписк|подписк\w*\s+на\s+канал|наш\s+канал)',
    re.IGNORECASE,
)


def is_promotional(text: str) -> bool:
    """Грубый, высокоточный детектор рекламы/спама (ссылки, telegram-хендлы,
    призывы к подписке). Цель — не ставить рекламу на УЛИЦУ: при срабатывании
    сообщение идёт в strategy=random (отображается случайной точкой), но НЕ
    игнорируется. Мягкую рекламу НЕ ловит намеренно (precision важнее).
    """
    if not text:
        return False
    return bool(_PROMO_RE.search(text))


def strip_tail(text: str) -> str:
    """Отбросить хвост сообщения начиная с самого раннего служебного маркера.

    Структурированные пины (📍 … 🏠 Адрес: … 📝 Описание: …) перед этим
    освобождаются от суффикса «🌐 Открыть пин на карте» — он служебный и
    для NLP/фронта бесполезен.
    """
    if not text:
        return ''

    text = _STRUCT_TAIL_RE.sub('', text)

    lowered = text.lower()
    cut = len(text)
    for marker in _TAIL_MARKERS:
        pos = lowered.find(marker)
        if pos != -1 and pos < cut:
            cut = pos

    return text[:cut].strip()


def strip_emoji(text: str) -> str:
    """Удалить emoji/пиктограммы — для этапа матчинга названий сущностей.

    НЕ применять к тексту, уходящему на фронтенд: там emoji должны остаться
    в исходном виде. Используется только перед токенизацией/классификацией.
    """
    if not text:
        return ''
    return _SPACES_RE.sub(' ', _EMOJI_RE.sub(' ', text)).strip()


def preprocess_light(text: str) -> str:
    """Мягкая очистка: снять HTML, удалить таймстампы, теги '#', нормализовать укр. буквы.

    СОХРАНЯЕТ регистр и пунктуацию — результат уходит на фронтенд как description.
    Символ '#' удаляется (тег — не доверенный якорь), но слово после него остаётся.
    Токенайзер (word_tokenizer.tokenize) сам режет по не-буквенным символам.
    """
    if not text:
        return ''

    text = html.unescape(text)
    text = _TAG_RE.sub(' ', text)
    text = _TIME_RE.sub(' ', text)
    text = _BP_SLASH_RE.sub('бп', text)
    text = _HASH_RE.sub(' ', text)
    text = text.translate(_UA_TABLE)
    for pattern, repl in _UA_SUFFIX_FIXES:
        text = pattern.sub(repl, text)
    text = _SPACES_RE.sub(' ', text)
    return text.strip()


def truncate_for_geo(text: str, max_len: int) -> str:
    """Обрезка длинного сообщения для гео-анализа вместо выброса (B6).

    Длинные сообщения раньше целиком заменялись заглушкой «слишком длиннное…»
    и уходили в random ни с чем. Теперь сохраняем начало и конец текста —
    локация в длинных сообщениях обычно в начале или в конце, середина —
    повествование и детали.
    """
    if len(text) <= max_len:
        return text
    half = max_len // 2
    head = text[:half].rsplit(' ', 1)[0]
    tail = text[-half:].split(' ', 1)[-1]
    return f"{head} … {tail}"


def clean(text: str) -> str:
    """Агрессивная нормализация: убрать пунктуацию, lower-case.

    Применяется к небольшим фрагментам (LOC-спаны, alias-имена улиц) для
    приведения к канонической форме перед лексическим фуззи-матчем.
    """
    if not text:
        return ''

    text = html.unescape(text)
    text = _TAG_RE.sub(' ', text)
    text = _TIME_RE.sub(' ', text)
    text = _NON_ALNUM_RE.sub(' ', text)
    text = text.translate(_UA_TABLE)
    for pattern, repl in _UA_SUFFIX_FIXES:
        text = pattern.sub(repl, text)
    text = _SPACES_RE.sub(' ', text)
    return text.strip().lower()


def sanitize_text(text: Optional[str]) -> Optional[str]:
    """Ensure text is valid UTF-8, replacing any invalid bytes.

    Used before database insertion to prevent encoding errors from
    Telegram messages containing mixed encodings.
    """
    if not text:
        return text
    return text.encode('utf-8', errors='replace').decode('utf-8')
