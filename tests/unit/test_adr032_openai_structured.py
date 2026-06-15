"""Unit: structured-output единый текстовый путь для OpenAIAgentClient (ADR-032 §3, docs §Unit).

Источник истины — docs/adr/ADR-032-llm-provider-abstraction-openai.md §3 (единый текстовый
extract_json-путь, БЕЗ native json_schema), docs/06-testing-strategy.md §Unit «OpenAI
structured-output — единый текстовый путь».

Покрывает сценарий 8 ТЗ: extract_json / append_strict_json / bounded retry работают с
OpenAIAgentClient так же, как с Claude (provider-agnostic). Используем РЕАЛЬНЫЙ OpenAIAgentClient
с подменённым responses.stream (последовательность запрограммированных текстов) — структура
извлекается structured-слоем тем же extract_json (fence-снятие + repair ADR-026 + bounded retry),
provider-свитч не меняет семантику парсинга/валидации.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.pipeline.agents.openai_client import OpenAIAgentClient
from app.pipeline.agents.structured import (
    STRICT_JSON_SUFFIX,
    StructuredOutputError,
    run_structured_agent,
)

pytestmark = pytest.mark.asyncio


def _openai_settings(**overrides) -> Settings:  # noqa: ANN003
    base = {"llm_provider": "openai", "openai_api_key": "sk-openai-test"}
    base.update(overrides)
    return Settings(**base)


def _scripted_client(texts: list[str]) -> OpenAIAgentClient:
    """OpenAIAgentClient с responses.stream, отдающим запрограммированные тексты по порядку.

    Каждый вызов run_agent → следующий текст. usage минимален (cost не предмет этих тестов).
    """
    client = OpenAIAgentClient(_openai_settings())
    queue = list(texts)

    class _Ctx:
        def __init__(self, text: str) -> None:
            self._text = text

        async def __aenter__(self):  # noqa: ANN202
            return self

        async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
            return False

        async def get_final_response(self):  # noqa: ANN202
            usage = SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                input_tokens_details=SimpleNamespace(cached_tokens=0),
            )
            return SimpleNamespace(output_text=self._text, usage=usage)

    def _fake_stream(**kwargs):  # noqa: ANN003, ANN202
        return _Ctx(queue.pop(0))

    client._client.responses.stream = _fake_stream  # type: ignore[method-assign]
    return client


async def _noop() -> None:
    return None


async def _noop_usage(call) -> None:  # noqa: ANN001
    return None


async def _noop_diag(**kwargs) -> None:  # noqa: ANN003
    return None


async def _run(client, texts_validate, settings=None, agent="agent1"):  # noqa: ANN001, ANN202
    return await run_structured_agent(
        settings or _openai_settings(),
        client,
        agent=agent,
        model="gpt-5.4-mini",
        system_prompt="sys",
        user_content="user",
        validate=texts_validate,
        before_call=_noop,
        after_call=_noop_usage,
        on_attempt_failure=_noop_diag,
    )


# --- fence-снятие на OpenAI-пути (как у Claude) ---


async def test_openai_fenced_json_parsed_without_error():
    """```json {…}``` от OpenAI-клиента → extract_json без ValueError (тот же путь, §3)."""
    client = _scripted_client(['```json\n{"questions": [{"text": "Q"}]}\n```'])
    result = await _run(client, lambda s: s)
    assert result.value == {"questions": [{"text": "Q"}]}


async def test_openai_no_language_tag_fence_parsed():
    client = _scripted_client(['```\n{"spec_markdown": "# x"}\n```'])
    result = await _run(client, lambda s: s, agent="agent2")
    assert result.value == {"spec_markdown": "# x"}


# --- repair неэкранированных кавычек (ADR-026) на OpenAI-пути ---


async def test_openai_unescaped_inner_quotes_repaired():
    """Реальный кейс ADR-026 на OpenAI-выводе чинится repair-fallback extract_json (§3)."""
    bad = '{"questions":[{"position":1,"text":"… (e.g., "Where every cup tells a story")?"}]}'
    client = _scripted_client([bad])
    result = await _run(client, lambda s: s)
    assert result.value["questions"][0]["position"] == 1


# --- append_strict_json применяется к system-промту обоих провайдеров ---


async def test_openai_strict_json_suffix_appended_to_instructions():
    """run_structured_agent добавляет STRICT_JSON_SUFFIX к system-промту → instructions (§3)."""
    client = _scripted_client(['{"questions": []}'])
    captured: dict = {}
    orig = client._client.responses.stream

    def _capture(**kwargs):  # noqa: ANN003, ANN202
        captured.update(kwargs)
        return orig(**kwargs)

    client._client.responses.stream = _capture  # type: ignore[method-assign]
    await _run(client, lambda s: s)
    assert STRICT_JSON_SUFFIX in captured["instructions"]


# --- bounded retry: parse-фейл ретраится, затем успех (provider-agnostic) ---


async def test_openai_bounded_retry_recovers_after_parse_fail():
    """parse-фейл на 1-й попытке OpenAI → retry → успех на валидном JSON (тот же bounded retry)."""
    client = _scripted_client(["not json at all", '{"questions": []}'])
    result = await _run(client, lambda s: s)
    assert result.value == {"questions": []}


async def test_openai_bounded_retry_exhausted_raises():
    """Исчерпание ретраев на сплошном мусоре → StructuredOutputError (parse_error), как у Claude."""
    settings = _openai_settings()
    # max_retries дефолт 2 → 3 вызова: даём 3 мусорных текста.
    client = _scripted_client(["garbage1", "garbage2", "garbage3"])
    with pytest.raises(StructuredOutputError):
        await _run(client, lambda s: s, settings=settings)


async def test_openai_schema_validation_applied_over_structure():
    """Доменная валидация поверх извлечённой структуры → schema-фейл ретраится, как у Claude."""

    def _validate(structure):  # noqa: ANN001, ANN202
        if "questions" not in structure:
            raise ValueError("missing questions")
        return structure

    # 1-й вызов: валидный JSON без 'questions' (schema-фейл) → retry; 2-й: валидный.
    client = _scripted_client(['{"foo": 1}', '{"questions": [{"text": "Q"}]}'])
    result = await _run(client, _validate)
    assert result.value == {"questions": [{"text": "Q"}]}
