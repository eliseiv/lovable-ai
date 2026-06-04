"""Обёртка над Anthropic SDK для агентов (skill claude-api).

- Prompt caching стабильного system-промта (cache_control: ephemeral) — кэш между
  агентами и fix-итерациями.
- Adaptive thinking + effort из конфига (модели claude-opus-4-8 / claude-sonnet-4-6).
- Cost-ledger: usage (input/output/cache_read/cache_write) + cost_usd → llm_usage.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import OutputConfigParam

from app.core.config import Settings
from app.workers.retry_policy import LLMCredentialError

# Себестоимость per-1M токенов (USD), по модели (skill claude-api → Current Models).
# input / output / cache_read (~0.1x input) / cache_write (~1.25x input).
_MODEL_PRICING: dict[str, dict[str, Decimal]] = {
    "claude-opus-4-8": {
        "input": Decimal("5.00"),
        "output": Decimal("25.00"),
        "cache_read": Decimal("0.50"),
        "cache_write": Decimal("6.25"),
    },
    "claude-sonnet-4-6": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
        "cache_read": Decimal("0.30"),
        "cache_write": Decimal("3.75"),
    },
}
_PER_MILLION = Decimal("1000000")


@dataclass(frozen=True)
class AgentCall:
    """Результат вызова агента: текст ответа + учёт токенов/стоимости."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal


def _compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> Decimal:
    pricing = _MODEL_PRICING.get(model)
    if pricing is None:
        # Неизвестная модель: считаем по тарифу Opus (консервативно), не падаем.
        pricing = _MODEL_PRICING["claude-opus-4-8"]
    cost = (
        pricing["input"] * input_tokens
        + pricing["output"] * output_tokens
        + pricing["cache_read"] * cache_read_tokens
        + pricing["cache_write"] * cache_write_tokens
    ) / _PER_MILLION
    return cost.quantize(Decimal("0.0001"))


class ClaudeAgentClient:
    """Async-клиент для одного вызова агента с prompt caching и cost-учётом."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())

    async def run_agent(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
    ) -> AgentCall:
        """Один текстовый вызов агента (ADR-020 §I.1, revised): стабильный system кэшируется,
        user — волатильная часть. Стримим (длинный вывод) + собираем финальное сообщение —
        защита от HTTP-таймаута при больших max_tokens (skill claude-api).

        ЕДИНЫЙ нормативный путь structured-output всех 4 агентов (ADR-020, revised 2026-06-04):
        ТЕКСТОВЫЙ режим — `thinking=adaptive` + `output_config={effort}`, БЕЗ `tools`/`tool_choice`.
        Форсированный tool-use ОТОЗВАН: несовместим с thinking → HTTP 400 «Thinking may not be
        enabled when tool_choice forces tool use» → 100% отказ (ADR-020 §Ограничение API).
        Структура извлекается из `block.text` хелпером `extract_json` (structured.py §I.2).
        """
        message = await self._stream_final_message(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        return self._build_call(model, message, text)

    async def _stream_final_message(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
    ) -> Any:
        """Транспорт: stream + get_final_message для текстового вызова агента.

        Собранные kwargs `messages.stream` НЕ содержат `tools`/`tool_choice` (ADR-020 §I.1,
        revised) — несовместимы с `thinking=adaptive` (HTTP 400). Только `thinking=adaptive` +
        `output_config={effort}`; формат выхода форсируется строгим системным промтом.

        ADR-019 §Fix round 3 (подстраховка, §G): единственная идентифицируемая точка обращения
        к Anthropic SDK. При невалидном (но непустом — пустой отсекается preflight'ом §G)
        credential SDK бросает ВСТРОЕННЫЙ stdlib-`TypeError` на client-side auth-resolution
        (`_validate_headers`) ДО HTTP-запроса. Перехватываем РОВНО здесь (узкий, version-agnostic
        матч по классу в точке SDK-вызова, а НЕ по подстроке через весь стек) и поднимаем
        доменный LLMCredentialError → классификатор §D трактует как не-транзиентный LLM-сбой →
        FAILED(agent_unavailable) без ретраев.
        """
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self._settings.agent_max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": cast(OutputConfigParam, {"effort": self._settings.agent_effort}),
            "system": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_content}],
        }
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                return await stream.get_final_message()
        except TypeError as exc:
            raise LLMCredentialError(
                "Anthropic SDK auth-resolution failed (invalid LLM credential)"
            ) from exc

    def _build_call(self, model: str, message: Any, text: str) -> AgentCall:
        """Собирает AgentCall (текст + учёт токенов/стоимости) из финального сообщения SDK."""
        usage = message.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cost = _compute_cost(model, input_tokens, output_tokens, cache_read, cache_write)
        return AgentCall(
            text=text,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=cost,
        )
