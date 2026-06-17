"""Unit: normalize_locale — нормализация явного client-locale → `ru` | `en` | `None` (ADR-036).

Точка нормализации Form-поля `locale` (POST /v1/projects) в `app/pipeline/language.py`.
Единственный нормативный источник правила — ADR-036 §4 (BCP-47-подобный сабтег):
  1. Регистронезависимо; первый сабтег до разделителя `-` или `_`
     (`ru-RU`/`ru_RU`/`RU` → `ru`; `en-US` → `en`).
  2. Первый сабтег ∈ {`ru`, `en`} → нормализованный код.
  3. Иначе (неподдерживаемый `fr`/`de`/…, пустая строка, пробелы, `None`) → `None`
     (= «locale не передан» → авто-детект, обратносовместимо, НЕ ошибка `422`).

`None` — единственный канал «нет валидного locale». Чистый Python без IO/БД/Claude —
детерминирован, тестируется прямым вызовом (env conftest самодостаточен, правило qa.md).

Покрывает чек-лист ТЗ:
  1. (§4) `ru`/`ru-RU`/`ru_RU`/`RU` → `ru`; `en`/`en-US` → `en`;
          `fr`/`de`/``/None/пробелы → None (fallback, не 422).
  6. неподдерживаемый `fr` → None (→ авто-детект, контр-проверка «не 422»).
"""

from __future__ import annotations

import pytest

from app.pipeline.language import language_from_bcp47, normalize_locale

# --------------------------------------------------------------------------- #
# 1. Поддерживаемые locale → нормализованный код (§4 п.1-2).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # ru — все формы первого сабтега `ru` (регистр + оба разделителя).
        ("ru", "ru"),
        ("ru-RU", "ru"),
        ("ru_RU", "ru"),
        ("RU", "ru"),
        ("Ru", "ru"),
        ("rU", "ru"),
        ("ru-Cyrl-RU", "ru"),  # многосабтеговый BCP-47 — берётся только первый сабтег
        ("RU_ru", "ru"),
        # en — все формы первого сабтега `en`.
        ("en", "en"),
        ("en-US", "en"),
        ("en_US", "en"),
        ("EN", "en"),
        ("En", "en"),
        ("en-GB", "en"),
    ],
)
def test_supported_locale_normalizes_to_code(raw, expected):  # noqa: ANN001
    """Поддерживаемый locale (первый сабтег ru/en, любой регистр/разделитель) → `ru`/`en`
    (ADR-036 §4 п.1-2)."""
    assert normalize_locale(raw) == expected


def test_ru_variants_all_equal():
    """Все ru-формы дают идентичный нормализованный `ru` (детерминизм §4)."""
    assert (
        normalize_locale("ru")
        == normalize_locale("ru-RU")
        == normalize_locale("ru_RU")
        == normalize_locale("RU")
        == "ru"
    )


def test_en_variants_all_equal():
    """Все en-формы дают идентичный нормализованный `en` (детерминизм §4)."""
    assert normalize_locale("en") == normalize_locale("en-US") == normalize_locale("EN") == "en"


# --------------------------------------------------------------------------- #
# 2. Неподдерживаемый / пустой / None → None (§4 п.3 — fallback, НЕ 422).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "fr",  # неподдерживаемый язык (сценарий 6 ТЗ)
        "de",
        "fr-FR",
        "de-DE",
        "es",
        "zh-CN",
        "uk",  # украинский — не из набора ru/en
        "russian",  # не BCP-47-сабтег `ru`
        "english",
        "xx",
        "r",  # частичный
        "e",
    ],
)
def test_unsupported_locale_returns_none(raw):  # noqa: ANN001
    """Неподдерживаемый locale (не ru/en первый сабтег) → None = «не передан» → авто-детект
    (ADR-036 §4 п.3, НЕ ошибка 422)."""
    assert normalize_locale(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "",  # пустая строка
        "   ",  # только пробелы
        "\t",
        "\n",
        " \t \n ",
    ],
)
def test_empty_or_whitespace_returns_none(raw):  # noqa: ANN001
    """Пустая строка / пробелы → None (трактуется как «locale не передан», §4 п.3)."""
    assert normalize_locale(raw) is None


def test_none_returns_none():
    """`None` (поле не передано клиентом) → None — авто-детект, байт-в-байт обратная
    совместимость (ADR-036 §4 п.3 / §9)."""
    assert normalize_locale(None) is None


def test_whitespace_padded_supported_locale_normalizes():
    """Поддерживаемый locale с окружающими пробелами → код (strip перед разбором, §4)."""
    assert normalize_locale("  ru  ") == "ru"
    assert normalize_locale("\ten-US\n") == "en"


# --------------------------------------------------------------------------- #
# 3. Контракт с downstream: результат normalize_locale годен для language_from_bcp47.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("raw", "expected_name"), [("ru", "Russian"), ("en-US", "English")])
def test_normalized_locale_feeds_language_from_bcp47(raw, expected_name):  # noqa: ANN001
    """Нормализованный код (`ru`/`en`) — валидный вход language_from_bcp47 (приоритет-ветка
    _interview, ADR-036 §6): восстанавливает корректную пару без передетекта."""
    code = normalize_locale(raw)
    assert code is not None
    lang = language_from_bcp47(code)
    assert lang.bcp47 == code
    assert lang.name == expected_name


def test_normalize_locale_is_pure_deterministic():
    """Чистая функция: повтор на одном входе даёт идентичный результат (нет state)."""
    assert normalize_locale("ru-RU") == normalize_locale("ru-RU") == "ru"
    assert normalize_locale("fr") is normalize_locale("fr")  # оба None
