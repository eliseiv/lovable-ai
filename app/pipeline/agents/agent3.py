"""Agent 3 (Builder): спека → дерево файлов статик-сайта (JSON).

Output ВАЛИДИРУЕТСЯ строго по схеме (app.schemas.agent_output) ДО упаковки source.tgz.
Невалид → AgentOutputError → FAILED(invalid_agent_output).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.core.config import Settings
from app.observability.timing import timed_agent_call
from app.pipeline.agents.claude_client import AgentCall, ClaudeAgentClient
from app.pipeline.prompts import load_prompt
from app.schemas.agent_output import (
    AgentOutputError,
    ValidatedTree,
    validate_agent_output,
)

_SYSTEM_PROMPT = load_prompt("agent3_builder")


@dataclass(frozen=True)
class Agent3Result:
    tree: ValidatedTree
    call: AgentCall


async def run_agent3(settings: Settings, spec_markdown: str) -> Agent3Result:
    client = ClaudeAgentClient(settings)
    with timed_agent_call("agent3", settings.agent3_model):
        call = await client.run_agent(
            model=settings.agent3_model,
            system_prompt=_SYSTEM_PROMPT,
            user_content=f"Specification:\n\n{spec_markdown}\n\nProduce the file tree JSON.",
        )
    try:
        raw = json.loads(call.text)
    except json.JSONDecodeError as exc:
        raise AgentOutputError(
            "agent3 output is not valid JSON",
            signature="agent3_not_json",
            call=call,
        ) from exc
    try:
        tree = validate_agent_output(raw, settings)
    except AgentOutputError as exc:
        # Прокидываем call наверх, чтобы записать llm_usage даже при невалид-output.
        exc.call = call
        raise
    return Agent3Result(tree=tree, call=call)
