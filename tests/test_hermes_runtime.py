from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import Activity
from app.runtime.hermes import HermesRuntime
from app.services.team import TeamService
from tests.conftest import open_team


class FakeAuth:
    def hermes_invocation(self, _preferred=None):
        return "openrouter", "test-model", {}


def test_chief_charter_leads_with_role_and_operating_job():
    charter = (Path(__file__).parents[1] / "app" / "prompts" / "chief.md").read_text(
        encoding="utf-8"
    )
    assert charter.startswith(
        "You are a Chief of Staff.\nYour job is to delegate, manage, and verify,"
    )
    assert "A failed check remains a failure" in charter
    assert "not to be the default individual contributor" in charter


@pytest.mark.asyncio
async def test_runtime_releases_context_write_before_subprocess(world, tmp_path: Path, monkeypatch):
    binary = tmp_path / "hermes"
    binary.touch()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        hermes_binary=str(binary),
        hermes_home=str(tmp_path / "agents"),
        agent_workspace=str(workspace),
        allow_host_hermes=True,
    )

    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        dm = await team.get_or_create_dm(you, chief)
        await team.send_message(you, dm.id, "Please inspect this")

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
        event = (await team.claim_due_events())[0]
        run = await team.start_run(event)
        await session.commit()

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                async with world["factory"]() as other_session:
                    other_session.add(Activity(kind="test", title="parallel tool write", detail=""))
                    await other_session.commit()
                return b"finished", b""

        command = []

        async def create_subprocess(*args, **_kwargs):
            command.extend(args)
            return FakeProcess()

        monkeypatch.setattr("app.runtime.hermes.asyncio.create_subprocess_exec", create_subprocess)
        result = await HermesRuntime(settings, FakeAuth()).run(team, chief, event, run)

    assert result.decision == "acted"
    assert "--provider" not in command
    assert "--model" not in command
    assert result.turns is None
    assert result.cost_usd is None
    async with world["factory"]() as session:
        assert (
            await session.execute(select(Activity).where(Activity.title == "parallel tool write"))
        ).scalar_one()
