from datetime import timedelta

import pytest

from app.errors import NotFound, TeamError
from app.models import Message
from tests.conftest import open_team


async def test_focus_loads_old_reply_and_parent_without_loading_entire_history(world):
    async with open_team(world) as team:
        you, chief = await team.get_agent("you"), await team.get_agent("chief")
        dm = await team.get_or_create_dm(you, chief)
        root = await team.send_message(chief, dm.id, "Older decision")
        reply = await team.send_message(chief, dm.id, "Older result", parent_message_id=root.id)
        for i in range(90):
            team.db.add(
                Message(
                    id=f"recent_{i:03d}",
                    room_id=dm.id,
                    sender_id=chief.id,
                    body="Recent message",
                    created_at=world["clock"].now() + timedelta(seconds=i + 1),
                )
            )
        await team.db.flush()
        normal = await team.history(dm.id, viewer=you, include_thread_roots=True)
        assert len(normal) == 80 and reply.id not in {row.id for row in normal}
        focused = await team.history(
            dm.id, viewer=you, include_thread_roots=True, focus_message_id=reply.id
        )
        assert len(focused) == 82
        assert {root.id, reply.id} <= {row.id for row in focused}
        with pytest.raises(NotFound):
            await team.history(dm.id, viewer=you, focus_message_id="missing")


async def test_files_and_message_focus_preserve_room_visibility(world):
    async with open_team(world) as team:
        you, chief = await team.get_agent("you"), await team.get_agent("chief")
        peer = await team.hire_agent(
            chief, "reviewer", "Reviewer", "Checks delivered work against its source evidence."
        )
        public_dm = await team.get_or_create_dm(you, chief)
        private_dm = await team.get_or_create_dm(chief, peer)
        public = await team.send_message(chief, public_dm.id, "Shared result")
        private = await team.send_message(chief, private_dm.id, "Private result")
        visible = await team.attach_artifact(
            chief, public.id, reference="https://example.test/report.pdf", label="100% report"
        )
        await team.attach_artifact(
            chief, private.id, reference="https://example.test/private.pdf", label="Private report"
        )
        assert [row.id for row in await team.list_artifacts(you)] == [visible.id]
        assert [row.id for row in await team.list_artifacts(you, "%")] == [visible.id]
        assert await team.list_artifacts(you, "private") == []
        assert await team.list_artifacts(you, room_id=private_dm.id) == []
        assert len(await team.list_artifacts(chief, "REPORT")) == 2
        with pytest.raises(NotFound):
            await team.history(public_dm.id, viewer=you, focus_message_id=private.id)
        with pytest.raises(TeamError):
            await team.history(private_dm.id, viewer=peer, focus_message_id=public.id)
