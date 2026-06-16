"""Unit/contract: ADR-034 §D3/§D10 — vision-вход обоих провайдеров (реальное тело запроса).

Источник истины: docs/adr/ADR-034 §D3/§D10, docs/06-testing-strategy.md §Integration
«vision-вход обоих провайдеров (мок SDK, contract на реальном теле)» + §Unit «D10 cost».

Покрывает сценарии ТЗ:
- 5 (vision-инвариант D3, КРИТИЧНО): при images=None тело обоих провайдеров байт-в-байт
  текстовое (нет регрессий Anthropic-дефолта); при непустом images — image-блоки ПЕРЕД text;
- 10 (cost D10): image-вход НЕ вводит новой ставки — input_tokens обоих провайдеров покрывают
  vision; cost_usd и usage-маппинг идентичны текстовому пути (регрессия cost-ledger).

Реального сетевого вызова НЕТ: подменяем РОВНО точку SDK-вызова (messages.stream /
responses.stream) на сборщик kwargs, возвращающий фейковый финальный объект (как реальный SDK).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.pipeline.agents.base import ImageInput
from app.pipeline.agents.claude_client import ClaudeAgentClient
from app.pipeline.agents.openai_client import OpenAIAgentClient

pytestmark = pytest.mark.asyncio


# ============================ Anthropic (ClaudeAgentClient) ============================


class _CapturedAnthropicStream:
    def __init__(self, message) -> None:  # noqa: ANN001
        self._message = message

    async def __aenter__(self):  # noqa: ANN202
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
        return False

    async def get_final_message(self):  # noqa: ANN202
        return self._message


def _anthropic_message(*, input_tokens=100, output_tokens=50, cache_read=0, cache_write=0):  # noqa: ANN001, ANN202
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )
    text_block = SimpleNamespace(type="text", text='{"questions": [{"text": "Q"}]}')
    return SimpleNamespace(content=[text_block], usage=usage)


def _capturing_claude(*, message=None):  # noqa: ANN001, ANN202
    client = ClaudeAgentClient(Settings(llm_provider="anthropic", anthropic_api_key="sk-ant-test"))
    captured: dict = {}
    msg = message or _anthropic_message()

    def _fake_stream(**kwargs):  # noqa: ANN003, ANN202
        captured.clear()
        captured.update(kwargs)
        return _CapturedAnthropicStream(msg)

    client._client.messages.stream = _fake_stream  # type: ignore[method-assign]
    return client, captured


async def test_anthropic_text_path_byte_for_byte_when_images_none():
    """images=None (дефолт): messages[0].content — голая строка user_content (текстовый путь)."""
    client, captured = _capturing_claude()
    await client.run_agent(
        agent="agent1", model="claude-sonnet-4-6", system_prompt="sys", user_content="hello"
    )
    content = captured["messages"][0]["content"]
    assert content == "hello", "дефолтный текстовый путь Anthropic = голая строка (нет регрессий)"
    assert isinstance(content, str)


async def test_anthropic_vision_image_blocks_before_text():
    """Непустой images: content = [image-блоки base64] ПЕРЕД [text-блок] (§D3)."""
    client, captured = _capturing_claude()
    imgs = [
        ImageInput(data=b"\x89PNG-bytes", media_type="image/png"),
        ImageInput(data=b"GIF8-bytes", media_type="image/gif"),
    ]
    await client.run_agent(
        agent="agent1",
        model="claude-sonnet-4-6",
        system_prompt="sys",
        user_content="describe",
        images=imgs,
    )
    content = captured["messages"][0]["content"]
    assert isinstance(content, list)
    # Два image-блока ПЕРЕД одним text-блоком.
    assert [b["type"] for b in content] == ["image", "image", "text"]
    assert content[0]["source"]["type"] == "base64"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[2]["text"] == "describe"


# ============================ OpenAI (OpenAIAgentClient) ============================


class _CapturedOpenAIStream:
    def __init__(self, response) -> None:  # noqa: ANN001
        self._response = response

    async def __aenter__(self):  # noqa: ANN202
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
        return False

    async def get_final_response(self):  # noqa: ANN202
        return self._response


def _openai_response(*, input_tokens=100, output_tokens=50, cached_tokens=0):  # noqa: ANN001, ANN202
    details = SimpleNamespace(cached_tokens=cached_tokens)
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_tokens_details=details,
    )
    return SimpleNamespace(output_text='{"questions": [{"text": "Q"}]}', usage=usage)


def _capturing_openai(*, response=None):  # noqa: ANN001, ANN202
    client = OpenAIAgentClient(Settings(llm_provider="openai", openai_api_key="sk-openai-test"))
    captured: dict = {}
    resp = response or _openai_response()

    def _fake_stream(**kwargs):  # noqa: ANN003, ANN202
        captured.clear()
        captured.update(kwargs)
        return _CapturedOpenAIStream(resp)

    client._client.responses.stream = _fake_stream  # type: ignore[method-assign]
    return client, captured


async def test_openai_text_path_byte_for_byte_when_images_none():
    """images=None (дефолт): input — голая строка user_content (текстовый путь, нет регрессий)."""
    client, captured = _capturing_openai()
    await client.run_agent(
        agent="agent1", model="gpt-5.4-mini", system_prompt="sys", user_content="hello"
    )
    assert captured["input"] == "hello"
    assert isinstance(captured["input"], str)


async def test_openai_vision_input_image_before_input_text():
    """Непустой images: input = [{input_image data-URL} ПЕРЕД {input_text}] (§D3)."""
    client, captured = _capturing_openai()
    imgs = [ImageInput(data=b"\x89PNG-bytes", media_type="image/png")]
    await client.run_agent(
        agent="agent2",
        model="gpt-5.5",
        system_prompt="sys",
        user_content="describe",
        images=imgs,
    )
    payload = captured["input"]
    assert isinstance(payload, list)
    parts = payload[0]["content"]
    assert [p["type"] for p in parts] == ["input_image", "input_text"]
    assert parts[0]["image_url"].startswith("data:image/png;base64,")
    assert parts[1]["text"] == "describe"


# ============================ сценарий 10: cost D10 — image НЕ вводит новой ставки ===========


async def test_anthropic_cost_unchanged_image_in_input_tokens():
    """D10: одинаковый usage даёт одинаковый cost независимо от наличия image-блоков.

    image-токены уже включены в usage.input_tokens — отдельной image-ставки нет. Тело с image
    и без него при равном usage → равный cost_usd (cost-ledger не меняется)."""
    usage_kwargs = {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    client_text, _ = _capturing_claude(message=_anthropic_message(**usage_kwargs))
    call_text = await client_text.run_agent(
        agent="agent1", model="claude-sonnet-4-6", system_prompt="s", user_content="u"
    )
    client_img, _ = _capturing_claude(message=_anthropic_message(**usage_kwargs))
    call_img = await client_img.run_agent(
        agent="agent1",
        model="claude-sonnet-4-6",
        system_prompt="s",
        user_content="u",
        images=[ImageInput(data=b"\x89PNGxx", media_type="image/png")],
    )
    # Sonnet input-ставка = 3.00 / 1M → 1M input = 3.0000.
    assert call_text.cost_usd == Decimal("3.0000")
    assert call_img.cost_usd == call_text.cost_usd
    assert call_img.input_tokens == call_text.input_tokens == 1_000_000


async def test_openai_cost_unchanged_image_in_input_tokens():
    """D10 (OpenAI): vision-вход не меняет usage-маппинг/ставку — равный usage → равный cost."""
    resp_kwargs = {"input_tokens": 1_000_000, "output_tokens": 0, "cached_tokens": 0}
    client_text, _ = _capturing_openai(response=_openai_response(**resp_kwargs))
    call_text = await client_text.run_agent(
        agent="agent1", model="gpt-5.4-mini", system_prompt="s", user_content="u"
    )
    client_img, _ = _capturing_openai(response=_openai_response(**resp_kwargs))
    call_img = await client_img.run_agent(
        agent="agent1",
        model="gpt-5.4-mini",
        system_prompt="s",
        user_content="u",
        images=[ImageInput(data=b"\x89PNGxx", media_type="image/png")],
    )
    # gpt-5.4-mini input-ставка = 0.75 / 1M → 1M input = 0.7500.
    assert call_text.cost_usd == Decimal("0.7500")
    assert call_img.cost_usd == call_text.cost_usd
    assert call_img.input_tokens == call_text.input_tokens == 1_000_000
    assert call_img.cache_write_tokens == 0
