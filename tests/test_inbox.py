import pytest

from app.services.inbox import accept_human_text
from app.services.portal import parse_inbound
from tests.conftest import open_team


def test_parse_at_mention():
    assert parse_inbound("@researcher look at billing", ["chief", "researcher"], "chief") == (
        "researcher",
        "look at billing",
    )


def test_parse_falls_back_when_name_unknown():
    assert parse_inbound("just a thought", ["chief"], "chief") == (
        "chief",
        "just a thought",
    )


@pytest.mark.asyncio
async def test_portal_text_lands_in_dm_and_wakes(world):
    async with open_team(world) as team:
        await accept_human_text(team, "@chief monitor the deploy feed", "researcher")
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        dm = await team.get_or_create_dm(you, chief)
        bodies = [row.body for row in await team.history(dm.id)]
        assert "monitor the deploy feed" in bodies
