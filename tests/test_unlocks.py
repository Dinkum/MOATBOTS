from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest
from sqlalchemy import select

from app.errors import TeamError
from app.models import Agent, OpenLoop, Routine, Run, WakeEvent
from app.runtime.base import Runtime, RuntimeResult
from app.services.kanban import HermesKanban
from app.services.team import TeamService
from tests.conftest import open_team


async def test_idle_dispatcher_sees_a_wake_only_after_commit(world, monkeypatch):
    dispatcher = world["dispatcher"]
    signal = world["signal"]
    sleeping = asyncio.Event()
    started = asyncio.Event()
    wait = signal.wait

    async def wait_for_signal(timeout):
        assert timeout is None
        sleeping.set()
        await wait(timeout)

    async def run(*args):
        started.set()
        return RuntimeResult("acted")

    monkeypatch.setattr(signal, "wait", wait_for_signal)
    monkeypatch.setattr(dispatcher.runtime, "run", run)
    dispatch = asyncio.create_task(dispatcher.run_forever())
    try:
        await asyncio.wait_for(sleeping.wait(), 2)
        async with open_team(world) as team:
            chief = await team.get_agent("chief")
            await team.create_routine(chief, "aborted", interval_seconds=60)
            await team.db.rollback()
            await team.db.commit()
            assert not signal.event.is_set()
            chief = await team.get_agent("chief")
            you = await team.get_agent("you")
            dm = await team.get_or_create_dm(you, chief)
            await team.send_message(you, dm.id, "Please take a look")
            assert not signal.event.is_set()
        await asyncio.wait_for(started.wait(), 2)
    finally:
        dispatcher.stop()
        await asyncio.wait_for(dispatch, 2)


async def test_retirement_cancels_queued_work_before_run_admission(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        worker = await team.upsert_agent("worker", kind="hermes")
        routine = await team.create_routine(worker, "interval", interval_seconds=60)
        due = await team.enqueue_wake(target=worker, actor=chief, reason="ready")
        later = world["clock"].advance(seconds=1)
        loop = await team.schedule_loop(chief, "later", worker.name, later)
        await team.claim_due_events(limit=1)

    async with open_team(world) as claimant:
        claimed = await claimant.db.get_one(WakeEvent, due.id)
        assert claimed.status == "claimed"
        async with open_team(world) as team:
            chief = await team.get_agent("chief")
            with pytest.raises(TeamError, match="Only teammates"):
                await team.retire_agent(chief, "you")
            await team.retire_agent(chief, worker.name)
        assert await claimant.start_run(claimed) is None

    world["clock"].advance(seconds=61)
    assert await world["dispatcher"].drain() == 0
    async with open_team(world) as team:
        assert {row.status for row in await team.db.scalars(select(WakeEvent))} == {"cancelled"}
        saved = await team.db.get_one(Routine, routine.id)
        assert (saved.active, saved.next_due_at) == (0, None)
        assert (await team.db.get_one(OpenLoop, loop.id)).status == "cancelled"
        assert await team.earliest_due() is None


async def test_startup_clears_work_left_by_previously_retired_agents(world):
    async with open_team(world) as team:
        worker = await team.upsert_agent("worker", kind="hermes")
        routine = await team.create_routine(worker, "legacy interval", interval_seconds=60)
        event = await team.enqueue_wake(target=worker, actor=None, reason="legacy wake")
        worker.retired_at = world["clock"].now()
        worker.status = "retired"

    async with open_team(world) as team:
        await team.cancel_retired_work()
        assert await team.earliest_due() is None
        assert (await team.db.get_one(WakeEvent, event.id)).status == "cancelled"
        assert not (await team.db.get_one(Routine, routine.id)).active


@pytest.mark.parametrize("outcome", ["done", "cancelled", "lease_expired"])
async def test_run_settlement_preserves_retirement(world, outcome):
    async with open_team(world) as team:
        worker = await team.upsert_agent("worker", kind="hermes")
        event = await team.enqueue_wake(target=worker, actor=None, reason="test")
        await team.claim_due_events()
        run = await team.start_run(event)

    async with open_team(world) as running:
        await running.db.get_one(Agent, worker.id)
        event = await running.db.get_one(WakeEvent, event.id)
        run = await running.db.get_one(Run, run.id)
        async with open_team(world) as team:
            chief = await team.get_agent("chief")
            await team.retire_agent(chief, worker.name)
            assert (await team.db.get_one(WakeEvent, event.id)).status == "claimed"
        if outcome == "done":
            await running.complete_event(event, run, "acted")
        elif outcome == "cancelled":
            await running.release_claim(event, "shutdown")
        else:
            world["clock"].advance(seconds=world["budgets"].wake_lease_seconds + 1)
            assert await running.claim_due_events() == []

    async with open_team(world) as team:
        assert (await team.db.get_one(Agent, worker.id)).status == "retired"
        assert (await team.db.get_one(WakeEvent, event.id)).status == (
            "done" if outcome == "done" else "cancelled"
        )
        saved = await team.db.get_one(Run, run.id)
        assert saved.status == ("error" if outcome == "lease_expired" else outcome)


@pytest.mark.parametrize("raises", [False, True])
async def test_runtime_failures_are_terminal_errors(world, monkeypatch, raises):
    async def fail(team, agent, event, run):
        if raises:
            raise RuntimeError("Hermes failed")
        return RuntimeResult("error", "Hermes failed")

    dispatcher = world["dispatcher"]
    monkeypatch.setattr(dispatcher.runtime, "run", fail)
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        event = await team.enqueue_wake(target=chief, actor=None, reason="test")
    assert await dispatcher.drain() == 1
    async with open_team(world) as team:
        event = await team.db.get_one(WakeEvent, event.id)
        run = await team.db.get_one(Run, event.claimed_run_id)
        assert (event.status, run.status, run.decision, run.note) == (
            "error",
            "error",
            "error",
            "Hermes failed",
        )


async def test_expired_claim_is_recovered_and_previous_run_is_closed(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        event = await team.enqueue_wake(target=chief, actor=None, reason="test")
        claimed = (await team.claim_due_events())[0]
        previous = await team.start_run(claimed)
        assert event and event.lease_expires_at

    world["clock"].advance(seconds=world["budgets"].wake_lease_seconds + 1)
    async with open_team(world) as team:
        reclaimed = await team.claim_due_events()
        assert [row.id for row in reclaimed] == [event.id]
        old_run = await team.db.get(Run, previous.id)
        assert old_run and old_run.status == "error"
        assert old_run.decision == "lease_expired"
        assert reclaimed[0].attempts == 2


async def test_routine_lifecycle_history_and_self_expiration(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        routine = await team.create_routine(
            chief,
            "one shot",
            source="github",
            max_runs=1,
        )
        events = await team.ingest_webhook(
            "github", {"event_type": "pull_request", "action": "opened"}
        )
        assert len(events) == 1
        assert routine.active == 0
        history = await team.routine_history(chief, routine.id)
        assert history[0].trigger == "github"
        assert history[0].wake_event_id == events[0].id
        await team.retire_routine(chief, routine.id)
        assert (await team.routine_history(chief, routine.id))[0].id == history[0].id
        assert routine not in await team.list_routines(chief.id, include_inactive=True)


async def test_human_choice_resolution_resumes_same_agent(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        human = await team.get_agent("you")
        request = await team.create_human_request(
            chief,
            kind="choice",
            prompt="Which release channel?",
            options=["stable", "preview"],
        )
        await team.resolve_human_request(human, request.id, "stable")
        assert request.status == "resolved"
        wakes = list(
            (
                await team.db.execute(select(WakeEvent).where(WakeEvent.reason == "human_response"))
            ).scalars()
        )
        assert len(wakes) == 1
        assert wakes[0].target_id == chief.id
        assert request.id in wakes[0].payload_json


async def test_human_secret_bypasses_database_and_model_payload(world, tmp_path):
    async with world["factory"]() as session:
        team = TeamService(
            session,
            world["clock"],
            world["kanban"],
            world["signal"],
            world["budgets"],
            world["portal"],
            world["profiles"],
            str(tmp_path / "agents"),
        )
        chief = await team.get_agent("chief")
        human = await team.get_agent("you")
        request = await team.create_human_request(
            chief,
            kind="secret",
            prompt="Add the service credential",
            secret_name="SERVICE_TOKEN",
        )
        await team.resolve_human_request(human, request.id, "never-store-this")
        await session.commit()

        secret_file = tmp_path / "agents" / "chief" / "secrets.env"
        assert secret_file.read_text(encoding="utf-8") == "SERVICE_TOKEN=never-store-this\n"
        assert secret_file.stat().st_mode & 0o777 == 0o600
        assert "never-store-this" not in request.response_json
        wake = (
            await session.execute(select(WakeEvent).where(WakeEvent.reason == "human_response"))
        ).scalar_one()
        assert "never-store-this" not in wake.payload_json


async def test_search_is_full_text_and_membership_scoped(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        human = await team.get_agent("you")
        outsider = await team.upsert_agent("outsider", kind="hermes", profile="outsider")
        dm = await team.get_or_create_dm(human, chief)
        sent = await team.send_message(human, dm.id, "quartz falcon incident")
        found = await team.search_messages(chief, "quartz falcon")
        hidden = await team.search_messages(outsider, "quartz falcon")
        assert [row.id for row in found] == [sent.id]
        assert hidden == []


async def test_moatbots_is_the_only_task_execution_owner(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        human = await team.get_agent("you")
        dm = await team.get_or_create_dm(human, chief)
        ref = await team.create_task(human, dm.id, chief.name, "Verify the report")
        card = await world["kanban"].show(ref.hermes_task_id)
        assert card and card.assignee == ""
        wake = (
            await team.db.execute(select(WakeEvent).where(WakeEvent.context_reference == ref.id))
        ).scalar_one()
        assert wake.target_id == chief.id


async def test_hermes_card_is_created_without_worker_assignment(monkeypatch):
    board = HermesKanban()
    command = []

    async def fake_run(*args):
        command.extend(args)
        return '{"id":"task-1","status":"todo"}'

    monkeypatch.setattr(board, "_run", fake_run)
    card = await board.create("Verify", "", "Check it")
    assert card.assignee == ""
    assert "--assignee" not in command
    assert command[command.index("--created-by") + 1] == "moatbots"


async def test_headless_lane_overlaps_but_shared_computer_stays_single_file(world):
    class ObservedRuntime(Runtime):
        def __init__(self):
            self.active = 0
            self.shared = 0
            self.max_active = 0
            self.max_shared = 0
            self.lock = asyncio.Lock()

        async def run(self, team, agent, event, run):
            del team, agent, run
            async with self.lock:
                self.active += 1
                self.shared += event.execution_lane == "shared"
                self.max_active = max(self.max_active, self.active)
                self.max_shared = max(self.max_shared, self.shared)
            await asyncio.sleep(0.04)
            async with self.lock:
                self.active -= 1
                self.shared -= event.execution_lane == "shared"
            return RuntimeResult("done")

    observed = ObservedRuntime()
    world["dispatcher"].runtime = observed
    async with open_team(world) as team:
        agents = [
            await team.upsert_agent(f"lane_{index}", kind="hermes", profile=f"lane_{index}")
            for index in range(4)
        ]
        for index, agent in enumerate(agents):
            await team.enqueue_wake(
                target=agent,
                actor=None,
                reason="lane-test",
                execution_lane="shared" if index < 2 else "headless",
            )

    assert await world["dispatcher"].drain() == 4
    assert observed.max_active == 3
    assert observed.max_shared == 1


async def test_cancelled_dispatch_releases_claim_for_immediate_retry(world):
    class BlockingRuntime(Runtime):
        def __init__(self):
            self.started = asyncio.Event()

        async def run(self, team, agent, event, run):
            del team, agent, event, run
            self.started.set()
            await asyncio.Event().wait()
            return RuntimeResult("done")

    blocking = BlockingRuntime()
    world["dispatcher"].runtime = blocking
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        event = await team.enqueue_wake(target=chief, actor=None, reason="cancel-test")

    drain = asyncio.create_task(world["dispatcher"].drain())
    await blocking.started.wait()
    drain.cancel()
    with suppress(asyncio.CancelledError):
        await drain

    async with open_team(world) as team:
        released = await team.db.get(WakeEvent, event.id)
        run = (await team.db.execute(select(Run).where(Run.wake_event_id == event.id))).scalar_one()
        assert released and released.status == "pending"
        assert released.lease_expires_at is None
        assert run.status == "cancelled"
