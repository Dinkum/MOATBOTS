import pytest
from sqlalchemy import select

from app.errors import SelfWakeRejected
from app.models import WakeEvent
from tests.conftest import open_team


@pytest.mark.asyncio
async def test_group_message_does_not_wake_unmentioned_members(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        await team.hire_agent(
            chief,
            "researcher",
            "Researcher",
            "Investigates operational evidence. Specializes in careful primary-source review.",
        )
        await team.create_room(you, "work", ["chief", "researcher"], "task", approved=True)
        room = (await team.list_rooms(type="task"))[0]
        await team.send_message(you, room.id, "just chatting, no one named")
        pending = [
            event
            for event in (await team.db.execute(select(WakeEvent))).scalars()
            if event.status == "pending"
        ]
        assert pending == []


@pytest.mark.asyncio
async def test_mention_wakes_only_named_agent(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        await team.hire_agent(
            chief,
            "researcher",
            "Researcher",
            "Investigates operational evidence. Specializes in careful primary-source review.",
        )
        researcher = await team.get_agent("researcher")
        room = await team.create_room(you, "work", ["chief", "researcher"], "task", approved=True)
        await team.send_message(you, room.id, "@researcher take this")
        events = list((await team.db.execute(select(WakeEvent))).scalars())
        targets = {event.target_id for event in events}
        assert researcher.id in targets
        assert chief.id not in targets


@pytest.mark.asyncio
async def test_self_wake_without_change_is_rejected(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        await team.enqueue_wake(
            target=chief,
            actor=chief,
            reason="agent_message",
            payload={"x": 1},
            state_hash="same",
        )
        with pytest.raises(SelfWakeRejected):
            await team.enqueue_wake(
                target=chief,
                actor=chief,
                reason="agent_message",
                payload={"x": 1},
                state_hash="same",
            )


@pytest.mark.asyncio
async def test_named_webhook_without_routine_does_not_wake_anyone(world):
    async with open_team(world) as team:
        created = await team.ingest_webhook(
            "deploy",
            {"status": "failed", "message": "crash"},
            agent_name="chief",
        )
        assert created == []


@pytest.mark.asyncio
async def test_future_self_wake_is_allowed(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        later = world["clock"].now().replace(year=2027)
        event = await team.enqueue_wake(
            target=chief,
            actor=chief,
            reason="open_loop",
            due_at=later,
            payload={"n": 1},
            state_hash="same",
        )
        assert event is not None
        assert event.due_at == later
