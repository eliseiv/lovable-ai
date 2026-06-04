"""Unit: обёртки агентов 2/3 поверх structured-слоя (ADR-020 §I) — фейк tool-use, без сети.

Agent 2/3 получают структуру форсированным tool-use (structured.run_structured_agent):
структура читается из tool_use.input (фейк-клиент возвращает её), доменная валидация —
поверх tool-use. Хуки before_call/after_call/on_attempt_failure инъектируются (здесь — no-op
с учётом числа вызовов/usage), как это делает task-слой.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.pipeline.agents import agent2, agent3
from app.pipeline.agents.claude_client import AgentCall, AgentToolCall
from app.schemas.agent_output import AgentOutputError

pytestmark = pytest.mark.asyncio


def _call(text):  # noqa: ANN001, ANN201
    return AgentCall(
        text=text,
        model="claude-opus-4-8",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal("0.0001"),
    )


class _FakeToolClient:
    """Фейк ClaudeAgentClient.run_agent_tool: структура из tool_input (ADR-020 §I.1).

    Очередь dict-ответов; невалидный/строковый ответ → tool_input=None (текстовый fallback).
    """

    def __init__(self, responses):  # noqa: ANN001
        self._responses = list(responses)
        self.calls = 0

    async def run_agent_tool(  # noqa: ANN201
        self,
        *,
        model,
        system_prompt,
        user_content,
        tool_name,
        input_schema,  # noqa: ANN001
    ):
        self.calls += 1
        resp = self._responses.pop(0) if self._responses else {}
        if isinstance(resp, dict):
            tool_input, text = resp, json.dumps(resp)
        else:
            tool_input, text = None, str(resp)
        return AgentToolCall(tool_input=tool_input, text=text, call=_call(text))


def _hooks():  # noqa: ANN202
    """Инъектируемые хуки task-слоя (no-op): счётчики guard/usage/diag-вызовов."""
    state = {"before": 0, "after": 0, "failures": []}

    async def before_call():  # noqa: ANN202
        state["before"] += 1

    async def after_call(call):  # noqa: ANN001, ANN202
        state["after"] += 1

    async def on_attempt_failure(**kw):  # noqa: ANN003, ANN202
        state["failures"].append(kw)

    return before_call, after_call, on_attempt_failure, state


@pytest.fixture
def settings():  # noqa: ANN201
    return get_settings()


def _wire2(monkeypatch, responses):  # noqa: ANN001, ANN202
    client = _FakeToolClient(responses)
    monkeypatch.setattr(agent2, "ClaudeAgentClient", lambda s: client)
    return client


def _wire3(monkeypatch, responses):  # noqa: ANN001, ANN202
    client = _FakeToolClient(responses)
    monkeypatch.setattr(agent3, "ClaudeAgentClient", lambda s: client)
    return client


async def test_agent2_returns_stripped_spec(settings, monkeypatch):
    _wire2(monkeypatch, [{"spec_markdown": "  # Spec\nbody  "}])
    before, after, on_fail, st = _hooks()
    result = await agent2.run_agent2(
        settings,
        "prompt",
        [("Q1", "A1")],
        before_call=before,
        after_call=after,
        on_attempt_failure=on_fail,
    )
    assert result.spec_markdown == "# Spec\nbody"
    assert result.call.cost_usd == Decimal("0.0001")
    assert st["before"] == 1 and st["after"] == 1


async def test_agent2_empty_spec_retries_then_raises(settings, monkeypatch):
    """Пустая спека — schema-фейл: ретраится до AGENT_OUTPUT_MAX_RETRIES, затем терминал."""
    n = settings.agent_output_max_retries + 1
    _wire2(monkeypatch, [{"spec_markdown": "   "}] * n)
    before, after, on_fail, st = _hooks()
    from app.pipeline.agents.structured import StructuredOutputError

    with pytest.raises(StructuredOutputError, match="empty specification"):
        await agent2.run_agent2(
            settings,
            "prompt",
            [],
            before_call=before,
            after_call=after,
            on_attempt_failure=on_fail,
        )
    # До терминала — N вызовов (1 + retries), каждый оплачен + диагностирован.
    assert st["before"] == n and st["after"] == n
    assert len(st["failures"]) == n


async def test_agent3_valid_tree(settings, monkeypatch):
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    tree = {
        "files": [
            {"path": "package.json", "encoding": "utf8", "content": pkg},
            {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
        ],
        "entry": "index.html",
        "build": {"command": "npm ci && vite build"},
    }
    _wire3(monkeypatch, [tree])
    before, after, on_fail, st = _hooks()
    result = await agent3.run_agent3(
        settings, "spec", before_call=before, after_call=after, on_attempt_failure=on_fail
    )
    assert result.tree.entry == "index.html"
    assert result.call is not None
    assert st["before"] == 1 and st["after"] == 1


async def test_agent3_invalid_tree_retries_then_raises_agent_output_error(settings, monkeypatch):
    """Невалидное дерево (пустой files) — доменный schema-фейл: ретрай, затем AgentOutputError."""
    n = settings.agent_output_max_retries + 1
    bad = {"files": [], "entry": "x", "build": {"command": "vite build"}}
    _wire3(monkeypatch, [bad] * n)
    before, after, on_fail, st = _hooks()
    with pytest.raises(AgentOutputError):
        await agent3.run_agent3(
            settings, "spec", before_call=before, after_call=after, on_attempt_failure=on_fail
        )
    # Каждый из N вызовов оплачен (usage пишется ВСЕГДА, §I.3) и диагностирован.
    assert st["after"] == n
    assert len(st["failures"]) == n
