"""Unit: обёртки агентов 2/3 поверх ClaudeAgentClient (фейк-клиент, без сети)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.pipeline.agents import agent2, agent3
from app.pipeline.agents.claude_client import AgentCall
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


class _FakeClient:
    def __init__(self, text):  # noqa: ANN001
        self._text = text

    async def run_agent(self, *, model, system_prompt, user_content):  # noqa: ANN001, ANN003, ANN202
        return _call(self._text)


@pytest.fixture
def settings():  # noqa: ANN201
    return get_settings()


async def test_agent2_returns_stripped_spec(settings, monkeypatch):
    monkeypatch.setattr(agent2, "ClaudeAgentClient", lambda s: _FakeClient("  # Spec\nbody  "))
    result = await agent2.run_agent2(settings, "prompt", [("Q1", "A1")])
    assert result.spec_markdown == "# Spec\nbody"
    assert result.call.cost_usd == Decimal("0.0001")


async def test_agent2_empty_spec_raises(settings, monkeypatch):
    monkeypatch.setattr(agent2, "ClaudeAgentClient", lambda s: _FakeClient("   "))
    with pytest.raises(ValueError, match="empty specification"):
        await agent2.run_agent2(settings, "prompt", [])


async def test_agent3_valid_tree(settings, monkeypatch):
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    tree_json = json.dumps(
        {
            "files": [
                {"path": "package.json", "encoding": "utf8", "content": pkg},
                {"path": "index.html", "encoding": "utf8", "content": "<html></html>"},
            ],
            "entry": "index.html",
            "build": {"command": "npm ci && vite build"},
        }
    )
    monkeypatch.setattr(agent3, "ClaudeAgentClient", lambda s: _FakeClient(tree_json))
    result = await agent3.run_agent3(settings, "spec")
    assert result.tree.entry == "index.html"
    assert result.call is not None


async def test_agent3_not_json_raises_with_call(settings, monkeypatch):
    monkeypatch.setattr(agent3, "ClaudeAgentClient", lambda s: _FakeClient("{not json"))
    with pytest.raises(AgentOutputError) as ei:
        await agent3.run_agent3(settings, "spec")
    assert ei.value.signature == "agent3_not_json"
    # call прокинут (для cost-ledger при невалид-output).
    assert isinstance(ei.value.call, AgentCall)


async def test_agent3_invalid_tree_propagates_call(settings, monkeypatch):
    bad = json.dumps({"files": [], "entry": "x", "build": {"command": "vite build"}})
    monkeypatch.setattr(agent3, "ClaudeAgentClient", lambda s: _FakeClient(bad))
    with pytest.raises(AgentOutputError) as ei:
        await agent3.run_agent3(settings, "spec")
    assert isinstance(ei.value.call, AgentCall)
