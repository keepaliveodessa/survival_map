"""Определение слоя события по ключевым словам с морфологической нормализацией.

Раньше слой определялся жёстким substring-match (`word.startswith(keyword)`),
что давало ложные срабатывания (`пост` ловил `постель`) и не учитывало
словоформы. Сейчас и ключевые слова, и токены сообщения приводятся к
нормальной форме через mawo_pymorphy3 — поэтому `патрулём`, `патруля`,
`патрули` одинаково матчатся с ключом `патруль`. Коды и аббревиатуры
(`h1`-`h5`, `бп`, `дтп`) лемматизация не меняет — они матчатся как есть.

В новой архитектуре `classify()` принимает уже лемматизированный List[Lemma]
от Morphology (общая лемматизация для матчера и классификатора — единый
проход pymorphy3 на сообщение).

Приоритет при совпадении ключей из разных слоёв: bus → cops → traffic → pig.
Теги '#' на классификацию не влияют — '#' удаляется в preprocess_light, слой
определяется только по тексту (леммам), а не по тому, что автор пометил тегом.

HARD RULES (переопределяют standard priority):
  - «блокпост» / «бп» → traffic (situational keyword, НЕ cops)
  - Template messages (📍📌 + Адрес:) → pig (structured info, не situational)
"""

import logging
import re
from typing import Dict, List, Optional, Set

from .morphology import Lemma, Morphology

try:
    from common.settings import settings
    from common.settings import LAYER_PRIORITY as _LAYER_PRIORITY
except Exception:
    settings = None
    _LAYER_PRIORITY = ('bus', 'cops', 'traffic')

try:
    from common.metrics import layer_classification_fallback_total
except Exception:
    layer_classification_fallback_total = None

logger = logging.getLogger(__name__)

# Template message pattern: 📍 or 🏠 + Адрес: — structured info messages
_TEMPLATE_RE = re.compile(r'(📍|📌|🏠.*Адрес:)', re.IGNORECASE)


def _get_layer_keywords(layer: str) -> tuple:
    """Ключевые слова слоя из настроек (БД или fallback из common/settings)."""
    if settings and settings.similarity:
        return settings.similarity.get_layer_keywords(layer)
    return ()


class LayerClassifier:
    """Морфологический классификатор слоя события."""

    def __init__(self, morph: Morphology) -> None:
        """morph — Morphology обёртка (общий MorphAnalyzer на процесс)."""
        self._morph = morph
        # {layer: множество лемматизированных ключевых слов}
        self._keyword_lemmas: Dict[str, Set[str]] = {}
        for layer in _LAYER_PRIORITY:
            self._keyword_lemmas[layer] = {
                self._lemma(kw) for kw in _get_layer_keywords(layer) if kw
            }
        logger.info(
            "[Layer] keyword lemmas: "
            + ", ".join(f"{l}={len(s)}" for l, s in self._keyword_lemmas.items())
        )

    def _lemma(self, word: str) -> str:
        """Начальная форма ключевого слова через Morphology."""
        word = word.strip().lower()
        if not word:
            return ''
        return self._morph.lemmatize_word(word).normal_form

    def classify(self, lemmas: List[Lemma], raw_text: str = None) -> str:
        """Слой по приоритету bus → cops → traffic, иначе 'pig'.

        Принимает уже лемматизированные токены (от Morphology.lemmatize_tokens).
        Слой определяется по совпадению лемм с ключевыми словами слоёв,
        с учётом hard rules для блокпостов и шаблонных сообщений.

        raw_text — исходный текст (для детекции шаблонов). Если не задан,
        шаблонный детектор не вызывается.
        """
        result = 'pig'
        if lemmas:
            token_lemmas: Set[str] = {l.normal_form for l in lemmas if l.normal_form}

            # HARD RULE 1: «блокпост» / «бп» → traffic
            # Блокпост — situational keyword, ВСЕГДА traffic, даже если
            # в тексте есть «менты»/«мусора» (cops). Слово «блокпост»
            # не является гео-названием — это situation descriptor.
            if 'блокпост' in token_lemmas or 'бп' in token_lemmas:
                result = 'traffic'
            # HARD RULE 2: Template messages (📍📌 + Адрес:) → pig
            # Шаблонные сообщения — structured info, не situational.
            # Даже если описание содержит «менты/мусора», слой = pig.
            elif raw_text and _TEMPLATE_RE.search(raw_text):
                result = 'pig'
            # Standard priority: bus → cops → traffic
            else:
                for layer in _LAYER_PRIORITY:
                    if self._keyword_lemmas[layer] & token_lemmas:
                        result = layer
                        break

        if layer_classification_fallback_total is not None:
            layer_classification_fallback_total.labels(result).inc()
        return result
