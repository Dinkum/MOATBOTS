from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import select, text

from app.models import HumanRequest, Routine, WakeEvent
from tests.conftest import open_team


async def test_future_wake_deadline_survives_a_new_session(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        await team.enqueue_wake(
            target=chief,
            actor=None,
            reason="test",
            due_at=world["clock"].now() + timedelta(seconds=60),
        )
    async with open_team(world) as team:
        assert ((await team.earliest_due()) - world["clock"].now()).total_seconds() == 60


async def test_unexpired_claim_schedules_its_own_recovery(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        await team.enqueue_wake(target=chief, actor=None, reason="test")
        event = (await team.claim_due_events())[0]
        await team.start_run(event)
        lease = event.lease_expires_at
    async with open_team(world) as team:
        assert await team.earliest_due() == lease
    world["clock"].advance(seconds=world["budgets"].wake_lease_seconds + 1)
    async with open_team(world) as team:
        assert [row.id for row in await team.claim_due_events()] == [event.id]


async def test_expiry_comparisons_survive_a_new_session(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        team.db.add(
            Routine(
                agent_id=chief.id,
                reason="expired",
                expires_at=world["clock"].now() - timedelta(seconds=1),
            )
        )
        team.db.add(
            HumanRequest(
                agent_id=chief.id,
                kind="choice",
                prompt="test",
                expires_at=world["clock"].now() + timedelta(seconds=60),
            )
        )
    async with open_team(world) as team:
        routine = (await team.db.execute(select(Routine))).scalar_one()
        assert not team._routine_available(routine)
        request = (await team.db.execute(select(HumanRequest))).scalar_one()
        assert request.expires_at > world["clock"].now()


async def test_datetime_storage_normalizes_offsets_and_reads_existing_utc(world):
    instant = world["clock"].now().astimezone(timezone(timedelta(hours=-4)))
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        team.db.add(WakeEvent(id="offset", target_id=chief.id, reason="test", due_at=instant))
        team.db.add(
            WakeEvent(
                id="naive",
                target_id=chief.id,
                reason="test",
                due_at=instant.astimezone(UTC).replace(tzinfo=None),
            )
        )
    async with open_team(world) as team:
        rows = list((await team.db.execute(select(WakeEvent))).scalars())
        assert all(row.due_at == world["clock"].now() for row in rows)
        stored = await team.db.scalar(text("SELECT due_at FROM wake_event WHERE id='offset'"))
        assert datetime.fromisoformat(stored) == world["clock"].now().replace(tzinfo=None)
