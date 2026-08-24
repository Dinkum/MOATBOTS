from __future__ import annotations

from dataclasses import dataclass

from app.models import Agent, Run, WakeEvent
from app.services.team import TeamService


@dataclass
class RuntimeResult:
    decision: str
    note: str = ""
    turns: int = 1


class Runtime:
    async def run(
        self,
        team: TeamService,
        agent: Agent,
        event: WakeEvent,
        run: Run,
    ) -> RuntimeResult:
        raise NotImplementedError
