from collections.abc import AsyncIterator

from fastapi import Request

from app.services.team import TeamService


async def get_team(request: Request) -> AsyncIterator[TeamService]:
    async with request.app.state.session_factory() as session:
        team = TeamService(
            session,
            request.app.state.clock,
            request.app.state.kanban,
            request.app.state.signal,
            request.app.state.settings.budgets,
            request.app.state.portal,
            request.app.state.profiles,
            request.app.state.settings.agent_state_dir,
            request.app.state.settings.agent_workspace,
        )
        try:
            yield team
            await session.commit()
        except Exception:
            await session.rollback()
            raise
