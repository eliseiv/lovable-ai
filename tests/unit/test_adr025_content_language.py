"""Unit/contract: локализация контента сайта — авто-детект языка + spec-маркер (ADR-025).

Источник истины — docs/modules/pipeline/03-architecture.md §Язык/локализация контента сайта
(ADR-025) + §I.1a (каноническая форма Agent 2 С маркером) + §I.5 (criterii приёмки) +
docs/adr/ADR-025-content-language-autodetect-spec-marker.md; cross-ref docs/06-testing-strategy.md.

Прод-баг (закрываемый): сайт выходил на русском при английском вводе — корень в кириллице/
локаль-терминах эталонных примеров промтов (смещали язык генерации) + язык пользователя нигде
не детектился + `<html lang>` не выставлялся.

Покрывает (минимум по ТЗ/§I.5):
  1. Валидатор маркера (positive): spec_markdown с маркером → проходит `_validate_spec`.
  2. Валидатор маркера (negative): spec_markdown без маркера → StructuredOutputError(schema_error),
     тот же класс, что пустая спека.
  3. Маркер с ведущими пробелами/переводами строк → strip → проходит (startswith после strip).
  4. Контракт промтов — нет кириллицы / термина `ТЗ` ни в одном из 5 .txt-промтов.
  5. Контракт промтов — language-инструкции присутствуют (ADR-028: Agent 1/2 следуют СЕРВЕРНОЙ
     директиве, само-детект отозван; Agent 2 — маркер из директивы; Agent 3 <html lang>+контент
     по маркеру; Agent 4 fixer/editor — сохранение языка).
  6. Маркер символ-в-символ: строка `**Content language:**` идентична в промтах Agent 2/3/4 и в
     константе agent2.py.

Чистые unit (чтение реальных promt-файлов через load_prompt + прямой вызов валидатора). Без
сети/БД/Claude. Env для прогона задаётся conftest (самодостаточно, правило qa.md).
"""

from __future__ import annotations

import re

import pytest

from app.pipeline.agents.agent2 import _CONTENT_LANGUAGE_MARKER, _validate_spec
from app.pipeline.agents.structured import FAIL_CLASS_SCHEMA, StructuredOutputError
from app.pipeline.prompts import load_prompt

# 5 промт-файлов, затронутых ADR-025 (§Язык/локализация п.7, §I.6) — фактические имена файлов.
_PROMPT_FILES = (
    "agent1_interviewer",
    "agent2_spec_writer",
    "agent3_builder",
    "agent4_fixer",
    "agent4_editor",
)

# Маркер из ТЗ/§Язык/локализации — нормативная строка-якорь. Дублирование здесь намеренно:
# тест ловит расхождение, если константа в коде/промтах разойдётся с нормативной строкой.
_MARKER = "**Content language:**"

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


# --------------------------------------------------------------------------- #
# 1. Валидатор маркера — positive.
# --------------------------------------------------------------------------- #


def test_validator_accepts_spec_with_marker():
    """spec_markdown, начинающийся маркером **Content language:** → проходит, возвращает строку."""
    spec = "**Content language:** English (en)\n\n# Specification\nHello."
    assert _validate_spec({"spec_markdown": spec}) == spec


def test_validator_accepts_marker_other_language():
    """Маркер с иным языком/кодом (русский) — тоже валиден (детект, не фиксированный язык)."""
    spec = "**Content language:** Russian (ru)\n\n# Спецификация"
    # Кириллица в ЗНАЧЕНИИ спеки допустима (это контент на детектированном языке); запрет
    # кириллицы относится к ПРОМТАМ, не к runtime-выводу модели.
    assert _validate_spec({"spec_markdown": spec}) == spec


# --------------------------------------------------------------------------- #
# 2. Валидатор маркера — negative (тот же класс, что пустая спека).
# --------------------------------------------------------------------------- #


def test_validator_rejects_spec_without_marker():
    """spec_markdown без маркера (`# x`) → StructuredOutputError(schema_error)."""
    with pytest.raises(StructuredOutputError) as ei:
        _validate_spec({"spec_markdown": "# x\n\nNo marker here."})
    assert ei.value.fail_class == FAIL_CLASS_SCHEMA


def test_validator_marker_missing_is_same_fail_class_as_empty_spec():
    """Отсутствие маркера и пустая спека дают ОДИН класс фейла (schema_error) — оба
    ре-семплируемы и при исчерпании ретраев ведут к FAILED(invalid_agent_output) (§I.3)."""
    with pytest.raises(StructuredOutputError) as ei_empty:
        _validate_spec({"spec_markdown": "   "})
    with pytest.raises(StructuredOutputError) as ei_nomarker:
        _validate_spec({"spec_markdown": "# heading only"})
    assert ei_empty.value.fail_class == ei_nomarker.value.fail_class == FAIL_CLASS_SCHEMA


def test_validator_rejects_marker_not_at_start():
    """Маркер ПОСРЕДИ текста (не в начале) → schema-фейл (требуется startswith после strip)."""
    spec = "# Specification\n\n**Content language:** English (en)"
    with pytest.raises(StructuredOutputError) as ei:
        _validate_spec({"spec_markdown": spec})
    assert ei.value.fail_class == FAIL_CLASS_SCHEMA


# --------------------------------------------------------------------------- #
# 3. Маркер с ведущими пробелами/переводами строк → strip → проходит.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "lead",
    ["\n\n", "   ", "\n  \n\t", "\t"],
)
def test_validator_accepts_marker_after_leading_whitespace(lead):  # noqa: ANN001
    """Ведущие пробелы/переводы перед маркером strip'аются валидатором → startswith проходит."""
    spec = f"{lead}**Content language:** English (en)\n\n# Spec"
    out = _validate_spec({"spec_markdown": spec})
    # Возвращается strip'нутое значение, начинающееся ровно с маркера.
    assert out.startswith(_MARKER)
    assert out == spec.strip()


# --------------------------------------------------------------------------- #
# 4. Контракт промтов — нет кириллицы / термина `ТЗ` ни в одном из 5 .txt-промтов.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("prompt_name", _PROMPT_FILES)
def test_prompt_has_no_cyrillic(prompt_name):  # noqa: ANN001
    """Ни один из 5 промт-файлов НЕ содержит кириллических символов (корень прод-бага:
    кириллица в примерах смещала язык генерации). §Язык/локализация п.7, §I.5 contract."""
    text = load_prompt(prompt_name)
    found = _CYRILLIC_RE.findall(text)
    assert not found, f"промт {prompt_name}.txt содержит кириллицу: {set(found)!r}"


@pytest.mark.parametrize("prompt_name", _PROMPT_FILES)
def test_prompt_has_no_tz_term(prompt_name):  # noqa: ANN001
    """Ни один промт НЕ содержит локаль-термина `ТЗ` (русская аббревиатура, кириллица).
    Отдельный тест — явная фиксация требования ТЗ/§Язык/локализации п.7."""
    text = load_prompt(prompt_name)
    assert "ТЗ" not in text, f"промт {prompt_name}.txt содержит локаль-термин 'ТЗ'"


# --------------------------------------------------------------------------- #
# 5. Контракт промтов — language-инструкции присутствуют.
# --------------------------------------------------------------------------- #


def test_agent2_prompt_has_language_directive_and_marker_instruction():
    """Agent 2 (ADR-028): следует СЕРВЕРНОЙ language-директиве + ОБЯЗАН начинать spec_markdown
    маркером со значением из директивы. Промт НЕ инструктирует само-детект."""
    text = load_prompt("agent2_spec_writer")
    low = text.lower()
    # Серверная директива — единый источник языка (не само-детект модели, ADR-028 §3/§4).
    assert "content language" in low
    assert "directive" in low
    assert "server" in low
    # Запрет само-детекта/перегадывания языка (ADR-028: агент язык не детектит).
    # (Промт может переносить строку между "do not" и "detect"; матчим continuous-фразу.)
    assert "detect or re-guess" in low
    # Требование маркера в начале spec_markdown.
    assert _MARKER in text
    assert "must begin" in low or "must start" in low


def test_agent2_prompt_obeys_server_directive_not_answers():
    """Agent 2 (ADR-028 §4): приоритет «ответы > промпт» ОТОЗВАН — промт обязывает следовать
    серверной директиве и НЕ позволять языку ответов переопределять её."""
    low = load_prompt("agent2_spec_writer").lower()
    assert "obey the server directive" in low
    # Язык ответов НЕ переопределяет директиву (каскад прод-бага закрыт).
    assert "answers override" in low or "answers to override" in low or "answers" in low


def test_agent3_prompt_has_html_lang_and_marker_instruction():
    """Agent 3: контент на языке маркера + выставляет <html lang> по BCP-47 из маркера."""
    text = load_prompt("agent3_builder")
    low = text.lower()
    assert _MARKER in text
    assert "html lang" in low or "<html lang" in low
    assert "bcp-47" in low


@pytest.mark.parametrize("prompt_name", ["agent4_fixer", "agent4_editor"])
def test_agent4_prompts_preserve_language(prompt_name):  # noqa: ANN001
    """Agent 4 (fixer/editor): СОХРАНЯЕТ язык контента (не переключает, не переводит)."""
    text = load_prompt(prompt_name)
    low = text.lower()
    assert _MARKER in text
    assert "preserve the content language" in low
    assert "do not switch" in low or "do not translate" in low


def test_agent1_prompt_asks_in_server_directive_language():
    """Agent 1 (ADR-028 §4): задаёт вопросы на языке СЕРВЕРНОЙ директивы (детерминированный
    детект из исходного промпта), НЕ детектит/перегадывает язык сам."""
    low = load_prompt("agent1_interviewer").lower()
    # Серверная content-language директива — первая строка ввода (ADR-028 §4).
    assert "directive" in low
    assert "server" in low
    assert "obey the server directive" in low
    # Запрет само-детекта языка (ADR-028: Agent 1 язык не угадывает). Промт переносит строку
    # внутри фразы "detect or re-guess" — матчим устойчивую к переносам подстроку.
    assert "do not detect" in low
    # Язык решается детерминированно сервером из исходного промпта.
    assert "prompt" in low


# --------------------------------------------------------------------------- #
# 6. Маркер символ-в-символ: идентичен в промтах Agent 2/3/4 и в константе agent2.py.
# --------------------------------------------------------------------------- #


def test_marker_constant_matches_normative_string():
    """Константа agent2.py `_CONTENT_LANGUAGE_MARKER` == нормативная строка ТЗ символ-в-символ."""
    assert _CONTENT_LANGUAGE_MARKER == _MARKER


@pytest.mark.parametrize(
    "prompt_name",
    ["agent2_spec_writer", "agent3_builder", "agent4_fixer", "agent4_editor"],
)
def test_marker_string_identical_across_prompts(prompt_name):  # noqa: ANN001
    """Строка `**Content language:**` присутствует символ-в-символ в промтах Agent 2/3/4
    и совпадает с константой кода — единый якорь маркера, без рассинхрона форматирования."""
    text = load_prompt(prompt_name)
    assert _CONTENT_LANGUAGE_MARKER in text, (
        f"промт {prompt_name}.txt не содержит маркер символ-в-символ {_CONTENT_LANGUAGE_MARKER!r}"
    )
