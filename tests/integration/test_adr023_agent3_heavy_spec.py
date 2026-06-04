"""Integration «тяжёлая спека» Agent 3 (ADR-023, §I.5, ОБЯЗАТЕЛЬНЫЙ — воспроизводит прод-инцидент).

Источник истины — docs/modules/pipeline/03-architecture.md §I.5 (integration «тяжёлая» спека) +
§Token-бюджет агентов (ADR-023), docs/adr/ADR-023-agent3-token-budget-thinking-room.md.

Прод-инцидент: Agent 3 (Builder) на multi-file спеке сложного сайта ДЕТЕРМИНИРОВАННО падал
invalid_agent_output — все 3 попытки stop_reason=max_tokens (усечённый JSON / пустой текст при
едином AGENT_MAX_TOKENS=16000). После ADR-023 (cap 56000 + thinking=disabled у Builder) полный
file-tree помещается под cap.

Этот тест воспроизводит сценарий через МОК-клиент, возвращающий ПОЛНЫЙ валидный JSON-tree
multi-file спеки (живой Anthropic в CI недоступен — допустим real-stack-skip с мок-фолбэком,
§I.5). Проверяется: extract_json извлекает структуру без ValueError, текст НЕпустой, доменная
валидация дерева проходит, run_agent3 НЕ уходит в AgentOutputError (≈ FAILED(invalid_agent_output)).
Дополнительно проверяем, что путь СОБИРАЕТ корректный token-бюджет Agent 3 (cap 56000 +
thinking=disabled) — связка с contract-тестом §I.5.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.pipeline.agents import agent3
from app.pipeline.agents.agent3 import run_agent3
from app.pipeline.agents.claude_client import AgentCall

pytestmark = pytest.mark.asyncio


def _heavy_tree_json() -> str:
    """Полный валидный file-tree multi-file сложного сайта (эквивалент репро-спеки упавшей джобы).

    Несколько HTML/CSS/TS/JSON-файлов + бинарный ассет (base64) — структурно «тяжёлая» спека,
    которая при cap=16000 усекалась посреди JSON. Здесь — полное сбалансированное дерево.
    """
    pkg = json.dumps(
        {
            "name": "heavy-site",
            "scripts": {"build": "vite build"},
            "dependencies": {"vite": "^5.0.0"},
            "devDependencies": {"typescript": "^5.4.0"},
        }
    )
    # Крупный текстовый контент нескольких страниц — имитирует «тяжёлый» вывод (>28763 симв.,
    # доказанно недостаточных для cap=16000 в прод-инциденте).
    page_html = (
        "<!doctype html><html><head><title>Heavy</title></head><body>"
        + ("<section>content block</section>" * 400)
        + "</body></html>"
    )
    big_css = "\n".join(f".cls-{i} {{ color: #{i:06x}; margin: {i}px; }}" for i in range(600))
    big_ts = "\n".join(f"export const v{i} = {i};" for i in range(600))
    tiny_png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="  # noqa: E501
    tree = {
        "files": [
            {"path": "index.html", "encoding": "utf8", "content": page_html},
            {"path": "about.html", "encoding": "utf8", "content": page_html},
            {"path": "contact.html", "encoding": "utf8", "content": page_html},
            {"path": "package.json", "encoding": "utf8", "content": pkg},
            {"path": "src/styles.css", "encoding": "utf8", "content": big_css},
            {"path": "src/main.ts", "encoding": "utf8", "content": big_ts},
            {"path": "src/app.ts", "encoding": "utf8", "content": big_ts},
            {"path": "public/logo.png", "encoding": "base64", "content": tiny_png_b64},
        ],
        "entry": "index.html",
        "build": {"tool": "vite", "command": "npm ci && vite build", "output_dir": "dist"},
    }
    return json.dumps(tree)


_HEAVY_SPEC = (
    "# Спецификация: многостраничный корпоративный сайт\n\n"
    "Главная, О компании, Контакты, общие стили, TypeScript-модули, логотип-ассет.\n"
    + ("Раздел требований. " * 500)
)


class _FullTreeClient:
    """Мок ClaudeAgentClient: возвращает ПОЛНЫЙ валидный JSON-tree (живой Anthropic недоступен).

    Фиксирует переданный agent — связка с token-бюджетом (§I.5): убеждаемся, что run_agent3
    зовёт run_agent именно как agent='agent3' (по которому собирается cap 56000 + disabled).
    """

    def __init__(self, settings) -> None:  # noqa: ANN001
        self.captured_agent: str | None = None

    async def run_agent(self, *, agent, model, system_prompt, user_content):  # noqa: ANN001, ANN201
        self.captured_agent = agent
        return AgentCall(
            text=_heavy_tree_json(),
            model=model,
            input_tokens=2000,
            output_tokens=30000,  # большой, но НЕ усечённый вывод полного дерева
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=Decimal("0.5000"),
        )


async def _noop_before() -> None:
    return None


async def _noop_after(call) -> None:  # noqa: ANN001
    return None


async def _noop_diag(**_kw) -> None:  # noqa: ANN003
    return None


async def test_agent3_heavy_spec_produces_full_valid_tree(monkeypatch):
    """Agent 3 на «тяжёлой» multi-file спеке → полное валидное дерево, НЕ invalid_agent_output.

    Воспроизводит и закрывает прод-инцидент (детерминированный max_tokens-усечённый/пустой вывод):
    при cap 56000 + thinking=disabled полный file-tree извлекается extract_json без ValueError,
    доменная валидация проходит, шаг НЕ уходит в AgentOutputError.
    """
    settings = get_settings()
    monkeypatch.setattr(agent3, "ClaudeAgentClient", _FullTreeClient)

    result = await run_agent3(
        settings,
        _HEAVY_SPEC,
        before_call=_noop_before,
        after_call=_noop_after,
        on_attempt_failure=_noop_diag,
    )

    # extract_json извлёк структуру + доменная валидация прошла (иначе был бы AgentOutputError).
    tree = result.tree
    paths = {f.path for f in tree.files}
    assert "index.html" in paths
    assert "package.json" in paths
    assert len(tree.files) >= 5  # multi-file дерево, не усечено
    # Текст ответа НЕпустой (анти-регресс attempt-2/3 text_len=0).
    assert result.call.text
    assert result.call.output_tokens > 0


async def test_agent3_heavy_spec_invoked_as_agent3_token_budget(monkeypatch):
    """run_agent3 зовёт run_agent как agent='agent3' → по нему собирается cap 56000 + disabled.

    Связка с §I.5 contract: «тяжёлая» спека проходит через тот же конфиг-маппинг token-бюджета,
    который проверяет test_adr023_token_budget_contract (cap 56000, thinking=disabled).
    """
    settings = get_settings()
    captor = {}

    class _Capturing(_FullTreeClient):
        async def run_agent(self, *, agent, model, system_prompt, user_content):  # noqa: ANN001, ANN201
            captor["agent"] = agent
            return await super().run_agent(
                agent=agent, model=model, system_prompt=system_prompt, user_content=user_content
            )

    monkeypatch.setattr(agent3, "ClaudeAgentClient", _Capturing)

    await run_agent3(
        settings,
        _HEAVY_SPEC,
        before_call=_noop_before,
        after_call=_noop_after,
        on_attempt_failure=_noop_diag,
    )

    assert captor["agent"] == "agent3"
    # Конфиг-маппинг по agent3 — cap 56000 + thinking disabled (нормативно §Token-бюджет).
    assert settings.agent_max_tokens("agent3") == 56000
    assert settings.agent_thinking("agent3") == {"type": "disabled"}
