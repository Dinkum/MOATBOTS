from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio

from app.clock import FrozenClock
from app.config import BudgetSettings, Settings
from app.database import create_schema, make_engine, make_session_factory
from app.dispatcher import Dispatcher
from app.seed import seed_roster
from app.services.kanban import MemoryKanban
from app.services.portal import MemoryPortal
from app.services.profiles import MemoryProfileProvisioner
from app.services.team import NewEventSignal, TeamService
from tests.scripted import ScriptedRuntime


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest_asyncio.fixture
async def world(tmp_path: Path, clock: FrozenClock, settings: Settings):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await create_schema(engine)
    factory = make_session_factory(engine)
    kanban = MemoryKanban()
    signal = NewEventSignal()
    portal = MemoryPortal()
    budgets = BudgetSettings()
    profiles = MemoryProfileProvisioner()
    async with factory() as session:
        team = TeamService(session, clock, kanban, signal, budgets, portal, profiles)
        await seed_roster(session, settings, team)
        await session.commit()
    dispatcher = Dispatcher(
        factory, clock, kanban, signal, ScriptedRuntime(), budgets, portal, profiles
    )
    yield {
        "factory": factory,
        "clock": clock,
        "kanban": kanban,
        "signal": signal,
        "portal": portal,
        "dispatcher": dispatcher,
        "settings": settings,
        "budgets": budgets,
        "profiles": profiles,
    }
    await engine.dispose()


@asynccontextmanager
async def open_team(world) -> AsyncIterator[TeamService]:
    async with world["factory"]() as session:
        team = TeamService(
            session,
            world["clock"],
            world["kanban"],
            world["signal"],
            world["budgets"],
            world["portal"],
            world["profiles"],
        )
        yield team
        await session.commit()
