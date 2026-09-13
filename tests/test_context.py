import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import Message, Notification
from app.runtime.hermes import HermesRuntime, _wake_prompt
from app.services.context import encode_context
from app.services.tools import Toolbelt
from tests.conftest import open_team
from tests.test_hermes_runtime import FakeAuth


async def add_notifications(world):
    async with open_team(world) as team:
        chief, you = await team.get_agent("chief"), await team.get_agent("you")
        room = await team.get_or_create_dm(you, chief)
        for i, body in enumerate(("long " * 4000, "Please check the deployment")):
            message = Message(
                id=f"context_message_{i}",
                room_id=room.id,
                sender_id=you.id,
                body=body,
                created_at=world["clock"].now() + timedelta(seconds=i),
            )
            team.db.add(message)
            await team.db.flush()
            team.db.add(
                Notification(
                    id=f"context_notification_{i}",
                    agent_id=chief.id,
                    room_id=room.id,
                    message_id=message.id,
                    kind="dm",
                    created_at=message.created_at,
                )
            )
        return room.id


async def test_wake_budget_retains_whole_notifications_or_unacknowledged_references(world):
    room_id = await add_notifications(world)
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        context = await team.load_context(chief, char_budget=6000, room_id=room_id)
        encoded = encode_context(context)
        assert len(encoded) <= 6000 and json.loads(encoded) == context
        long, short = context["notifications"]
        assert long["reference_only"] and long["message_id"] == "context_message_0"
        assert "body" not in long
        assert short["body"] == "Please check the deployment"
        assert context["more_available"]
        assert all(
            row.delivered_at is None
            for row in (await team.db.execute(select(Notification))).scalars()
        )
        await team.acknowledge_context(chief, context)
    async with open_team(world) as team:
        assert (await team.db.get(Notification, "context_notification_0")).delivered_at is None
        assert (await team.db.get(Notification, "context_notification_1")).delivered_at is not None
        chief = await team.get_agent("chief")
        full = await Toolbelt(team, chief).call("context.load", {})
        assert full["notifications"][0]["body"] == "long " * 4000
    async with open_team(world) as team:
        assert (await team.db.get(Notification, "context_notification_0")).delivered_at is not None


async def test_full_budget_is_valid_json_and_does_not_cut_notifications(world):
    async with open_team(world) as team:
        chief, you = await team.get_agent("chief"), await team.get_agent("you")
        room = await team.get_or_create_dm(you, chief)
        for i in range(60):
            message = Message(
                id=f"context_msg_{i:02d}",
                room_id=room.id,
                sender_id=you.id,
                body=f"notification {i} " + "context " * 60,
                created_at=world["clock"].now() + timedelta(seconds=i),
            )
            team.db.add(message)
            await team.db.flush()
            team.db.add(
                Notification(
                    id=f"context_ntf_{i:02d}",
                    agent_id=chief.id,
                    room_id=room.id,
                    message_id=message.id,
                    kind="dm",
                    created_at=message.created_at,
                )
            )
        await team.db.flush()
        context = await team.load_context(chief, char_budget=6000, room_id=room.id)
        event = await team.enqueue_wake(
            target=chief,
            actor=you,
            reason="test",
            payload={"room_id": room.id, "message": "trigger preserved"},
        )
        prompt = _wake_prompt(chief, event, context)
        office = prompt.split("Office state:\n")[1].split("\n\n")[0]
        parsed = json.loads(office)
        assert len(office) <= 6000 and parsed["more_available"]
        assert "trigger preserved" in prompt
        complete = [row for row in context["notifications"] if "body" in row]
        assert complete and all(row["body"].endswith("context ") for row in complete)
        await team.acknowledge_context(chief, context)
        delivered = list(
            (
                await team.db.execute(
                    select(Notification).where(Notification.delivered_at.is_not(None))
                )
            ).scalars()
        )
        assert {row.id for row in delivered} == {row["id"] for row in complete}


@pytest.mark.parametrize("outcome", ["failed-start", "failed-run", "success"])
async def test_runtime_acknowledges_only_delivered_context_after_success(
    world, tmp_path, monkeypatch, outcome
):
    room_id = await add_notifications(world)
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

    class Process:
        returncode = 1 if outcome == "failed-run" else 0

        async def communicate(self):
            return b"finished", b""

    async def create(*args, **kwargs):
        if outcome == "failed-start":
            raise OSError("cannot start")
        return Process()

    monkeypatch.setattr("app.runtime.hermes.asyncio.create_subprocess_exec", create)
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        event = await team.enqueue_wake(
            target=chief, actor=None, reason="test", payload={"room_id": room_id}
        )
        run = await team.start_run(event)
        await team.db.commit()
        runtime = HermesRuntime(settings, FakeAuth())
        if outcome == "failed-start":
            with pytest.raises(OSError):
                await runtime.run(team, chief, event, run)
        else:
            await runtime.run(team, chief, event, run)
    async with open_team(world) as team:
        assert (await team.db.get(Notification, "context_notification_0")).delivered_at is None
        short = await team.db.get(Notification, "context_notification_1")
        assert (short.delivered_at is not None) == (outcome == "success")
