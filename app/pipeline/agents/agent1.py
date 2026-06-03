"""Agent 1 (Interviewer): промт → список уточняющих вопросов.

Output → таблица questions (docs/modules/pipeline/03-architecture.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.observability.timing import timed_agent_call
from app.pipeline.agents.claude_client import AgentCall, ClaudeAgentClient
from app.pipeline.prompts import load_prompt

_SYSTEM_PROMPT = load_prompt("agent1_interviewer")


@dataclass(frozen=True)
class ParsedQuestion:
    position: int
    text: str
    kind: str | None
    options: list[Any] | None


@dataclass(frozen=True)
class Agent1Result:
    questions: list[ParsedQuestion]
    call: AgentCall


def _parse_questions(text: str) -> list[ParsedQuestion]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("agent1 output is not valid JSON") from exc
    raw = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("agent1 output missing non-empty 'questions'")

    questions: list[ParsedQuestion] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError("agent1 question must be an object")
        q_text = item.get("text")
        if not isinstance(q_text, str) or not q_text.strip():
            raise ValueError("agent1 question text must be a non-empty string")
        position = item.get("position")
        if not isinstance(position, int):
            position = idx
        kind = item.get("kind")
        kind = kind if kind in ("free_text", "choice") else None
        options = item.get("options")
        if kind == "choice":
            if not isinstance(options, list) or not options:
                raise ValueError("agent1 choice question must have non-empty options")
        else:
            options = None
        questions.append(ParsedQuestion(position=position, text=q_text, kind=kind, options=options))
    return questions


async def run_agent1(settings: Settings, prompt: str) -> Agent1Result:
    client = ClaudeAgentClient(settings)
    with timed_agent_call("agent1", settings.agent1_model):
        call = await client.run_agent(
            model=settings.agent1_model,
            system_prompt=_SYSTEM_PROMPT,
            user_content=f"User website idea:\n\n{prompt}",
        )
    questions = _parse_questions(call.text)
    return Agent1Result(questions=questions, call=call)
