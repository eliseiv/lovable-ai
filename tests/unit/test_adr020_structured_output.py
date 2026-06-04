"""Unit: надёжный structured-output всех 4 агентов (ADR-020 revised, docs pipeline §I / §I.5).

REVISED 2026-06-04 (ADR-020 revised): форсированный tool-use ОТОЗВАН (несовместим с
thinking → HTTP 400, 100% отказ). Нормативный механизм — ТЕКСТОВЫЙ режим (`thinking=adaptive`
+ `output_config={effort}`, БЕЗ `tools`/`tool_choice`) + строгий системный промт + `extract_json`
из `block.text` + bounded retry. Фейк-клиенты здесь возвращают ТЕКСТ (block.text); структура
извлекается через extract_json (НЕ из tool_use.input).

Покрывает критерии приёмки §I.5 + budget/wall-clock между ретраями (06-testing-strategy §Unit
«Structured-output агентов»). Единый слой app/pipeline/agents/structured.py — тестируется напрямую
(run_structured_agent / extract_json) + через обёртки агентов (agent3/agent4) с фейк текстовым
клиентом (без сети). Хуки before_call/after_call/on_attempt_failure — как инъектирует task-слой.

Сценарии (ТЗ §I.5 + budget/wall-clock):
  1. Регрессия на fence: ```json {…}``` и ``` {…}``` (без tag) → extract_json без ValueError;
     скобки {} внутри строковых литералов JSON не сбивают баланс.
  2. Текстовый режим: структура из block.text через extract_json (НЕ из tool_use.input);
     строгий JSON-суффикс добавлен к системному промту (append_strict_json).
  3. Bounded retry: parse/schema-фейл ретраится до AGENT_OUTPUT_MAX_RETRIES; каждый retry →
     отдельный after_call (llm_usage); before_call ПЕРЕД каждым вызовом включая ретраи.
  4. Budget/wall-clock между ретраями: PreCallGuardTripped перед retry-вызовом пробрасывается
     (НЕ проглатывается как schema-фейл).
  5. Диагностируемость: on_attempt_failure получает agent/attempt/max_attempts/fail_class/
     error/raw_tail; raw_tail усечён до AGENT_RAW_OUTPUT_LOG_BYTES и scrubbed (нет секретов).
  6. Agent 4 unrecoverable через JSON-поля block.text → UnrecoverableSignal (дерево None).
  7. Доменная валидация поверх extract_json: валидный JSON-формы с traversal-деревом →
     schema-фейл, ретраится; на исчерпании = AgentOutputError (agent_output_invalid).
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.observability.sentry import scrub_text
from app.pipeline.agents import agent3, agent4
from app.pipeline.agents.claude_client import AgentCall
from app.pipeline.agents.structured import (
    FAIL_CLASS_PARSE,
    FAIL_CLASS_SCHEMA,
    STRICT_JSON_SUFFIX,
    StructuredOutputError,
    append_strict_json,
    extract_json,
    run_structured_agent,
)
from app.pipeline.guards import PreCallGuardTripped
from app.schemas.agent_output import AgentOutputError

# asyncio_mode=auto (pyproject) автоматически распознаёт async-тесты — module-level
# mark не нужен (он бы навесил asyncio на синхронные extract_json-тесты → warning).


# --------------------------------------------------------------------------- #
# Фейк-инфраструктура: ТЕКСТОВЫЙ клиент (revised) + хуки (как у task-слоя).
# --------------------------------------------------------------------------- #


def _call(text: str = "{}", *, model: str = "claude-sonnet-4-6") -> AgentCall:
    return AgentCall(
        text=text,
        model=model,
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal("0.0010"),
    )


class _FakeTextClient:
    """Фейк ClaudeAgentClient.run_agent (ADR-020 §I.1 revised): запрограммированные ТЕКСТЫ.

    run_agent — единственный путь structured-output: возвращает AgentCall с .text (block.text);
    структура извлекается structured-слоем через extract_json. tool-use ОТОЗВАН.
    """

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.user_contents: list[str] = []
        self.system_prompts: list[str] = []

    async def run_agent(  # noqa: ANN201
        self,
        *,
        agent,
        model,
        system_prompt,
        user_content,  # noqa: ANN001
    ):
        self.user_contents.append(user_content)
        self.system_prompts.append(system_prompt)
        text = self._texts.pop(0) if self._texts else "{}"
        return _call(text, model=model)


class _Hooks:
    """Регистраторы before_call/after_call/on_attempt_failure (как инъектирует task-слой)."""

    def __init__(self, *, guard_raises_on=None):  # noqa: ANN001
        self.before = 0
        self.after = 0
        self.failures: list[dict] = []
        # guard_raises_on: номер вызова before_call (1-based), на котором бросить
        # PreCallGuardTripped (моделирует исчерпание budget/wall-clock перед retry-вызовом).
        self._guard_raises_on = guard_raises_on
        self._guard_reason = "budget_exhausted"

    async def before_call(self) -> None:
        self.before += 1
        if self._guard_raises_on is not None and self.before == self._guard_raises_on:
            raise PreCallGuardTripped(self._guard_reason)

    async def after_call(self, call) -> None:  # noqa: ANN001
        self.after += 1

    async def on_attempt_failure(self, **kw) -> None:  # noqa: ANN003
        self.failures.append(kw)


async def _run(settings, client, *, validate, hooks):  # noqa: ANN001, ANN202
    return await run_structured_agent(
        settings,
        client,
        agent="agent1",
        model="claude-sonnet-4-6",
        system_prompt="sys",
        user_content="user",
        validate=validate,
        before_call=hooks.before_call,
        after_call=hooks.after_call,
        on_attempt_failure=hooks.on_attempt_failure,
    )


@pytest.fixture
def settings():  # noqa: ANN201
    return get_settings()


# --------------------------------------------------------------------------- #
# (1) Регрессия на fence — extract_json (§I.2, прод-инцидент run1/run4).
# --------------------------------------------------------------------------- #


def test_extract_json_strips_json_fence():
    raw = '```json\n{"questions": ["a", "b"]}\n```'
    assert extract_json(raw) == {"questions": ["a", "b"]}


def test_extract_json_strips_bare_fence_without_tag():
    raw = '```\n{"x": 1}\n```'
    assert extract_json(raw) == {"x": 1}


def test_extract_json_strips_leading_and_trailing_prose():
    raw = 'Here is the result:\n{"a": 1, "b": [2, 3]}\nThanks!'
    assert extract_json(raw) == {"a": 1, "b": [2, 3]}


def test_extract_json_braces_inside_string_literals_do_not_break_balance():
    """Скобки {} внутри строковых литералов JSON не должны сбивать баланс (§I.2)."""
    raw = '```json\n{"content": "<div>{not json}</div>", "n": 1}\n```'
    assert extract_json(raw) == {"content": "<div>{not json}</div>", "n": 1}


def test_extract_json_escaped_quote_inside_string():
    raw = '{"content": "a \\" b } c", "ok": true}'
    assert extract_json(raw) == {"content": 'a " b } c', "ok": True}


def test_extract_json_array_top_level():
    raw = '```json\n[{"text": "q1"}, {"text": "q2"}]\n```'
    assert extract_json(raw) == [{"text": "q1"}, {"text": "q2"}]


def test_extract_json_no_balanced_json_raises():
    with pytest.raises(ValueError, match="no balanced JSON"):
        extract_json("just prose, no json here")


async def test_fence_text_does_not_fail_agent_step(settings):
    """Модель вернула ```json {…}``` в тексте (прод-инцидент run1/run4) → толерантный парсинг
    (§I.2) извлекает структуру из block.text, шаг агента НЕ уходит в FAILED."""
    client = _FakeTextClient(['```json\n{"questions": [{"text": "Q?"}]}\n```'])
    hooks = _Hooks()
    result = await _run(settings, client, validate=lambda d: d["questions"], hooks=hooks)
    assert result.value == [{"text": "Q?"}]
    assert hooks.failures == []  # без parse-фейла → нет диагностики, нет терминала


# --------------------------------------------------------------------------- #
# (2) Текстовый режим — структура из block.text; строгий JSON-суффикс в system-промте.
# --------------------------------------------------------------------------- #


async def test_structure_read_from_block_text_via_extract_json(settings):
    """Структура читается из текстового ответа (block.text) хелпером extract_json (§I.1 revised).
    Ведущая проза перед JSON срезается — основной путь детерминирован толерантным парсером."""
    client = _FakeTextClient(['Sure! Here:\n{"questions": [{"text": "from_text"}]}'])
    hooks = _Hooks()
    result = await _run(settings, client, validate=lambda d: d["questions"], hooks=hooks)
    assert result.value == [{"text": "from_text"}]


async def test_strict_json_suffix_appended_to_system_prompt(settings):
    """Системный промт агента несёт строгую «raw JSON без фенсов»-инструкцию (§I.1): она
    добавляется общим слоем через append_strict_json (STRICT_JSON_SUFFIX), не дублируется."""
    client = _FakeTextClient(['{"questions": [{"text": "Q"}]}'])
    hooks = _Hooks()
    await _run(settings, client, validate=lambda d: d["questions"], hooks=hooks)
    assert client.system_prompts == [append_strict_json("sys")]
    assert client.system_prompts[0].endswith(STRICT_JSON_SUFFIX)
    assert "raw JSON" in client.system_prompts[0]


async def test_prose_only_text_is_parse_fail(settings):
    """Только проза (нет JSON в тексте) → parse-фейл (re-семплируемый), не молча."""
    client = _FakeTextClient(["I think you want a landing page."] * 3)
    hooks = _Hooks()
    with pytest.raises(StructuredOutputError) as ei:
        await _run(settings, client, validate=lambda d: d, hooks=hooks)
    assert ei.value.fail_class == FAIL_CLASS_PARSE


# --------------------------------------------------------------------------- #
# (3) Bounded retry — до AGENT_OUTPUT_MAX_RETRIES; каждый retry = before/after; диагностика.
# --------------------------------------------------------------------------- #


async def test_retry_succeeds_on_second_attempt(settings):
    """Первый ответ — schema-фейл, второй — валиден → успех на 2-й попытке (re-sample)."""

    def _validate(d):  # noqa: ANN001, ANN202
        if not d.get("questions"):
            raise StructuredOutputError("missing questions", fail_class=FAIL_CLASS_SCHEMA)
        return d["questions"]

    client = _FakeTextClient(['{"questions": []}', '{"questions": [{"text": "Q"}]}'])
    hooks = _Hooks()
    result = await _run(settings, client, validate=_validate, hooks=hooks)
    assert result.value == [{"text": "Q"}]
    assert hooks.before == 2  # before_call ПЕРЕД каждым вызовом (вкл. retry)
    assert hooks.after == 2  # after_call ПОСЛЕ каждого вызова (оба оплачены → 2 llm_usage)
    assert len(hooks.failures) == 1  # первая попытка — диагностирована


async def test_retry_exhausted_raises_structured_output_error(settings):
    """Все попытки — schema-фейл → на исчерпании StructuredOutputError; N before/after/failures."""
    n = settings.agent_output_max_retries + 1

    def _validate(d):  # noqa: ANN001, ANN202
        raise StructuredOutputError("always bad", fail_class=FAIL_CLASS_SCHEMA)

    client = _FakeTextClient(['{"x": 1}'] * n)
    hooks = _Hooks()
    with pytest.raises(StructuredOutputError):
        await _run(settings, client, validate=_validate, hooks=hooks)
    assert hooks.before == n
    assert hooks.after == n  # каждый вызов оплачен → N записей llm_usage (§I.3)
    assert len(hooks.failures) == n


async def test_guard_checked_before_every_call_including_retries(settings):
    """before_call (budget/wall-clock-гард §C(b)/(c)) вызывается ПЕРЕД каждым LLM-вызовом,
    включая ретраи — счётчик before == числу вызовов."""
    n = settings.agent_output_max_retries + 1

    def _validate(d):  # noqa: ANN001, ANN202
        raise StructuredOutputError("bad", fail_class=FAIL_CLASS_PARSE)

    client = _FakeTextClient(["prose"] * n)
    hooks = _Hooks()
    with pytest.raises(StructuredOutputError):
        await _run(settings, client, validate=_validate, hooks=hooks)
    assert hooks.before == n


# --------------------------------------------------------------------------- #
# (4) Budget/wall-clock между ретраями — PreCallGuardTripped пробрасывается, не глотается.
# --------------------------------------------------------------------------- #


async def test_pre_call_guard_trip_propagates_not_swallowed(settings):
    """PreCallGuardTripped (budget/wall-clock §C) перед retry-вызовом ПРОБРАСЫВАЕТСЯ из шага
    агента (НЕ ловится retry-loop как schema-фейл) → task терминализует FAILED(reason)."""

    def _validate(d):  # noqa: ANN001, ANN202
        raise StructuredOutputError("schema bad", fail_class=FAIL_CLASS_SCHEMA)

    # 1-й вызов проходит guard и фейлит по schema; 2-й before_call бросает guard.
    client = _FakeTextClient(['{"x": 1}'] * 3)
    hooks = _Hooks(guard_raises_on=2)
    with pytest.raises(PreCallGuardTripped) as ei:
        await _run(settings, client, validate=_validate, hooks=hooks)
    assert ei.value.reason == "budget_exhausted"
    # guard сорвал второй вызов: первый attempt оплачен (after==1), второй до LLM не дошёл.
    assert hooks.after == 1
    assert hooks.before == 2


async def test_pre_call_guard_trip_on_first_call_no_llm(settings):
    """Guard исчерпан ДО первого вызова → ни одного after_call (kill перед LLM, §C(b))."""
    client = _FakeTextClient(['{"x": 1}'])
    hooks = _Hooks(guard_raises_on=1)
    with pytest.raises(PreCallGuardTripped):
        await _run(settings, client, validate=lambda d: d, hooks=hooks)
    assert hooks.after == 0


# --------------------------------------------------------------------------- #
# (5) Диагностируемость — on_attempt_failure payload + scrubbed усечённый raw_tail.
# --------------------------------------------------------------------------- #


async def test_diagnostics_payload_has_required_fields(settings):
    def _validate(d):  # noqa: ANN001, ANN202
        raise StructuredOutputError("missing 'files'", fail_class=FAIL_CLASS_SCHEMA)

    client = _FakeTextClient(['{"x": 1, "marker": "raw model text"}'])
    hooks = _Hooks()
    with pytest.raises(StructuredOutputError):
        await _run(settings, client, validate=_validate, hooks=hooks)
    f = hooks.failures[0]
    assert f["agent"] == "agent1"
    assert f["attempt"] == 1
    assert f["max_attempts"] == settings.agent_output_max_retries + 1
    assert "missing 'files'" in f["error_text"]
    assert f["fail_class"] == FAIL_CLASS_SCHEMA
    assert "raw" in f["raw_tail"]


async def test_diagnostics_raw_tail_truncated_to_log_bytes(settings, monkeypatch):
    """raw_tail усекается до AGENT_RAW_OUTPUT_LOG_BYTES (§I.4)."""
    monkeypatch.setattr(settings, "agent_raw_output_log_bytes", 16, raising=False)
    huge = "X" * 5000

    def _validate(d):  # noqa: ANN001, ANN202
        raise StructuredOutputError("bad", fail_class=FAIL_CLASS_SCHEMA)

    # raw-текст = огромная проза (парсится? нет → parse-фейл; но raw_tail диагностируется в любом
    # случае). Делаем валидный JSON, чтобы дойти до schema-validate, но с огромным «хвостом».
    client = _FakeTextClient(['{"x": 1}' + " " + huge])
    hooks = _Hooks()
    with pytest.raises(StructuredOutputError):
        await _run(settings, client, validate=_validate, hooks=hooks)
    assert len(hooks.failures[0]["raw_tail"]) <= 16


async def test_diagnostics_raw_tail_scrubbed_no_secrets(settings):
    """raw_tail scrubbed: секреты (Bearer/lv_-ключ) не утекают в job_events.payload (§I.4)."""
    # Тест-данные для проверки scrubbing — не настоящие секреты (S105 false-positive).
    secret_raw = '{"x":1} leaked Bearer sk-ant-supersecret and key lv_pubid_topsecretpart'  # noqa: S105, E501

    def _validate(d):  # noqa: ANN001, ANN202
        raise StructuredOutputError("bad", fail_class=FAIL_CLASS_SCHEMA)

    client = _FakeTextClient([secret_raw])
    hooks = _Hooks()
    with pytest.raises(StructuredOutputError):
        await _run(settings, client, validate=_validate, hooks=hooks)
    tail = hooks.failures[0]["raw_tail"]
    assert "sk-ant-supersecret" not in tail
    assert "topsecretpart" not in tail
    # scrubbed-форма совпадает с публичным scrubber'ом.
    assert tail == scrub_text(secret_raw)
    # key_id остаётся (только секретная часть lv_-ключа вырезана).
    assert "lv_pubid" in tail


# --------------------------------------------------------------------------- #
# (6) Agent 4 unrecoverable через JSON-поля block.text → UnrecoverableSignal (дерево None).
# --------------------------------------------------------------------------- #


def _src_tgz() -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in {"index.html": b"<html></html>", "package.json": b"{}"}.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _noop_before() -> None:
    return None


async def _noop_after(call) -> None:  # noqa: ANN001
    return None


async def _noop_fail(**kw) -> None:  # noqa: ANN003
    return None


async def test_agent4_unrecoverable_via_json_text(settings, monkeypatch):
    """Agent 4 unrecoverable выражается полями JSON в block.text (§A): дерево None, не ошибка
    валидации. Текстовый режим (revised) — структура из extract_json(block.text)."""
    signal = json.dumps({"unrecoverable": True, "reason": "irreparable", "explanation": "give up"})
    monkeypatch.setattr(agent4, "ClaudeAgentClient", lambda s: _FakeTextClient([signal]))
    result = await agent4.run_agent4(
        settings,
        spec_markdown="# Spec",
        source_tgz=_src_tgz(),
        failure_class="build_error",
        failure_log="log",
        before_call=_noop_before,
        after_call=_noop_after,
        on_attempt_failure=_noop_fail,
    )
    assert result.tree is None
    assert result.unrecoverable is not None
    assert result.unrecoverable.reason == "irreparable"


# --------------------------------------------------------------------------- #
# (7) Доменная валидация поверх extract_json — traversal/dotfile/over-limit → schema-фейл, ретрай.
# --------------------------------------------------------------------------- #


def _tree_with_path(path: str) -> str:
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    return json.dumps(
        {
            "files": [
                {"path": "package.json", "encoding": "utf8", "content": pkg},
                {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
                {"path": path, "encoding": "utf8", "content": "x"},
            ],
            "entry": "index.html",
            "build": {"command": "npm ci && vite build"},
        }
    )


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.js",  # path traversal
        ".npmrc",  # dotfile вне allowlist
        "/etc/passwd",  # абсолютный путь
    ],
)
async def test_agent3_domain_validation_over_extract_json_retries_then_raises(
    settings, monkeypatch, bad_path
):
    """Валидный JSON-формы (извлекается extract_json), но дерево нарушает доменные правила
    (§Контракт Agent 3: traversal/dotfile/абсолютный путь) → schema-фейл ПОВЕРХ извлечённой
    структуры, ретраится; на исчерпании = AgentOutputError (agent_output_invalid, §I.3)."""
    n = settings.agent_output_max_retries + 1
    client = _FakeTextClient([_tree_with_path(bad_path)] * n)
    monkeypatch.setattr(agent3, "ClaudeAgentClient", lambda s: client)

    after_calls = []
    failures = []

    async def _after(call):  # noqa: ANN001, ANN202
        after_calls.append(call)

    async def _fail(**kw):  # noqa: ANN003, ANN202
        failures.append(kw)

    with pytest.raises(AgentOutputError):
        await agent3.run_agent3(
            settings, "spec", before_call=_noop_before, after_call=_after, on_attempt_failure=_fail
        )
    # Доменный фейл ретраился N раз; каждый вызов оплачен и диагностирован как schema_error.
    assert len(after_calls) == n
    assert len(failures) == n
    assert all(f["fail_class"] == FAIL_CLASS_SCHEMA for f in failures)


async def test_agent3_over_limit_tree_is_schema_fail(settings, monkeypatch):
    """Дерево валидной формы, но файл превышает MAX_FILE_BYTES → доменный schema-фейл (§I.1)."""
    monkeypatch.setattr(settings, "max_file_bytes", 32, raising=False)
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    oversized = json.dumps(
        {
            "files": [
                {"path": "package.json", "encoding": "utf8", "content": pkg},
                {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
                {"path": "big.js", "encoding": "utf8", "content": "y" * 5000},
            ],
            "entry": "index.html",
            "build": {"command": "npm ci && vite build"},
        }
    )
    n = settings.agent_output_max_retries + 1
    client = _FakeTextClient([oversized] * n)
    monkeypatch.setattr(agent3, "ClaudeAgentClient", lambda s: client)
    failures = []

    async def _fail(**kw):  # noqa: ANN003, ANN202
        failures.append(kw)

    with pytest.raises(AgentOutputError):
        await agent3.run_agent3(
            settings,
            "spec",
            before_call=_noop_before,
            after_call=_noop_after,
            on_attempt_failure=_fail,
        )
    assert len(failures) == n
    assert all(f["fail_class"] == FAIL_CLASS_SCHEMA for f in failures)
