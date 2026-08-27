from __future__ import annotations

import asyncio

from app.clock import Clock
from app.config import BudgetSettings
from app.logger import emit
from app.models import WakeEvent
from app.runtime.base import Runtime
from app.services.kanban import KanbanBoard
from app.services.portal import NullPortal, Portal
from app.services.profiles import ProfileProvisioner, UnavailableProfileProvisioner
from app.services.team import NewEventSignal, TeamService


class Dispatcher:
    """Sleeps until a due event or a new-event signal, then wakes one Hermes profile."""

    def __init__(
        self,
        session_factory,
        clock: Clock,
        kanban: KanbanBoard,
        signal: NewEventSignal,
        runtime: Runtime,
        budgets: BudgetSettings,
        portal: Portal | None = None,
        profiles: ProfileProvisioner | None = None,
        agent_state_dir: str = ".moatbots-agents",
        workspace_dir: str = "workspace",
    ) -> None:
        self.session_factory = session_factory
        self.clock = clock
        self.kanban = kanban
        self.signal = signal
        self.runtime = runtime
        self.budgets = budgets
        self.portal = portal or NullPortal()
        self.profiles = profiles or UnavailableProfileProvisioner()
        self.agent_state_dir = agent_state_dir
        self.workspace_dir = workspace_dir
        self._stopped = asyncio.Event()
        self._capacity = asyncio.Semaphore(budgets.max_concurrency)
        self._shared_computer = asyncio.Semaphore(1)

    def stop(self) -> None:
        self._stopped.set()
        self.signal.notify()

    async def run_forever(self) -> None:
        emit("dispatcher", "start", "Dispatcher loop started")
        while not self._stopped.is_set():
            handled = await self.drain()
            if handled:
                continue
            async with self.session_factory() as session:
                team = self._team(session)
                nxt = await team.earliest_due()
            if nxt is None:
                await self.signal.wait(None)
                continue
            remaining = (nxt - self.clock.now()).total_seconds()
            if remaining <= 0:
                continue
            await self.signal.wait(remaining)

    async def drain(self, max_rounds: int = 24) -> int:
        total = 0
        for _ in range(max_rounds):
            async with self.session_factory() as session:
                team = self._team(session)
                await team.fire_due_routines_and_loops()
                events = await team.claim_due_events(limit=self.budgets.max_concurrency)
                claimed = [event.id for event in events]
                await session.commit()
            if not claimed:
                return total
            await asyncio.gather(*(self._run_claimed(event_id) for event_id in claimed))
            total += len(claimed)
        emit("dispatcher", "unsettle", "Drain hit the round cap")
        return total

    async def _run_claimed(self, event_id: str) -> None:
        async with self._capacity, self.session_factory() as session:
            event = await session.get(WakeEvent, event_id)
            if event is None:
                return
            lane = self._shared_computer if event.execution_lane == "shared" else _NullLane()
            async with lane:
                team = self._team(session)
                try:
                    await self._handle(team, event)
                except asyncio.CancelledError:
                    await team.release_claim(event, "Dispatcher stopped during the run")
                    await session.commit()
                    raise
                await session.commit()

    async def _handle(self, team: TeamService, event: WakeEvent) -> None:
        agent = await team.get_agent(event.target_id)
        run = await team.start_run(event)
        # Release the write lock before a Hermes subprocess so MCP/UI can commit.
        await team.db.commit()
        try:
            result = await self.runtime.run(team, agent, event, run)
        except Exception as exc:
            await team.ignore_event(event, run, str(exc)[:400])
            await team.settle_group_round(event)
            emit("dispatcher", "run.error", str(exc), agent=agent.name)
            return
        run.turns_used = result.turns
        run.cost_usd = result.cost_usd
        run.provider = result.provider
        run.model = result.model
        if result.decision == "ignore":
            await team.ignore_event(event, run, result.note)
        else:
            await team.complete_event(event, run, result.decision, result.note)
        await team.settle_group_round(event)
        emit(
            "dispatcher",
            "run.done",
            f"{agent.name} {result.decision}",
            agent=agent.name,
            decision=result.decision,
        )

    def _team(self, session) -> TeamService:
        return TeamService(
            session,
            self.clock,
            self.kanban,
            self.signal,
            self.budgets,
            self.portal,
            self.profiles,
            self.agent_state_dir,
            self.workspace_dir,
        )


class _NullLane:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
