"""Enum состояний джобы генерации (docs/03-data-model.md → generation_jobs.state).

Диспетчер маршрутизирует по этой колонке (docs/modules/pipeline/03-architecture.md).
"""

from __future__ import annotations

import enum


class JobState(enum.StrEnum):
    """State-machine генерации. Порядок happy-path:
    CREATED → INTERVIEWING → AWAITING_CLARIFICATION → SPECCING → BUILDING
            → DEPLOYING → LIVE. Ветки FIXING/FAILED.
    """

    CREATED = "CREATED"
    INTERVIEWING = "INTERVIEWING"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    SPECCING = "SPECCING"
    BUILDING = "BUILDING"
    DEPLOYING = "DEPLOYING"
    LIVE = "LIVE"
    FIXING = "FIXING"
    FAILED = "FAILED"


# Терминальные/устойчивые состояния — задач в очереди нет (docs/03-data-model.md).
PAUSED_STATES: frozenset[JobState] = frozenset(
    {JobState.AWAITING_CLARIFICATION, JobState.LIVE, JobState.FAILED}
)
TERMINAL_STATES: frozenset[JobState] = frozenset({JobState.FAILED})
