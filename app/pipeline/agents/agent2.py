"""Agent 2 (Spec Writer): промт + ответы → финальная спека (ТЗ, Markdown).

Output → generation_jobs.spec_tz (inline ≤ 16 KB) или spec_ref в S3.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.observability.timing import timed_agent_call
from app.pipeline.agents.claude_client import AgentCall, ClaudeAgentClient
from app.pipeline.prompts import load_prompt

_SYSTEM_PROMPT = load_prompt("agent2_spec_writer")


@dataclass(frozen=True)
class Agent2Result:
    spec_markdown: str
    call: AgentCall


def _build_user_content(prompt: str, qa_pairs: list[tuple[str, str]]) -> str:
    lines = [f"Original user prompt:\n{prompt}\n", "Clarifying questions and answers:"]
    for idx, (question, answer) in enumerate(qa_pairs, start=1):
        lines.append(f"{idx}. Q: {question}")
        lines.append(f"   A: {answer}")
    lines.append("\nWrite the specification now.")
    return "\n".join(lines)


async def run_agent2(
    settings: Settings, prompt: str, qa_pairs: list[tuple[str, str]]
) -> Agent2Result:
    client = ClaudeAgentClient(settings)
    with timed_agent_call("agent2", settings.agent2_model):
        call = await client.run_agent(
            model=settings.agent2_model,
            system_prompt=_SYSTEM_PROMPT,
            user_content=_build_user_content(prompt, qa_pairs),
        )
    spec = call.text.strip()
    if not spec:
        raise ValueError("agent2 produced an empty specification")
    return Agent2Result(spec_markdown=spec, call=call)
