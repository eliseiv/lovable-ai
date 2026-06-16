"""Обёртка над OpenAI SDK (Responses API) для агентов (ADR-032).

Альтернативный LLM-провайдер (`LLM_PROVIDER=openai`, ADR-032): 4 агента на reasoning-модели
класса GPT-5 через **Responses API** (`client.responses.stream`). Контракт идентичен Anthropic-
клиенту: `run_agent(...) -> AgentCall` (нейтральный тип из `base.py`).

Маппинг управляющих параметров (ADR-032 §2):
- `max_output_tokens` ← per-agent cap `AGENTn_MAX_TOKENS` (`settings.agent_max_tokens(agent)`);
- `reasoning.effort` ← agent3/agent4 → `none` (весь cap под вывод полного file-tree, перенос
  мотива ADR-023); agent1/agent2 → `settings.openai_agent_effort` (дефолт `high`);
- `instructions` ← system_prompt БЕЗ cache_control (caching у OpenAI автоматический, §6);
- `input` ← user_content.

Structured-output — тот же текстовый `extract_json`-путь (`structured.py`), native json_schema
НЕ используется (ADR-032 §3).

Usage/cost (ADR-032 §4): `input_tokens`/`output_tokens` (reasoning-токены входят в output и
биллятся по output-ставке); `cache_read` = `usage.input_tokens_details.cached_tokens`;
`cache_write` = `0` (OpenAI caching автоматический, без write-ставки). Pricing — нормативная
таблица observability §2.2A (per-1M USD), 1:1; неизвестная модель → консервативный fallback.
"""

from __future__ import annotations

import base64
from decimal import Decimal
from typing import Any

from openai import AsyncOpenAI

from app.core.config import Settings
from app.pipeline.agents.base import AgentCall, ImageInput

# Себестоимость per-1M токенов (USD) OpenAI-моделей — НОРМАТИВНАЯ таблица
# docs/modules/observability/03-architecture.md §2.2A (верифицирована по каталогу OpenAI
# 2026-06-15, Q-LLM-1 resolved). cache_write = 0 для всех (caching автоматический, без
# write-ставки). gpt-5.5-pro / gpt-5.4-pro — без cached-input-ставки (cache_read = 0, прочерк
# в §2.2A: cache-hit на этих моделях не даёт скидки). input / cache_read / output / cache_write.
_MODEL_PRICING: dict[str, dict[str, Decimal]] = {
    "gpt-5.5": {
        "input": Decimal("5.00"),
        "cache_read": Decimal("0.50"),
        "output": Decimal("30.00"),
        "cache_write": Decimal("0"),
    },
    "gpt-5.5-pro": {
        "input": Decimal("30.00"),
        "cache_read": Decimal("0"),
        "output": Decimal("180.00"),
        "cache_write": Decimal("0"),
    },
    "gpt-5.4": {
        "input": Decimal("2.50"),
        "cache_read": Decimal("0.25"),
        "output": Decimal("15.00"),
        "cache_write": Decimal("0"),
    },
    "gpt-5.4-mini": {
        "input": Decimal("0.75"),
        "cache_read": Decimal("0.075"),
        "output": Decimal("4.50"),
        "cache_write": Decimal("0"),
    },
    "gpt-5.4-nano": {
        "input": Decimal("0.20"),
        "cache_read": Decimal("0.02"),
        "output": Decimal("1.25"),
        "cache_write": Decimal("0"),
    },
    "gpt-5.4-pro": {
        "input": Decimal("30.00"),
        "cache_read": Decimal("0"),
        "output": Decimal("180.00"),
        "cache_write": Decimal("0"),
    },
}
# Консервативный fallback для неизвестной модели — самый дорогой per-output тариф каталога
# (gpt-5.5-pro), по аналогии с Anthropic-fallback на Opus-тариф (claude_client._compute_cost):
# при незнакомом env AGENTn_MODEL не занижаем себестоимость (cost-cap не обходится).
_FALLBACK_MODEL = "gpt-5.5-pro"
_PER_MILLION = Decimal("1000000")

# reasoning.effort агентов 3/4 (Builder/Fixer): наименьший уровень реального набора OpenAI
# (none/low/medium/high/xhigh) — весь max_output_tokens под вывод полного file-tree (ADR-032 §2,
# перенос мотива ADR-023). Агенты 1/2 — settings.openai_agent_effort (дефолт high).
_REASONING_NONE = "none"
_NO_REASONING_AGENTS = frozenset({"agent3", "agent4"})


def _compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> Decimal:
    """Себестоимость вызова (USD) по OpenAI-таблице §2.2A (та же формула, что Anthropic).

    cache_write-член = 0 для OpenAI (без write-ставки, §4); cached input-токены тарифицируются
    по cache_read-ставке. Неизвестная модель → консервативный fallback (не падаем, §4).
    """
    pricing = _MODEL_PRICING.get(model, _MODEL_PRICING[_FALLBACK_MODEL])
    cost = (
        pricing["input"] * input_tokens
        + pricing["output"] * output_tokens
        + pricing["cache_read"] * cache_read_tokens
        + pricing["cache_write"] * cache_write_tokens
    ) / _PER_MILLION
    return cost.quantize(Decimal("0.0001"))


class OpenAIAgentClient:
    """Async-клиент одного вызова агента через OpenAI Responses API + cost-учёт (ADR-032)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Credential (ADR-032 §5): пустой/whitespace OPENAI_API_KEY отсекается preflight'ом
        # (`llm_credential_present` / `active_llm_api_key`) ДО конструирования клиента —
        # fail-fast graceful FAILED(agent_unavailable). Невалидный (непустой) ключ НЕ
        # валидируется SDK client-side: `auth_headers` лишь подставляет `Bearer <key>`, запрос
        # уходит на сервер и возвращает 401 → `openai.AuthenticationError` (request-time,
        # подкласс APIStatusError) — она УЖЕ в NON_RETRYABLE_LLM_EXCEPTIONS (retry_policy),
        # классифицируется как не-транзиентный LLM-сбой → FAILED(agent_unavailable) без ретраев.
        # Поэтому отдельная обёртка credential-кейса в LLMCredentialError здесь НЕ нужна
        # (в отличие от Anthropic, где невалидный ключ даёт client-side stdlib-TypeError ДО HTTP).
        self._client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())

    def _reasoning_effort(self, agent: str) -> str:
        """reasoning.effort по агенту (ADR-032 §2): agent3/4 → none; agent1/2 → конфиг-effort."""
        if agent in _NO_REASONING_AGENTS:
            return _REASONING_NONE
        return self._settings.openai_agent_effort

    async def run_agent(
        self,
        *,
        agent: str,
        model: str,
        system_prompt: str,
        user_content: str,
        images: list[ImageInput] | None = None,
    ) -> AgentCall:
        """Один текстовый вызов агента через Responses API (ADR-032 §2/§3).

        Стримим (длинный вывод полного file-tree) + собираем финальный response — защита от
        HTTP-таймаута при больших max_output_tokens (симметрично Anthropic stream). Структура
        извлекается из текста выше по стеку (`structured.extract_json`, §3) — клиент возвращает
        сырой `text`, native json_schema не используется.

        instructions = system_prompt БЕЗ cache_control (caching у OpenAI автоматический по
        идентичному префиксу, §6); input = user_content. max_output_tokens / reasoning.effort —
        per-agent (§2). Credential (§5): пустой ключ отсекается preflight'ом, невалидный →
        request-time AuthenticationError(401) — не-транзиентный LLM-сбой (классифицируется
        retry_policy), клиент его НЕ оборачивает.

        Vision (ADR-034 §D3): при непустом `images` `input` становится списком content-частей
        `input_image` (data-URL base64) + `input_text`. Дефолт `images=None` ⇒ прежний текстовый
        путь байт-в-байт. reasoning/instructions не меняются (input_image совместим с reasoning).
        """
        response = await self._stream_final_response(
            agent=agent,
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            images=images,
        )
        text = response.output_text
        return self._build_call(model, response, text)

    @staticmethod
    def _input_payload(user_content: str, images: list[ImageInput] | None) -> Any:
        """`input` Responses API: строка (текстовый путь) или список content-частей (vision, §D3).

        Непустой `images`: `input_image` (data-URL) идут ПЕРЕД `input_text`. Пустой/None ⇒
        прежняя голая строка (байт-в-байт текстовый путь).
        """
        if not images:
            return user_content
        content: list[dict[str, Any]] = [
            {
                "type": "input_image",
                "image_url": (
                    f"data:{img.media_type};base64,{base64.b64encode(img.data).decode('ascii')}"
                ),
            }
            for img in images
        ]
        content.append({"type": "input_text", "text": user_content})
        return [{"role": "user", "content": content}]

    async def _stream_final_response(
        self,
        *,
        agent: str,
        model: str,
        system_prompt: str,
        user_content: str,
        images: list[ImageInput] | None = None,
    ) -> Any:
        """Транспорт: Responses API stream + финальный response (ADR-032 §2).

        kwargs БЕЗ `text.format`/`json_schema` (текстовый режим, §3) и БЕЗ `cache_control`
        (caching автоматический, §6). `reasoning.effort` per-agent: agent3/4 → none (весь cap
        под вывод), agent1/2 → openai_agent_effort. `max_output_tokens` ← per-agent cap (§2).

        Ошибки/ретраи (ADR-032 §5): ВСЕ исключения openai SDK пробрасываются БЕЗ обёртки —
        классифицирует `retry_policy` (единственная точка решения retry vs graceful-fail).
        Транзиентные (RateLimitError/APIConnectionError/APITimeoutError/APIStatusError 5xx) →
        Celery-ретрай; не-ретраябельные (AuthenticationError 401 / PermissionDeniedError 403 /
        BadRequestError 400) → FAILED(agent_unavailable). Credential-кейс: пустой ключ отсекается
        preflight'ом (§G); невалидный (непустой) ключ НЕ валидируется SDK client-side (см.
        __init__) — даёт request-time AuthenticationError(401), уже трактуемую как не-транзиентную.
        Поэтому, в отличие от Anthropic (client-side TypeError ДО HTTP), здесь обёртки в
        LLMCredentialError НЕТ — она исказила бы транзиентную классификацию (перемапила бы
        транзиентные 429/5xx/timeout в non-retryable).
        """
        kwargs: dict[str, Any] = {
            "model": model,
            "max_output_tokens": self._settings.agent_max_tokens(agent),
            "reasoning": {"effort": self._reasoning_effort(agent)},
            "instructions": system_prompt,
            "input": self._input_payload(user_content, images),
        }
        async with self._client.responses.stream(**kwargs) as stream:
            return await stream.get_final_response()

    def _build_call(self, model: str, response: Any, text: str) -> AgentCall:
        """Собирает AgentCall (текст + учёт токенов/стоимости) из финального response SDK.

        Маппинг usage Responses API (ADR-032 §4): input_tokens / output_tokens (включает
        reasoning-токены — output-ставка); cache_read = input_tokens_details.cached_tokens;
        cache_write = 0 (caching автоматический, без write-ставки, §6).
        """
        usage = response.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        details = getattr(usage, "input_tokens_details", None)
        cache_read = getattr(details, "cached_tokens", 0) or 0
        cache_write = 0
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
