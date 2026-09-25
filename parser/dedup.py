"""parser/dedup — дедупликация повторных сообщений канала (N4c).

Проблема (events_export6/7): авторы канала публикуют одно и то же сообщение
несколько раз («Бугаевская перед Дальницкая блокпост» ×3, «Окружная. ОККО» ×2) —
каждая копия доходит до pending_events и создаёт отдельное событие на карте.

Решение: in-memory кольцо «нормализованный текст → последний раз виден»
внутри ParserBot (одна копия на процесс parser, состояние не персистится —
рестарт только заново наберёт окно из истории, что безопасно).

Ключ — НЕЧУВСТВИТЕЛЬНЫЙ к регистру, пунктуации, emoji и повторам пробелов
(двойной пост «Блокпост!» и «блокпост» считают дубликатом), но РЕГИСТРО-
ЗНАЧИТЕЛЬНЫХ границ цифр не трогает: «2 застава» ≠ «12 застава».

ЭМОДЗИ И ЖЁСТКИЕ РАЗЛИЧИЯ: «⛔️⛔️ народ, остановитесь» и «народ,
остановитесь» склеятся в один ключ — это осознанный компромисс в пользу
дедупа; содержательные тексты канала различаются словами, а не эмодзи.
"""

import re
import time
from collections import OrderedDict
from typing import Optional

# Буквы+цифры всех алфавитов (для ключа пунктуация/эмодзи вырезаются).
_DEDUP_KEY_RE = re.compile(r'[^\w]+', re.UNICODE)

# Длина ключа после нормализации; пустой/ультракороткий текст не дедупим
# («блокпост» может повторяться ЛЕГИТИМНО как отдельное событие в другом
# месте — односимвольные/пустые ключи слишком часты, чтобы винить их).
_MIN_KEY_LEN = 4


def dedup_key(text: str) -> Optional[str]:
    """Ключ дедупликации: lowercase, без пунктуации/эмодзи/лишних пробелов.

    None — текст без нормализованного содержимого (дедуп не применять).
    """
    if not text:
        return None
    key = _DEDUP_KEY_RE.sub('', text.casefold())
    if len(key) < _MIN_KEY_LEN:
        return None
    return key


class TextDedup:
    """Окно последних увиденных текстов (LRU c TTL).

    OrderedDict + монотонные метки времени: пере-вставка существующего ключа
    двигает его в конец и обновляет TTL (повторяющиеся посты не дают ключу
    «протухнуть» посреди активного флуда).
    """

    def __init__(self, window_seconds: float = 1800.0, maxsize: int = 4096):
        self._window = float(window_seconds)
        self._maxsize = max(int(maxsize), 1)
        self._seen: "OrderedDict[str, float]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._seen)

    def _evict_expired(self, now: float) -> None:
        while self._seen:
            oldest_key, oldest_seen = next(iter(self._seen.items()))
            if now - oldest_seen <= self._window:
                break
            self._seen.popitem(last=False)

    def _evict_oversize(self) -> None:
        while len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)

    def is_duplicate(self, text: str, now: Optional[float] = None) -> bool:
        """True, если нормализованный текст уже встречался в пределах окна.

        Каждый вызов с валидным ключом ОБНОВЛЯЕТ метку времени — пользователь
        получит одно событие, пока флуд продолжается, и новое после паузы
        дольше окна (повторное «Бугаевская перед Дальницкая блокпост» через
        час — снова реальное событие).
        """
        key = dedup_key(text)
        if key is None:
            return False
        now = time.monotonic() if now is None else now
        self._evict_expired(now)
        seen_at = self._seen.get(key)
        if seen_at is not None:
            self._seen.move_to_end(key)
            self._seen[key] = now
            return True
        self._seen[key] = now
        self._evict_oversize()
        return False
