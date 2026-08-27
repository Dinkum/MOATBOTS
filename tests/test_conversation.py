import json

import pytest
from sqlalchemy import select

from app.errors import TeamError
from app.models import Activity, MessageReaction, Notification, WakeEvent
from app.services.tools import Toolbelt
from tests.conftest import open_team


async def hire_researcher(team, chief):
    return await team.hire_agent(
        chief,
        name="researcher",
        role_title="Researcher",
        job_description="Finds and checks primary sources for assigned work.",
        reports_to="chief",
    )


@pytest.mark.asyncio
async def test_dm_is_persistent_and_unique(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        first = await team.get_or_create_dm(you, chief)
        again = await team.get_or_create_dm(chief, you)
        assert first.id == again.id
        await team.send_message(you, first.id, "hello")
        history = await team.history(first.id)
        assert [row.body for row in history] == ["hello"]


@pytest.mark.asyncio
async def test_message_reaction_selects_switches_and_clears(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        room = await team.get_or_create_dm(you, chief)
        message = await team.send_message(chief, room.id, "pick one")

        assert await team.set_message_reaction(you, message.id, "up") == (room.id, "up")
        assert await team.set_message_reaction(you, message.id, "down") == (
            room.id,
            "down",
        )
        reaction = (
            await team.db.execute(
                select(MessageReaction).where(MessageReaction.message_id == message.id)
            )
        ).scalar_one()
        assert reaction.value == "down"

        assert await team.set_message_reaction(you, message.id, "down") == (room.id, None)
        assert (
            await team.db.execute(
                select(MessageReaction).where(MessageReaction.message_id == message.id)
            )
        ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_history_loads_reactors_for_aggregate_display(world):
    from app.routes.api import serialize_messages
    from app.routes.pages import templates

    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        room = await team.get_or_create_dm(you, chief)
        message = await team.send_message(chief, room.id, "ship it")

        await team.set_message_reaction(you, message.id, "up")
        await team.set_message_reaction(chief, message.id, "up")

        [loaded] = await team.history(room.id)
        assert sorted(reaction.agent.name for reaction in loaded.reactions) == ["chief", "you"]
        payload = serialize_messages([loaded], you.id)
        assert payload["messages"][0]["at"] == "2026-08-14T12:00:00Z"
        assert payload["messages"][0]["reactions"] == [
            {"value": "up", "agent": "chief", "mine": False},
            {"value": "up", "agent": "you", "mine": True},
        ]

        html = templates.get_template("chat_room.html").render(
            room=room,
            room_label="@chief",
            following=False,
            tasks=[],
            messages=[loaded],
            viewer_id=you.id,
            replies_by_root={},
            can_manage_channel=False,
        )
        assert 'class="message-meta"' in html
        assert '<span class="who">chief</span>' in html
        assert 'class="message-time" datetime="2026-08-14T12:00:00Z"' in html
        assert ">12:00 UTC</time>" in html
        assert '<span class="message-body">ship it</span>' in html
        assert 'title="👍 chief, you"' in html
        assert ">👍 2</span>" in html


@pytest.mark.asyncio
async def test_agent_can_open_and_join_public_channel(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        researcher = await hire_researcher(team, chief)
        room = await team.create_channel(chief, "standup", "Daily coordination")
        assert room.type == "channel"
        assert [row.title for row in await team.list_channels("stand")] == ["standup"]
        membership = await team.join_channel(researcher, "#standup")
        assert membership.notification_level == "mentions"


@pytest.mark.asyncio
async def test_channel_notification_override_controls_queue(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        room = await team.create_channel(chief, "operations", "Deploy coordination", ["you"])
        await team.send_message(chief, room.id, "deploy window moved")
        item = next(item for item in await team.list_inbox(you) if item.room.id == room.id)
        assert item.unread is False

        await team.set_channel_notifications(you, room.id, "all")
        await team.send_message(chief, room.id, "deploy starts now")
        item = next(item for item in await team.list_inbox(you) if item.room.id == room.id)
        assert item.unread is True

        await team.mark_room_read(you, room.id, item.last_message.id)
        item = next(item for item in await team.list_inbox(you) if item.room.id == room.id)
        assert item.unread is False

        await team.send_message(chief, room.id, "@you approval is still needed")
        item = next(item for item in await team.list_inbox(you) if item.room.id == room.id)
        assert item.unread is True

        await team.mark_room_read(you, room.id, item.last_message.id)
        await team.set_channel_notifications(you, room.id, "mentions")
        await team.send_message(chief, room.id, "deploy complete")
        item = next(item for item in await team.list_inbox(you) if item.room.id == room.id)
        assert item.unread is False


@pytest.mark.asyncio
async def test_channel_mention_enters_joined_members_queue(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        room = await team.create_channel(chief, "operations", "Deploy coordination", ["you"])
        await team.send_message(chief, room.id, "@you approve the deploy")

        item = next(item for item in await team.list_inbox(you) if item.room.id == room.id)
        assert item.unread is True


@pytest.mark.asyncio
async def test_general_is_seeded_with_everyone_on_mentions(world):
    async with open_team(world) as team:
        [general] = [room for room in await team.list_channels() if room.title == "general"]
        agents = {agent.name for agent in await team.list_agents()}
        assert {membership.agent.name for membership in general.memberships} == agents
        assert {membership.notification_level for membership in general.memberships} == {"mentions"}
        assert {
            membership.agent.name
            for membership in general.memberships
            if membership.role == "owner"
        } == {"chief", "you"}


@pytest.mark.asyncio
async def test_group_message_queues_and_wakes_every_other_agent(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        researcher = await hire_researcher(team, chief)
        room = await team.create_group_chat(chief, ["researcher"])
        message = await team.send_message(chief, room.id, "Please check this.")
        notification = (
            await team.db.execute(
                select(Notification).where(
                    Notification.agent_id == researcher.id,
                    Notification.message_id == message.id,
                )
            )
        ).scalar_one()
        assert notification.kind == "group"
        assert notification.wake_event_id is not None
        event = await team.db.get(WakeEvent, notification.wake_event_id)
        assert event is not None
        assert event.reason == "group_round"


@pytest.mark.asyncio
async def test_group_chat_is_capped_at_three_total_people(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        await hire_researcher(team, chief)
        await team.hire_agent(
            chief,
            "designer",
            "Designer",
            "Designs usable interfaces and specializes in visual interaction details.",
        )
        with pytest.raises(TeamError, match="at most 3"):
            await team.create_group_chat(you, ["chief", "researcher", "designer"])


@pytest.mark.asyncio
async def test_group_responses_are_collected_before_the_next_round(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        researcher = await hire_researcher(team, chief)
        room = await team.create_group_chat(you, ["chief", "researcher"])
        await team.send_message(you, room.id, "Should we ship this?")

        first_round = list(
            (
                await team.db.execute(select(WakeEvent).where(WakeEvent.reason == "group_round"))
            ).scalars()
        )
        assert {event.target_id for event in first_round} == {chief.id, researcher.id}
        chief_event = next(event for event in first_round if event.target_id == chief.id)
        researcher_event = next(event for event in first_round if event.target_id == researcher.id)

        chief_event.status = "claimed"
        chief_run = await team.start_run(chief_event)
        await Toolbelt(team, chief, chief_run).call(
            "messages.send", {"room": room.id, "message": "Yes. The evidence is solid."}
        )
        await team.complete_event(chief_event, chief_run, "acted")
        await team.settle_group_round(chief_event)
        assert len(list((await team.db.execute(select(WakeEvent))).scalars())) == 2

        researcher_event.status = "claimed"
        researcher_run = await team.start_run(researcher_event)
        await team.ignore_event(researcher_event, researcher_run, "nothing to add")
        await team.settle_group_round(researcher_event)

        events = list((await team.db.execute(select(WakeEvent))).scalars())
        second_round = [event for event in events if event.status == "pending"]
        assert len(second_round) == 1
        assert second_round[0].target_id == researcher.id
        payload = json.loads(second_round[0].payload_json)
        assert payload["round"] == 2
        assert [item["body"] for item in payload["messages"]] == ["Yes. The evidence is solid."]

        second_round[0].status = "claimed"
        final_run = await team.start_run(second_round[0])
        await team.ignore_event(second_round[0], final_run, "nothing to add")
        await team.settle_group_round(second_round[0])
        assert not [
            event
            for event in (await team.db.execute(select(WakeEvent))).scalars()
            if event.status == "pending"
        ]


@pytest.mark.asyncio
async def test_group_round_settles_without_recording_an_empty_round(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        await hire_researcher(team, chief)
        room = await team.create_group_chat(you, ["chief", "researcher"])
        await team.send_message(you, room.id, "Each teammate reply once")
        events = list(
            (
                await team.db.execute(select(WakeEvent).where(WakeEvent.reason == "group_round"))
            ).scalars()
        )

        for event, agent in (
            (events[0], await team.get_agent(events[0].target_id)),
            (events[1], await team.get_agent(events[1].target_id)),
        ):
            event.status = "claimed"
            run = await team.start_run(event)
            await Toolbelt(team, agent, run).call(
                "messages.send", {"room": room.id, "message": f"{agent.name} replied"}
            )
            await team.complete_event(event, run, "acted")
            await team.settle_group_round(event)

        activity = list(
            (
                await team.db.execute(
                    select(Activity)
                    .where(Activity.room_id == room.id)
                    .order_by(Activity.created_at)
                )
            ).scalars()
        )
        assert [row.kind for row in activity if row.kind.startswith("group.round")] == [
            "group.round",
            "group.round.end",
        ]
        assert not [event for event in events if event.status in {"pending", "claimed"}]


@pytest.mark.asyncio
async def test_group_message_to_only_a_human_does_not_start_a_round(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        room = await team.create_group_chat(you, ["chief"])
        await team.send_message(chief, room.id, "Human-only delivery")

        rounds = list(
            (
                await team.db.execute(
                    select(Activity).where(
                        Activity.room_id == room.id,
                        Activity.kind == "group.round",
                    )
                )
            ).scalars()
        )
        assert rounds == []


@pytest.mark.asyncio
async def test_thread_participant_gets_every_reply(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        researcher = await hire_researcher(team, chief)
        room = await team.create_channel(chief, "research", "Research notes", ["researcher"])
        root = await team.send_message(researcher, room.id, "Initial finding")
        await team.send_message(chief, room.id, "Can you source this?", parent_message_id=root.id)
        reply = await team.send_message(
            chief, room.id, "One more detail", parent_message_id=root.id
        )
        notification = (
            await team.db.execute(
                select(Notification).where(
                    Notification.agent_id == researcher.id,
                    Notification.message_id == reply.id,
                )
            )
        ).scalar_one()
        assert notification.kind == "thread"
        room_id = room.id
        root_id = root.id
        reply_id = reply.id

    async with open_team(world) as team:
        history = await team.history(room_id)
        persisted = {message.id: message for message in history}
        assert persisted[reply_id].parent_message_id == root_id


@pytest.mark.asyncio
async def test_dm_history_is_private_to_participants_and_owner_audit(world):
    from app.errors import TeamError

    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        researcher = await hire_researcher(team, chief)
        room = await team.get_or_create_dm(chief, researcher)
        await team.send_message(chief, room.id, "private")
        assert [message.body for message in await team.history(room.id, viewer=chief)] == [
            "private"
        ]
        assert [message.body for message in await team.history(room.id, viewer=you)] == ["private"]
        outsider = await team.hire_agent(
            chief,
            name="operator",
            role_title="Operator",
            job_description="Operates assigned systems.",
            reports_to="chief",
        )
        with pytest.raises(TeamError):
            await team.history(room.id, viewer=outsider)


@pytest.mark.asyncio
async def test_task_discussion_stays_out_of_conversation_inbox(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        room = await team.create_room(
            chief, "investigate billing", ["chief", "you"], "task", approved=True
        )
        await team.send_message(chief, room.id, "@you take a look")

        assert room.id not in {item.room.id for item in await team.list_inbox(you)}
