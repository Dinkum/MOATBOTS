from datetime import timedelta

import pytest
from sqlalchemy import event

from app.models import Membership, Message, Notification, Room
from app.routes.pages import templates
from tests.conftest import open_team


async def test_inbox_uses_bounded_queries_and_keeps_visibility_and_unread_rules(world):
    async with open_team(world) as team:
        you, chief = await team.get_agent("you"), await team.get_agent("chief")
        viewer_id, chief_id = you.id, chief.id
        for n in range(30):
            room = Room(id=f"audit_room_{n}", title=f"Room {n}", type="channel")
            team.db.add(room)
            await team.db.flush()
            team.db.add_all(
                [
                    Membership(room_id=room.id, agent_id=you.id),
                    Membership(room_id=room.id, agent_id=chief.id),
                ]
            )
            for j in range(80):
                team.db.add(
                    Message(
                        id=f"message_{n}_{j:02d}",
                        room_id=room.id,
                        sender_id=chief.id,
                        body=str(j),
                        created_at=world["clock"].now() + timedelta(seconds=n * 100 + j),
                    )
                )
        for name, kind, lifecycle in (
            ("empty", "dm", "open"),
            ("archived", "channel", "archived"),
            ("task", "task", "open"),
            ("private", "dm", "open"),
        ):
            room = Room(id=name, title=name, type=kind, lifecycle=lifecycle)
            team.db.add(room)
            await team.db.flush()
            if name != "private":
                team.db.add(Membership(room_id=name, agent_id=you.id))
        await team.db.flush()
        for i in (1, 2):
            team.db.add(
                Notification(
                    agent_id=you.id,
                    room_id="audit_room_0",
                    message_id=f"message_0_{i:02d}",
                    kind="channel",
                )
            )
        team.db.add(
            Notification(
                agent_id=chief.id, room_id="audit_room_1", message_id="message_1_00", kind="channel"
            )
        )
    engine = world["factory"].kw["bind"]
    queries = []

    def record(connection, cursor, statement, parameters, context, many):
        queries.append(statement)

    async with open_team(world) as team:
        viewer = await team.get_agent(viewer_id)
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            items = await team.list_inbox(viewer)
            assert len(queries) <= 8
            assert (
                len([row for row in team.db.identity_map.values() if isinstance(row, Message)])
                == 30
            )
            assert not any(
                "message_reaction" in statement or "FROM artifact" in statement
                for statement in queries
            )
            queries.clear()
            assert await team.inbox_unread_count(viewer) == 1
            assert len(queries) == 1
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        populated = [item for item in items if item.last_message]
        assert [item.room.id for item in populated] == [
            f"audit_room_{n}" for n in reversed(range(30))
        ]
        assert all(item.last_message.body == "79" for item in populated)
        assert sum(item.unread for item in items) == 1
        assert "empty" in {item.room.id for item in items}
        assert not {"archived", "task", "private"} & {item.room.id for item in items}
        assert (await team.get_agent(chief_id)).kind == "hermes"


@pytest.mark.parametrize("reply_count", [1, 5, 80])
async def test_ui_history_restores_excluded_roots_and_renders_replies(world, reply_count):
    async with open_team(world) as team:
        you, chief = await team.get_agent("you"), await team.get_agent("chief")
        dm = await team.get_or_create_dm(you, chief)
        root = Message(
            id="root",
            room_id=dm.id,
            sender_id=chief.id,
            body="Old root",
            created_at=world["clock"].now() - timedelta(days=1),
        )
        team.db.add(root)
        await team.db.flush()
        for i in range(80):
            team.db.add(
                Message(
                    id=f"new_{i:02d}",
                    room_id=dm.id,
                    sender_id=chief.id,
                    parent_message_id=root.id if i < reply_count else None,
                    body=f"REPLY_MARKER_{i}" if i < reply_count else "Independent",
                    created_at=world["clock"].now() + timedelta(seconds=i),
                )
            )
        room_id, viewer_id = dm.id, you.id
    async with open_team(world) as team:
        viewer = await team.get_agent(viewer_id)
        ordinary = await team.history(room_id, viewer=viewer)
        assert len(ordinary) == 80 and all(row.id != "root" for row in ordinary)
        rows = await team.history(room_id, viewer=viewer, include_thread_roots=True)
        assert len(rows) == 81 and rows[0].id == "root"
        roots = [row for row in rows if row.parent_message_id is None]
        html = templates.env.get_template("chat_room.html").render(
            room=await team.get_room(room_id),
            room_label="Chief",
            messages=roots,
            replies_by_root={
                row.id: [reply for reply in rows if reply.parent_message_id == row.id]
                for row in roots
            },
            viewer_id=viewer_id,
            following=False,
            can_manage_channel=False,
            tasks=[],
        )
        assert html.count("REPLY_MARKER_") == reply_count


async def test_preview_matches_history_when_timestamps_tie(world):
    async with open_team(world) as team:
        you, chief = await team.get_agent("you"), await team.get_agent("chief")
        dm = await team.get_or_create_dm(you, chief)
        team.db.add_all(
            [
                Message(
                    id=name,
                    room_id=dm.id,
                    sender_id=chief.id,
                    body=name,
                    created_at=world["clock"].now(),
                )
                for name in ("z", "a")
            ]
        )
        await team.db.flush()
        item = next(item for item in await team.list_inbox(you) if item.room.id == dm.id)
        assert item.last_message.id == "z"
        assert [row.id for row in await team.history(dm.id)] == ["a", "z"]
