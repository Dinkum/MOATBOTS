import pytest

from tests.conftest import open_team


@pytest.mark.asyncio
async def test_monitor_then_silence_then_team_handoff(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        dm = await team.get_or_create_dm(you, chief)
        await team.send_message(you, dm.id, "Monitor the deploy feed for failures")

    await world["dispatcher"].drain()

    async with open_team(world) as team:
        routines = await team.list_routines()
        assert routines
        assert routines[0].source == "deploy"
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        dm = await team.get_or_create_dm(you, chief)
        noise = await team.ingest_webhook("deploy", {"status": "ok", "message": "all green"})
        assert noise == []
        quiet = [row.body for row in await team.history(dm.id)]
        assert not any("rollback" in row.lower() for row in quiet)

        await team.ingest_webhook(
            "deploy",
            {"status": "failed", "service": "billing", "message": "billing crash loop"},
        )

    await world["dispatcher"].drain()

    async with open_team(world) as team:
        rooms = await team.list_rooms(type="task")
        assert rooms
        assert rooms[0].lifecycle == "archived"
        assert rooms[0].resolved_result
        tasks = await team.list_tasks()
        assert tasks
        assert tasks[0].last_status == "done"
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        researcher = await team.get_agent("researcher")
        members = {row.agent.name for row in rooms[0].memberships}
        assert {chief.name, researcher.name} <= members
        dm = await team.get_or_create_dm(you, chief)
        history = " ".join(row.body for row in await team.history(dm.id)).lower()
        assert "rollback" in history
