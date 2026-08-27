from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.clock import Clock
from app.config import get_settings
from app.database import create_schema, make_engine, make_session_factory
from app.dispatcher import Dispatcher
from app.logger import configure_logging, emit, request_id_context, stop_logging
from app.routes import agent_api, api, pages
from app.runtime.hermes import HermesRuntime
from app.seed import seed_roster
from app.services.auth import AuthService
from app.services.demonstrations import DemonstrationService
from app.services.desktop import DesktopService
from app.services.inbox import accept_human_text
from app.services.kanban import HermesKanban
from app.services.portal import ManagedPortal, build_portal
from app.services.profiles import HermesProfileProvisioner
from app.services.team import NewEventSignal, TeamService

APP_VERSION = json.loads(Path("version.json").read_text(encoding="utf-8"))["version"]
ASSET_VERSION = hashlib.sha256(
    b"".join(Path(path).read_bytes() for path in ("app/static/app.css", "app/static/app.js"))
).hexdigest()[:12]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.debug_logging)
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    await create_schema(engine)
    clock = Clock()
    signal = NewEventSignal()
    auth = AuthService(settings)
    desktop = DesktopService(settings)
    demonstrations = DemonstrationService("scripts/agent-computer.sh", settings.agent_workspace)
    portal = ManagedPortal(build_portal(settings))
    kanban = HermesKanban(settings.hermes_binary, settings.hermes_home)
    runtime = HermesRuntime(settings, auth)
    profiles = HermesProfileProvisioner(
        settings.hermes_binary,
        settings.hermes_home,
        state_root=settings.agent_state_dir,
    )
    async with session_factory() as session:
        team = TeamService(
            session,
            clock,
            kanban,
            signal,
            settings.budgets,
            portal,
            profiles,
            settings.agent_state_dir,
            settings.agent_workspace,
        )
        await seed_roster(session, settings, team)
        await session.commit()
    dispatcher = Dispatcher(
        session_factory,
        clock,
        kanban,
        signal,
        runtime,
        settings.budgets,
        portal,
        profiles,
        settings.agent_state_dir,
        settings.agent_workspace,
    )

    async def on_portal_text(text: str) -> None:
        default = portal.last_peer or (settings.agents[0].name if settings.agents else "chief")
        async with session_factory() as session:
            team = TeamService(
                session,
                clock,
                kanban,
                signal,
                settings.budgets,
                portal,
                profiles,
                settings.agent_state_dir,
                settings.agent_workspace,
            )
            peer = await accept_human_text(team, text, default)
            portal.last_peer = peer
            await session.commit()

    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.clock = clock
    app.state.kanban = kanban
    app.state.signal = signal
    app.state.auth = auth
    app.state.desktop = desktop
    app.state.demonstrations = demonstrations
    app.state.portal = portal
    app.state.profiles = profiles
    app.state.dispatcher = dispatcher
    app.state.version = APP_VERSION
    app.state.asset_version = ASSET_VERSION
    for key in (
        "settings",
        "session_factory",
        "clock",
        "kanban",
        "signal",
        "portal",
        "profiles",
    ):
        setattr(agent_app.state, key, getattr(app.state, key))
    agent_server = uvicorn.Server(
        uvicorn.Config(
            agent_app,
            host=settings.agent_api_host,
            port=settings.agent_api_port,
            log_level="warning",
            access_log=False,
        )
    )
    agent_server.install_signal_handlers = lambda: None
    agent_server_task = asyncio.create_task(agent_server.serve())
    for _ in range(100):
        if agent_server.started:
            break
        if agent_server_task.done():
            raise RuntimeError("Agent tool listener failed to start")
        await asyncio.sleep(0.02)
    else:
        raise RuntimeError("Agent tool listener did not become ready")
    await portal.start(on_portal_text)
    tasks = [agent_server_task, asyncio.create_task(dispatcher.run_forever())]
    emit("app", "startup", "Moatbots started", version=APP_VERSION)
    yield
    dispatcher.stop()
    await portal.close()
    agent_server.should_exit = True
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task
    await engine.dispose()
    stop_logging()


app = FastAPI(title="Moatbots", version=APP_VERSION, lifespan=lifespan)
agent_app = FastAPI(title="Moatbots Agent Tools", docs_url=None, redoc_url=None, openapi_url=None)
agent_app.include_router(agent_api.router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(pages.router)
app.include_router(api.router)


@app.middleware("http")
async def request_context(request: Request, call_next):
    token = request_id_context.set(str(uuid4()))
    try:
        return await call_next(request)
    finally:
        request_id_context.reset(token)
