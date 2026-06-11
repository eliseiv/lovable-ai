"""Unit: обёртки агентов 2/3 поверх structured-слоя (ADR-020 revised §I) — фейк текст, без сети.

REVISED: tool-use ОТОЗВАН. Agent 2/3 получают структуру ТЕКСТОВЫМ режимом
(structured.run_structured_agent → ClaudeAgentClient.run_agent): структура извлекается из
block.text через extract_json, доменная валидация — поверх извлечённой структуры. Хуки
before_call/after_call/on_attempt_failure инъектируются (здесь — no-op с учётом числа
вызовов/usage), как это делает task-слой.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.pipeline.agents import agent2, agent3
from app.pipeline.agents.claude_client import AgentCall
from app.pipeline.language import DetectedLanguage
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


class _FakeTextClient:
    """Фейк ClaudeAgentClient.run_agent (ADR-020 §I.1 revised): структура из block.text.

    Очередь ответов; dict → сериализуется в JSON-текст (как модель в текстовом режиме);
    строка → подаётся как есть (для невалидного/не-JSON текста).
    """

    def __init__(self, responses):  # noqa: ANN001
        self._responses = list(responses)
        self.calls = 0
        self.user_contents: list[str] = []

    async def run_agent(  # noqa: ANN201
        self,
        *,
        agent,
        model,
        system_prompt,
        user_content,  # noqa: ANN001
    ):
        self.calls += 1
        self.user_contents.append(user_content)
        resp = self._responses.pop(0) if self._responses else {}
        text = json.dumps(resp) if isinstance(resp, dict) else str(resp)
        return _call(text)


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
    client = _FakeTextClient(responses)
    monkeypatch.setattr(agent2, "ClaudeAgentClient", lambda s: client)
    return client


def _wire3(monkeypatch, responses):  # noqa: ANN001, ANN202
    client = _FakeTextClient(responses)
    monkeypatch.setattr(agent3, "ClaudeAgentClient", lambda s: client)
    return client


async def test_agent2_returns_stripped_spec(settings, monkeypatch):
    # Каноническая форма §I.1a: spec_markdown начинается маркером **Content language:**.
    # Ведущие/хвостовые пробелы по краям всего значения strip'аются валидатором (§I.5 сценарий 3:
    # `\n\n**Content language:**…` → strip → проходит startswith).
    spec_in = "  **Content language:** English (en)\n# Spec\nbody  "
    spec_out = "**Content language:** English (en)\n# Spec\nbody"
    client = _wire2(monkeypatch, [{"spec_markdown": spec_in}])
    before, after, on_fail, st = _hooks()
    # ADR-028: run_agent2 принимает серверную language-директиву (детерминированный детект из
    # исходного промпта), НЕ само-детект модели.
    language = DetectedLanguage(bcp47="en", name="English")
    result = await agent2.run_agent2(
        settings,
        "prompt",
        [("Q1", "A1")],
        language,
        before_call=before,
        after_call=after,
        on_attempt_failure=on_fail,
    )
    assert result.spec_markdown == spec_out
    assert result.call.cost_usd == Decimal("0.0001")
    assert st["before"] == 1 and st["after"] == 1
    # Contract (ADR-028 §4): серверная директива с marker_value инжектируется в собранный
    # ввод Agent 2 (проверка реального user_content, не слепого мока).
    assert "Generate all user-facing content in English (en)." in client.user_contents[0]
    assert "**Content language:** English (en)" in client.user_contents[0]


async def test_agent2_empty_spec_retries_then_raises(settings, monkeypatch):
    """Пустая спека — schema-фейл: ретраится до AGENT_OUTPUT_MAX_RETRIES, затем терминал."""
    n = settings.agent_output_max_retries + 1
    _wire2(monkeypatch, [{"spec_markdown": "   "}] * n)
    before, after, on_fail, st = _hooks()
    from app.pipeline.agents.structured import StructuredOutputError

    language = DetectedLanguage(bcp47="en", name="English")
    with pytest.raises(StructuredOutputError, match="empty specification"):
        await agent2.run_agent2(
            settings,
            "prompt",
            [],
            language,
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
