"""Unit/contract: детерминированный серверный детект языка из исходного промпта (ADR-028).

Ревизует ADR-025: язык контента сайта определяется НЕ LLM-само-детектом (недетерминирован,
прод-баг «один русский промпт → то русские, то английские вопросы»), а детерминированной
серверной script-эвристикой (`app/pipeline/language.py`) по доминирующему Unicode-script
текста `project.prompt`. Результат фиксируется один раз в `generation_jobs.content_language`
и инжектируется сервером в language-директиву Agent 1 / Agent 2.

Источник истины критериев приёмки — docs/modules/pipeline/03-architecture.md §Язык/локализация
(Критерии приёмки qa) + docs/adr/ADR-028-deterministic-source-prompt-language-detection.md;
cross-ref docs/06-testing-strategy.md (Детерминизм локализации).

Покрывает (нормативный чек-лист §Критерии приёмки):
  1. unit детект ДЕТЕРМИНИРОВАН (обязателен): кириллица → ru на КАЖДОМ из N повторов
     (не флапает, побайтово стабилен); латиница → en; результат не зависит от LLM/IO.
  2. unit fallback (обязателен): нет буквенных символов (цифры/emoji/пунктуация) ИЛИ
     смешанный script без строгого большинства > 50% → en детерминированно; ровно 50/50 → en.
  3. unit script-маппинг: доминирующая кириллица с латинскими вкраплениями ниже порога → ru;
     доминирующая латиница с кириллическими вкраплениями ниже порога → en.
  4. language_from_bcp47 (crash-resume): восстановление DetectedLanguage из сохранённого
     content_language без передетекта; неизвестный код → fallback en.
  5. DetectedLanguage.marker_value: каноническая форма `<язык> (<bcp-47>)` для директивы/маркера.
  6. contract (директива инжектируется, не самодетект): _build_user_content Agent 1/Agent 2
     несёт серверную директиву со значением языка; маркер Agent 2 = значение директивы.

Чистые unit (прямой вызов detect_language/хелперов сборки ввода). Без сети/БД/Claude —
серверный детект детерминирован и не зависит от LLM (тестируется без мока). Env для прогона
задаётся conftest (самодостаточно, правило qa.md).
"""

from __future__ import annotations

import pytest

from app.pipeline.agents.agent1 import _build_user_content as build_agent1_input
from app.pipeline.agents.agent2 import _build_user_content as build_agent2_input
from app.pipeline.language import (
    DetectedLanguage,
    detect_language,
    language_from_bcp47,
)

# Число повторов для проверки детерминизма (не флапает на N прогонах) — §Критерии приёмки
# «≥N повторов идентичны».
_N_REPEATS = 50


# --------------------------------------------------------------------------- #
# 1. Детект ДЕТЕРМИНИРОВАН — кириллица → ru, латиница → en, стабильно на N повторах.
# --------------------------------------------------------------------------- #


def test_cyrillic_prompt_detects_ru():
    """Русский (кириллица) промпт → ru (Cyrillic → Russian, ADR-028 §1 таблица)."""
    result = detect_language("Сделай лендинг для кофейни с меню и формой брони")
    assert result.bcp47 == "ru"
    assert result.name == "Russian"


def test_latin_prompt_detects_en():
    """Английский (латиница) промпт → en (Latin → English, ADR-028 §1 таблица)."""
    result = detect_language("Landing page for a coffee shop with menu and booking form")
    assert result.bcp47 == "en"
    assert result.name == "English"


def test_cyrillic_detection_is_deterministic_across_repeats():
    """Кириллический промпт → ru на КАЖДОМ из N повторов, побайтово идентично (закрывает
    корень прод-бага «один промпт → разный язык»: детект не флапает)."""
    prompt = "Создай сайт-портфолио фотографа с галереей и контактами"
    results = [detect_language(prompt) for _ in range(_N_REPEATS)]
    assert all(r == results[0] for r in results)
    assert all(r.bcp47 == "ru" for r in results)
    # Побайтовая стабильность marker_value (значение директивы/маркера).
    assert len({r.marker_value for r in results}) == 1


def test_latin_detection_is_deterministic_across_repeats():
    """Латинский промпт → en на КАЖДОМ из N повторов (детерминизм, не флапает)."""
    prompt = "Build a personal blog about hiking and travel photography"
    results = [detect_language(prompt) for _ in range(_N_REPEATS)]
    assert all(r == results[0] for r in results)
    assert all(r.bcp47 == "en" for r in results)


def test_detection_is_pure_no_side_effects():
    """Детект — чистая функция: повтор на ОДНОМ объекте строки даёт тот же результат
    (нет внутреннего state/мутаций). Гарантия детерминизма ADR-028 §1."""
    prompt = "Тестовый промпт на русском языке"
    first = detect_language(prompt)
    second = detect_language(prompt)
    # frozen dataclass — равенство по значению; повтор детекта стабилен (нет внутреннего state).
    assert first == second
    assert first.bcp47 == second.bcp47 == "ru"


# --------------------------------------------------------------------------- #
# 2. Fallback — детерминированный en при отсутствии букв / смешанном script / 50-50.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "prompt",
    [
        "12345 67890",  # только цифры
        "!@#$ %^&* ()_+",  # только пунктуация
        "123 !!! ... 456",  # цифры + пунктуация
        "",  # пустая строка
        "   \n\t  ",  # только пробелы
        "🚀🎨🔥💡✨",  # только emoji
        "2024 — 100% 🚀",  # цифры + emoji + пунктуация, без букв
    ],
)
def test_no_letters_fallback_en(prompt):  # noqa: ANN001
    """Промпт без буквенных символов (цифры/emoji/пунктуация/пусто) → en детерминированно
    (ADR-028 §5 fallback)."""
    result = detect_language(prompt)
    assert result.bcp47 == "en"
    assert result.name == "English"


def test_exact_5050_mixed_script_fallback_en():
    """Ровно 50/50 кириллица/латиница → НЕ строгое большинство (> 50%) → fallback en
    (ADR-028 §5: при равной доле всегда en)."""
    # 4 кириллических + 4 латинских буквы = ровно 50/50.
    result = detect_language("абвг abcd")
    assert result.bcp47 == "en"


def test_mixed_no_strict_majority_fallback_en():
    """Смешанный script без строгого большинства ни одного → fallback en (ADR-028 §5).

    Три script-группы примерно поровну: ни Cyrillic, ни Latin не превышают 50%.
    """
    # 3 cyr + 3 latin + 3 greek: доминирующий (любой) = 3/9 = 33% < 50% → fallback.
    result = detect_language("абв abc αβγ")
    assert result.bcp47 == "en"


def test_5050_is_deterministic_across_repeats():
    """50/50 граничный кейс → en детерминированно на N повторов (без недетерминизма в
    граничных случаях, ADR-028 §5)."""
    prompt = "абвг abcd"
    results = [detect_language(prompt) for _ in range(_N_REPEATS)]
    assert all(r.bcp47 == "en" for r in results)


# --------------------------------------------------------------------------- #
# 3. Script-маппинг — доминирующий script со строгим большинством, вкрапления ниже порога.
# --------------------------------------------------------------------------- #


def test_dominant_cyrillic_with_minor_latin_detects_ru():
    """Доминирующая кириллица с латинскими вкраплениями НИЖЕ порога (> 50%) → ru
    (бренды/термины латиницей не сбивают язык, ADR-028 §1/§5)."""
    # Много кириллицы + редкое латинское слово (бренд) — кириллица > 50%.
    result = detect_language(
        "Создай современный сайт для нашей компании Acme с разделами о услугах и контактах"
    )
    assert result.bcp47 == "ru"


def test_dominant_latin_with_minor_cyrillic_detects_en():
    """Доминирующая латиница с кириллическими вкраплениями НИЖЕ порога → en (ADR-028 §1/§5)."""
    result = detect_language(
        "Build a clean modern website for our company with sections about услуги and contacts"
    )
    assert result.bcp47 == "en"


def test_single_cyrillic_letter_majority_detects_ru():
    """Минимальный кейс: один кириллический буквенный символ (100% буквенных) → ru."""
    assert detect_language("я").bcp47 == "ru"


def test_single_latin_letter_majority_detects_en():
    """Минимальный кейс: один латинский буквенный символ (100% буквенных) → en."""
    assert detect_language("a").bcp47 == "en"


def test_just_over_half_cyrillic_detects_ru():
    """Кириллица чуть выше порога (3 из 5 букв = 60% > 50%) → ru (строгое большинство)."""
    # 3 cyr + 2 latin = 60% Cyrillic.
    result = detect_language("абв xy")
    assert result.bcp47 == "ru"


# --------------------------------------------------------------------------- #
# 4. language_from_bcp47 — crash-resume восстановление без передетекта.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("code", "expected_name"),
    [("ru", "Russian"), ("en", "English")],
)
def test_language_from_bcp47_known_codes(code, expected_name):  # noqa: ANN001
    """Восстановление DetectedLanguage из сохранённого content_language (crash-resume §2):
    известный код → корректная пара без передетекта."""
    result = language_from_bcp47(code)
    assert result.bcp47 == code
    assert result.name == expected_name


@pytest.mark.parametrize("code", ["", "de", "fr", "uk", "xx", "RU", "zzz"])
def test_language_from_bcp47_unknown_code_fallback_en(code):  # noqa: ANN001
    """Неизвестный/невалидный content_language → fallback en (директива всегда валидна,
    ADR-028 §2): не падает, восстанавливает безопасный дефолт."""
    result = language_from_bcp47(code)
    assert result.bcp47 == "en"
    assert result.name == "English"


def test_detect_then_resume_roundtrip_ru():
    """Roundtrip: detect_language(ru-промпт).bcp47 → language_from_bcp47 → та же пара.
    Моделирует «детект на interview → сохранение в БД → чтение на spec без передетекта»."""
    detected = detect_language("Русский промпт для сайта")
    resumed = language_from_bcp47(detected.bcp47)
    assert resumed == detected


# --------------------------------------------------------------------------- #
# 5. DetectedLanguage.marker_value — каноническая форма директивы/маркера.
# --------------------------------------------------------------------------- #


def test_marker_value_format_ru():
    """marker_value = `<язык> (<bcp-47>)` для ru (формат маркера/директивы ADR-028 §5)."""
    assert DetectedLanguage(bcp47="ru", name="Russian").marker_value == "Russian (ru)"


def test_marker_value_format_en():
    """marker_value = `English (en)` для en (формат маркера/директивы)."""
    assert DetectedLanguage(bcp47="en", name="English").marker_value == "English (en)"


def test_detected_language_is_frozen():
    """DetectedLanguage иммутабелен (frozen) — нельзя случайно перетереть язык после детекта."""
    lang = detect_language("Landing page")
    with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError
        lang.bcp47 = "ru"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 6. Contract — серверная директива инжектируется в собранный ввод агентов (не самодетект).
# --------------------------------------------------------------------------- #


def test_agent1_input_carries_server_directive_value():
    """Agent 1 (contract §4): собранный ввод несёт серверную директиву со значением языка
    из детекта (проверка реального user_content, не слепого мока). en."""
    language = detect_language("Landing page for a startup")
    content = build_agent1_input("Landing page for a startup", language)
    # Директива со значением marker_value, как в ADR-028 §4 / pipeline §Язык/локализация п.4.
    assert "Generate all questions in English (en)." in content
    # Исходный промпт также присутствует (модель видит текст целиком).
    assert "Landing page for a startup" in content


def test_agent1_input_carries_ru_directive_for_cyrillic_prompt():
    """Agent 1 (contract): кириллический промпт → директива `Russian (ru)` в собранном вводе
    (детерминированное серверное значение, а не самодетект модели)."""
    prompt = "Сделай сайт для пекарни"
    language = detect_language(prompt)
    content = build_agent1_input(prompt, language)
    assert "Generate all questions in Russian (ru)." in content


def test_agent2_input_carries_directive_and_marker_value():
    """Agent 2 (contract §4): собранный ввод несёт серверную директиву И требование начать
    spec_markdown маркером `**Content language:**` со ЗНАЧЕНИЕМ директивы (en)."""
    language = detect_language("Portfolio website for a designer")
    content = build_agent2_input(
        "Portfolio website for a designer",
        [("What sections?", "Home and works")],
        language,
    )
    assert "Generate all user-facing content in English (en)." in content
    # Маркер с значением content_language (а не результат детекта модели), ADR-028 §4/§5.
    assert "**Content language:** English (en)" in content


def test_agent2_input_carries_ru_marker_for_cyrillic_prompt():
    """Agent 2 (contract): кириллический промпт → директива и маркер несут `Russian (ru)`."""
    prompt = "Корпоративный сайт для юридической фирмы"
    language = detect_language(prompt)
    content = build_agent2_input(prompt, [("Вопрос?", "Ответ")], language)
    assert "Generate all user-facing content in Russian (ru)." in content
    assert "**Content language:** Russian (ru)" in content


def test_agent2_marker_value_matches_content_language_not_self_detect():
    """Contract (§Критерии приёмки): значение маркера в директиве Agent 2 = content_language
    (серверный детект), даже когда ответы пользователя на ДРУГОМ языке. Закрывает каскад
    прод-бага (язык ответов НЕ переопределяет директиву, ADR-028 §4)."""
    # Промпт русский → content_language=ru; ответ пользователя по-английски (каскад-кейс).
    prompt = "Создай сайт для кофейни"
    language = detect_language(prompt)  # ru
    content = build_agent2_input(
        prompt,
        [("Какой стиль?", "modern minimalist with warm colors")],  # ответ на английском
        language,
    )
    # Директива и маркер — ru (из исходного промпта), НЕ en (язык ответов не переопределяет).
    assert "Russian (ru)" in content
    assert "English (en)" not in content
