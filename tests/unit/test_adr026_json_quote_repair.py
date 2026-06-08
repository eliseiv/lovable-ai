"""Unit: устойчивость structured-output к неэкранированным двойным кавычкам (ADR-026).

Defense-in-depth уровень 2 (docs/modules/pipeline/03-architecture.md §I.2 «Repair-fallback» +
§I.5 «Критерии приёмки (qa)»; docs/06-testing-strategy.md → Unit «Structured-output агентов →
repair неэкранированных кавычек»). Воспроизводит прод-баг 2026-06-08: Agent 1 (interviewer)
вернул вопрос с примером в неэкранированных внутренних кавычках
(`… (e.g., "Where every cup tells a story")?`) → строгий json.loads → ValueError → FAILED.

ADR-026 чинит это собственной узкой эвристикой `_repair_unescaped_inner_quotes` /
`_is_legal_string_close` поверх той же машины строк/экранирования, что `_first_balanced_json`.
Repair применяется СТРОГО как fallback (только при JSONDecodeError строгого парса); валидный JSON
идёт прямым путём байт-в-байт.

Сценарии (ТЗ §I.5, обязательные):
  1. Реальный кейс прод-бага (главный) — repair чинит внутренние кавычки, значение сохранено.
  2. Repair строго fallback — на валидном JSON результат побайтово эквивалентен json.loads.
  3. Узость (negative) — trailing comma / одинарные кавычки / комментарии НЕ чинятся.
  4. Граница look-ahead — ложное закрытие перед `,` → честный ValueError, не порча данных.
  5. Уже-экранированные `\"` не задваиваются.
  6. Промт-инструкция кавычек в STRICT_JSON_SUFFIX и в собранном промте каждого из 4 агентов.
  7. Контракт не ослаблен — доменная validate-колбэк применяется ПОСЛЕ repair (schema-фейл
     не маскируется); parse/schema-классы и bounded-retry работают как раньше.
  8. Вложенность/идемпотентность — несколько внутренних кавычек в разных полях; повтор стабилен.
"""

from __future__ import annotations

import json

import pytest

from app.pipeline.agents.structured import (
    FAIL_CLASS_PARSE,
    FAIL_CLASS_SCHEMA,
    STRICT_JSON_SUFFIX,
    StructuredOutputError,
    _is_legal_string_close,
    _repair_unescaped_inner_quotes,
    append_strict_json,
    extract_json,
)
from app.pipeline.prompts import load_prompt

# asyncio_mode=auto (pyproject) — синхронные extract_json-тесты остаются синхронными.

# Реальный raw-ответ из прод-бага 2026-06-08 (§I.5, главный кейс): неэкранированные внутренние
# двойные кавычки вокруг примера-цитаты внутри string value поля "text".
PROD_BUG_RAW = (
    '{"questions":[{"position":1,"text":"What is the short tagline '
    '... (e.g., "Where every cup tells a story")?","kind":"free_text"}]}'
)


# --------------------------------------------------------------------------- #
# (1) Реальный кейс прод-бага (главный) — repair чинит, значение сохранено.
# --------------------------------------------------------------------------- #


def test_prod_bug_unescaped_inner_quotes_repaired_no_valueerror():
    """Главный кейс §I.5: реальный прод-ответ с неэкранированными внутренними `"` →
    extract_json возвращает валидную структуру, НЕ бросает ValueError."""
    result = extract_json(PROD_BUG_RAW)
    assert isinstance(result, dict)
    assert isinstance(result["questions"], list)
    q0 = result["questions"][0]
    assert q0["position"] == 1
    assert q0["kind"] == "free_text"
    # Кавычки сохранены В ЗНАЧЕНИИ (подстрока примера присутствует целиком).
    assert "Where every cup tells a story" in q0["text"]


def test_prod_bug_strict_json_loads_actually_fails_first():
    """Подтверждаем, что строгий json.loads на этом входе ДЕЙСТВИТЕЛЬНО падает —
    иначе тест №1 не доказывал бы работу repair-ветви (а не прямого парса)."""
    with pytest.raises(json.JSONDecodeError):
        json.loads(PROD_BUG_RAW)


def test_prod_bug_inner_quotes_present_in_value_not_stripped():
    """Внутренние кавычки `"Where …"` остаются частью значения (не съедены, не обрезаны)."""
    text = extract_json(PROD_BUG_RAW)["questions"][0]["text"]
    # И открывающая, и закрывающая кавычки примера присутствуют как символы значения.
    assert '"Where every cup tells a story"' in text
    assert text.endswith("?")


# --------------------------------------------------------------------------- #
# (2) Repair строго fallback — на валидном JSON результат == строгий json.loads.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        '{"a": 1, "b": [2, 3], "c": "plain string"}',
        '{"text": "already has \\"escaped\\" quotes inside", "n": 5}',
        '[{"q": "first"}, {"q": "second"}]',
        '{"nested": {"deep": {"v": "x"}}, "arr": [true, false, null]}',
        '{"empty": "", "unicode": "caf\\u00e9", "delim": "a, b: c} d] e"}',
    ],
)
def test_valid_json_equivalent_to_strict_json_loads(raw):
    """Repair — строго fallback: на валидном JSON extract_json возвращает то же, что строгий
    json.loads (структурно эквивалентно). Repair-путь не активируется и ничего не искажает."""
    assert extract_json(raw) == json.loads(raw)


def test_valid_json_with_legal_delimiters_in_value_not_corrupted():
    """Строковое значение содержит легально-выглядящие делимитеры (`:`/`,`/`}`/`]`) внутри —
    валидный JSON парсится прямым путём, символы значения не теряются."""
    raw = '{"css": "a { color: red; }", "list": "x, y, z"}'
    out = extract_json(raw)
    assert out == json.loads(raw)
    assert out["css"] == "a { color: red; }"


def test_repair_helper_idempotent_on_already_valid_string():
    """_repair_unescaped_inner_quotes на уже-валидной строке не меняет её (нет ложного
    экранирования легальных кавычек-делимитеров) — побайтовая эквивалентность."""
    valid = '{"a": "plain", "b": "with \\"escaped\\" inner"}'
    assert _repair_unescaped_inner_quotes(valid) == valid


# --------------------------------------------------------------------------- #
# (3) Узость (negative) — repair НЕ чинит trailing comma / одинарные кавычки / комментарии.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "broken",
    [
        '{"a":1,}',  # trailing comma
        "{'a':1}",  # одинарные кавычки как делимитеры
        '{"a": 1, "b": 2,}',  # trailing comma в объекте побольше
        "[1, 2, 3,]",  # trailing comma в массиве
        '{"a": 1 /* comment */}',  # комментарий
        '{"a": 1 // line comment\n}',  # line-комментарий
    ],
)
def test_narrow_repair_does_not_fix_non_quote_breakage(broken):
    """Узость (§I.2 §Границы): repair чинит ТОЛЬКО внутренние двойные кавычки. Trailing comma,
    одинарные кавычки-делимитеры, комментарии — по-прежнему ValueError (parse_error → retry),
    не «молчаливо чинятся»."""
    with pytest.raises(ValueError):
        extract_json(broken)


def test_single_quote_object_not_silently_accepted():
    """Одинарные кавычки как делимитеры объекта — не валидный JSON и repair их не «исправляет»."""
    with pytest.raises(ValueError):
        extract_json("{'key': 'value'}")


# --------------------------------------------------------------------------- #
# (4) Граница look-ahead — ложное закрытие перед `,` → ValueError, НЕ порча данных.
# --------------------------------------------------------------------------- #


def test_lookahead_false_close_before_comma_raises_not_corrupt():
    """`{"text":"he said "hi", bye"}` — внутренняя `"` перед `,` (ложное «легальное закрытие»)
    делает look-ahead неоднозначным → repair оставляет как есть → строгий парс падает →
    ValueError (retry). НЕ возвращается молчаливо «обрезанная»/испорченная структура."""
    raw = '{"text":"he said "hi", bye"}'
    with pytest.raises(ValueError):
        extract_json(raw)


def test_lookahead_does_not_return_truncated_structure():
    """Подтверждаем явно: на патологическом look-ahead-входе extract_json НЕ возвращает
    частичную структуру — поднимается исключение, вызывающий уйдёт в retry (§I.3)."""
    raw = '{"text":"he said "hi", bye"}'
    result = None
    with pytest.raises(ValueError):
        result = extract_json(raw)
    assert result is None  # ни в каком виде структура не «протекла»


@pytest.mark.parametrize(
    "pos_input,expected",
    [
        # после `"` (поз. 0) сразу структурный символ → легальное закрытие
        (('":next', 1), True),
        (('",x', 1), True),
        (('"}', 1), True),
        (('"]', 1), True),
        (('"   :', 1), True),  # пробелы перед `:` тоже легально
        (('"', 1), True),  # EOF после `"` → легальное закрытие
        # после `"` идёт непробельный неструктурный символ → внутренняя кавычка
        (('"Where', 1), False),
        (('")', 1), False),
        (('"5', 1), False),
    ],
)
def test_is_legal_string_close_lookahead_table(pos_input, expected):
    """_is_legal_string_close: `"`-закрытие легально только перед `:`/`,`/`}`/`]`/EOF
    (после опц. whitespace); иначе — внутренняя кавычка содержимого (§I.2 look-ahead)."""
    text, pos = pos_input
    assert _is_legal_string_close(text, pos) is expected


# --------------------------------------------------------------------------- #
# (5) Уже-экранированные `\"` не задваиваются.
# --------------------------------------------------------------------------- #


def test_already_escaped_quotes_not_doubled():
    """Вход с уже корректным `\\"` парсится без изменения значения (escape не задваивается)."""
    raw = '{"text": "say \\"hi\\" now"}'
    out = extract_json(raw)
    assert out == {"text": 'say "hi" now'}
    # Прямой путь (валидный JSON) — байт-в-байт со строгим парсом.
    assert out == json.loads(raw)


def test_repair_preserves_existing_escapes_on_broken_input():
    """Смешанный вход: одно поле уже с корректным `\\"`, другое — с неэкранированной внутренней
    `"`. Repair чинит только сломанное, существующий escape не трогает/не задваивает."""
    raw = '{"ok": "pre \\"esc\\" post", "bad": "inner "quote" here"}'
    out = extract_json(raw)
    assert out["ok"] == 'pre "esc" post'
    assert '"quote"' in out["bad"]


# --------------------------------------------------------------------------- #
# (6) Промт-инструкция кавычек — STRICT_JSON_SUFFIX + собранный промт каждого из 4 агентов.
# --------------------------------------------------------------------------- #

# 5 промт-файлов 4 агентов (Agent 4 несёт fixer + editor) — §I.6.
_AGENT_PROMPT_NAMES = [
    "agent1_interviewer",
    "agent2_spec_writer",
    "agent3_builder",
    "agent4_fixer",
    "agent4_editor",
]


def test_strict_suffix_has_escape_instruction():
    """STRICT_JSON_SUFFIX содержит нормативную инструкцию про экранирование `\\"` внутри
    string values + рекомендацию одинарных/типографских кавычек для примеров (§I.1, ADR-026)."""
    # Обязательное требование экранировать двойную кавычку как \".
    assert '\\"' in STRICT_JSON_SUFFIX
    assert "escap" in STRICT_JSON_SUFFIX.lower()
    # Рекомендация одинарных кавычек для примеров.
    assert "single quote" in STRICT_JSON_SUFFIX.lower()
    # Рекомендация типографских кавычек (“ ”).
    assert "“" in STRICT_JSON_SUFFIX and "”" in STRICT_JSON_SUFFIX


@pytest.mark.parametrize("prompt_name", _AGENT_PROMPT_NAMES)
def test_each_agent_assembled_prompt_carries_escape_instruction(prompt_name):
    """Собранный системный промт КАЖДОГО из 4 агентов (через append_strict_json над его
    промт-файлом) несёт инструкцию про экранирование `\\"` — единая формулировка прокинута во
    все агенты независимо от индивидуальных промт-файлов (§I.5 промт-инструкция кавычек)."""
    assembled = append_strict_json(load_prompt(prompt_name))
    assert '\\"' in assembled
    assert "escap" in assembled.lower()
    assert "single quote" in assembled.lower()
    # Суффикс прикреплён в конец (контракт append_strict_json).
    assert assembled.endswith(STRICT_JSON_SUFFIX)


# --------------------------------------------------------------------------- #
# (7) Контракт не ослаблен — доменная validate применяется ПОСЛЕ repair; классы/retry как раньше.
# --------------------------------------------------------------------------- #


def test_domain_validate_applies_after_repair_schema_fail_not_masked():
    """После repair доменная validate-колбэк применяется как обычно: синтаксически-починенная,
    но содержательно-невалидная структура → schema-фейл НЕ маскируется (§I.5 пункт «г»).

    Моделируем то, что делает run_structured_agent: extract_json (с repair) даёт структуру,
    затем validate её отвергает по доменному правилу → schema_error."""
    structure = extract_json(PROD_BUG_RAW)  # repair-путь, валидная форма

    def _validate(d):  # noqa: ANN001, ANN202
        # Доменное правило: требуем ключ "spec_markdown" (его в данных нет) → schema-фейл.
        if "spec_markdown" not in d:
            raise StructuredOutputError("missing spec_markdown", fail_class=FAIL_CLASS_SCHEMA)
        return d

    with pytest.raises(StructuredOutputError) as ei:
        _validate(structure)
    assert ei.value.fail_class == FAIL_CLASS_SCHEMA


def test_repaired_structure_passes_domain_validate_when_valid():
    """Если доменная validate ПРИНИМАЕТ починенную структуру — она возвращается без фейла
    (repair не ломает последующую валидацию валидной формы)."""
    structure = extract_json(PROD_BUG_RAW)

    def _validate(d):  # noqa: ANN001, ANN202
        if not d.get("questions"):
            raise StructuredOutputError("no questions", fail_class=FAIL_CLASS_SCHEMA)
        return d["questions"]

    assert _validate(structure)[0]["position"] == 1


def test_unrepairable_input_still_parse_error_class():
    """Если repair не дал валидного JSON (узкость) — класс фейла прежний (parse_error через
    extract_json → ValueError). Repair не вводит нового класса/reason-кода (§I.2 инвариант)."""
    # Одинарные кавычки — repair их не трогает, json.loads повторно падает → ValueError.
    with pytest.raises(ValueError):
        extract_json("{'a': 'b'}")
    # И на trailing-comma — тоже parse_error-семантика (ValueError из extract_json).
    with pytest.raises(ValueError):
        extract_json('{"a":1,}')


def test_extract_json_repair_works_inside_code_fence():
    """Repair совместим со снятием markdown-фенса (§I.2): ответ модели в ```json ... ``` с
    неэкранированными кавычками внутри → фенс снят, repair применён, структура извлечена."""
    raw = "```json\n" + PROD_BUG_RAW + "\n```"
    out = extract_json(raw)
    assert "Where every cup tells a story" in out["questions"][0]["text"]


# --------------------------------------------------------------------------- #
# (8) Вложенность / идемпотентность — несколько внутренних кавычек; повтор стабилен.
# --------------------------------------------------------------------------- #


def test_multiple_inner_quotes_across_different_fields():
    """Неэкранированные внутренние кавычки в РАЗНЫХ полельных значениях одного объекта чинятся
    все; значения сохранены (вложенность кейсов §I.5 пункт 8)."""
    raw = '{"a":"first "alpha" example","b":"second "beta" example","c":"plain no quotes"}'
    out = extract_json(raw)
    assert '"alpha"' in out["a"]
    assert '"beta"' in out["b"]
    assert out["c"] == "plain no quotes"


def test_inner_quotes_in_nested_array_object():
    """Внутренние кавычки в значении внутри вложенного массива объектов чинятся (структурная
    вложенность не ломает машину состояний repair)."""
    raw = '{"items":[{"label":"see "this" item"},{"label":"and "that" one"}],"count":2}'
    out = extract_json(raw)
    assert out["count"] == 2
    assert '"this"' in out["items"][0]["label"]
    assert '"that"' in out["items"][1]["label"]


def test_repair_idempotent_on_already_repaired_string():
    """Идемпотентность: прогон extract_json на УЖЕ-чиненной (валидной) сериализации стабилен —
    повторный парс даёт ту же структуру, без дополнительного искажения."""
    first = extract_json(PROD_BUG_RAW)
    # Сериализуем обратно (теперь корректный JSON с экранированными кавычками) и парсим снова.
    reserialized = json.dumps(first)
    second = extract_json(reserialized)
    assert second == first
    # И прямой re-extract того же исходника детерминирован.
    assert extract_json(PROD_BUG_RAW) == first


def test_repaired_serialization_roundtrips_through_strict_json():
    """Результат repair, будучи сериализован json.dumps, проходит СТРОГИЙ json.loads без
    repair-ветви (доказывает, что repair произвёл синтаксически-корректный JSON)."""
    out = extract_json(PROD_BUG_RAW)
    dumped = json.dumps(out)
    assert json.loads(dumped) == out  # строгий парс, repair не нужен


def test_parse_fail_class_constant_unchanged():
    """Sanity: класс parse_error существует и не переименован (контракт §I.3 не ослаблен)."""
    assert FAIL_CLASS_PARSE == "parse_error"
    assert FAIL_CLASS_SCHEMA == "schema_error"
