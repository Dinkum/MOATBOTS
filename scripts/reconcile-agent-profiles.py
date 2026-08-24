from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.database import make_engine, make_session_factory  # noqa: E402
from app.models import Agent  # noqa: E402
from app.services.profiles import HermesProfileProvisioner  # noqa: E402


async def reconcile() -> None:
    settings = Settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    provisioner = HermesProfileProvisioner(
        settings.hermes_binary,
        settings.hermes_home,
        ROOT,
        state_root=settings.agent_state_dir,
    )
    try:
        async with factory() as session:
            agents = list(
                await session.scalars(
                    select(Agent)
                    .where(Agent.kind == "hermes", Agent.retired_at.is_(None))
                    .order_by(Agent.created_at)
                )
            )
        for agent in agents:
            if not agent.hermes_profile:
                continue
            await provisioner.create(
                agent.hermes_profile,
                agent.role_title,
                agent.job_description,
            )
            print(f"agent profile ready -> {agent.hermes_profile}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(reconcile())
