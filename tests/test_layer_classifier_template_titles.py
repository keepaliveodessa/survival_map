"""Тесты LayerClassifier на заголовках структурированных пинов (REG-фикс).

Источник кейсов — все шаблонные пины из events_export{,2,3,4}.csv (28 шт., см.
анализ типов событий). Проверяем:
  1. Заголовок «📍 Полиция (пешие) 🏠 Адрес: …» → cops (раньше HARD RULE 2
     безусловно отправлял шаблон в pig — словарь cops не проверялся вовсе).
  2. «Мусоровоз» → cops — полицейский фургон (поправка владельца, словарь cops).
  3. «Черный/Белый/Серый транспорт» → bus — перемещение транспорта (поправка
     владельца, «транспорт» добавлен в словарь bus).
  4. «Тцк (пешие)» → pig (явный словарный матч, «тцк» в словаре pig).
  5. «Блокпост» → traffic (HARD RULE 1 приоритетнее словарной классификации).
  6. Описание не участвует: слой определяется заголовком, не описанием.
  7. Обычные (нешаблонные) сообщения не изменили поведение.
"""

import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("mawo_pymorphy3")

ROOT = Path(__file__).resolve().parent.parent

if "parser" not in sys.modules:
    _pkg = types.ModuleType("parser")
    _pkg.__path__ = [str(ROOT / "parser")]
    sys.modules["parser"] = _pkg

if "nlp_processor" not in sys.modules:
    _pkg = types.ModuleType("nlp_processor")
    _pkg.__path__ = [str(ROOT / "nlp_processor")]
    sys.modules["nlp_processor"] = _pkg

from nlp_processor.morphology import Morphology       # noqa: E402
from nlp_processor.layer_classifier import LayerClassifier  # noqa: E402
from nlp_processor.word_tokenizer import tokenize      # noqa: E402
from common.text_preprocessor import preprocess_light, strip_tail  # noqa: E402


@pytest.fixture(scope="module")
def classifier():
    return LayerClassifier(Morphology())


def _classify(classifier, text):
    pre = preprocess_light(strip_tail(text))
    toks = tokenize(pre)
    lemmas = classifier._morph.lemmatize_tokens(toks)
    return classifier.classify(lemmas, raw_text=pre)


# ===========================================================================
# Заголовки шаблонных пинов (реальные из экспортов)
# ===========================================================================

@pytest.mark.parametrize("title", [
    "📍 Полиция (пешие) 🏠 Адрес: 21, Пантелеймоновская улица, Центр",
    "📌 Полиция (пешие) 🏠 Адрес: 23, Малая Арнаутская улица, Отрада",
])
def test_template_police_title_is_cops(classifier, title):
    """REG: заголовок «Полиция (пешие)» — точный тип события → cops,
    а не pig-фоллбек (старое поведение HARD RULE 2)."""
    text = title + " 📝 Описание: 4 пеших с собакой"
    assert _classify(classifier, text) == "cops"


@pytest.mark.parametrize("title", [
    "📍 Мусоровоз 🏠 Адрес: Семенова улица, Жилой комплекс «Ильичевский Рив’ера»",
    "📍 Мусоровоз 🏠 Адрес: 14, Ралли улица, Средний Фонтан",
])
def test_template_musorovoz_is_cops(classifier, title):
    """«Мусоровоз» — полицейский фургон (поправка владельца) → cops."""
    text = title + " 📝 Описание: много мусорни и черных, скорее всего тцк"
    assert _classify(classifier, text) == "cops"


@pytest.mark.parametrize("title", [
    "📍 Тцк (пешие) 🏠 Адрес: АТБ-Маркет, улица Василия Стуса, Дальние Мельницы",
])
def test_template_tck_title_is_pig(classifier, title):
    """«Тцк (пешие)» — пешие сотрудники ТЦК → pig (явный словарный матч)."""
    text = title + " 📝 Описание: В составе могут быть и ТЦК и полиция"
    assert _classify(classifier, text) == "pig"


@pytest.mark.parametrize("title", [
    "📍 Серый транспорт ТЦК 🏠 Адрес: 55, Сергея Шелухина улица",
    "📍 Черный транспорт 🏠 Адрес: Кильцева улица, ОК ЖСТ «Морське», Аркадия",
    "📍 Белый транспорт 🏠 Адрес: улица Василия Стуса, Дальние Мельницы",
])
def test_template_transport_title_is_bus(classifier, title):
    """«Черный/Белый/Серый транспорт» — перемещение транспорта → bus
    (поправка владельца; раньше падали в pig-фоллбек)."""
    text = title + " 📝 Описание: Черный бус или легковой авто ТЦК"
    assert _classify(classifier, text) == "bus"


def test_template_checkpoint_title_is_traffic(classifier):
    """«Блокпост» в заголовке — traffic (HARD RULE 1 по лемме заголовка)."""
    text = ("📍 Блокпост 🏠 Адрес: 86, Люстдорфская дорога, 4-й квартал "
            "📝 Описание: В составе могут быть и ТЦК и полиция")
    assert _classify(classifier, text) == "traffic"


def test_template_description_does_not_override_title(classifier):
    """Описание не участвует в классификации шаблона: слой «Мусоровоз»
    определяется заголовком (cops), чужие слова описания («бус», «тцк»)
    не меняют его на bus/pig."""
    text = ("📍 Мусоровоз 🏠 Адрес: Семенова улица "
            "📝 Описание: белый бус тцк во дворе стоит")
    assert _classify(classifier, text) == "cops"


def test_template_bus_title(classifier):
    """Гипотетический заголовок «Бус тцк» → bus (приоритет bus выше pig)."""
    text = "📍 Бус тцк 🏠 Адрес: Балковская улица 📝 Описание: стоит"
    assert _classify(classifier, text) == "bus"


# ===========================================================================
# Обычные (нешаблонные) сообщения — регресс-защита
# ===========================================================================

@pytest.mark.parametrize("text,expected", [
    ("Туристская блокпост менты и нелюди", "traffic"),      # HARD RULE 1
    ("По Ришельевская едет полиция медленно", "cops"),
    ("С Таирова к вам джип с зеленью", "pig"),               # без словарных слов
    ("Коповский Дастер выехал в сторону Бреуса", "pig"),  # лемма «коповский» ≠ «коп»: до-существующий пробел словаря (прод-поведение экспорта 3 id 85; кандидат на добавление «коповский»/«ментовский» в cops)
    ("бп на въезде в Березовку", "traffic"),
])
def test_non_template_unchanged(classifier, text, expected):
    assert _classify(classifier, text) == expected


def test_template_without_markers_still_standard(classifier):
    """Текст с «Адрес:» в свободной форме (не шаблон) — обычная классификация."""
    assert _classify(classifier, "Адрес заправки не помню, но полиция там стоит") == "cops"
