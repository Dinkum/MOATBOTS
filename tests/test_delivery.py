import asyncio
import json
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Activity, Message, PortalDelivery, PortalReceipt, WakeEvent
from app.services.inbox import accept_human_text
from app.services.outbox import PortalOutbox
from app.services.portal import MemoryPortal, PortalDeliveryError, TelegramPortal
from tests.conftest import open_team


async def queue_message(world, body="queued message"):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        dm = await team.get_or_create_dm(await team.get_agent("you"), chief)
        return await team.send_message(chief, dm.id, body)


async def delivery_row(world):
    async with world["factory"]() as session:
        return (await session.execute(select(PortalDelivery))).scalar_one()


async def test_rollback_never_reaches_the_portal(world):
    with pytest.raises(RuntimeError, match="rollback"):
        async with open_team(world) as team:
            chief = await team.get_agent("chief")
            dm = await team.get_or_create_dm(await team.get_agent("you"), chief)
            await team.send_message(chief, dm.id, "must not escape")
            raise RuntimeError("rollback")
    assert await PortalOutbox(world["factory"], world["clock"], world["portal"]).drain() == 0
    assert world["portal"].sent == []
    async with world["factory"]() as session:
        assert await session.scalar(select(func.count()).select_from(PortalDelivery)) == 0


async def test_network_wait_does_not_block_an_unrelated_writer(world):
    class SlowPortal(MemoryPortal):
        async def push(self, **kwargs):
            started.set()
            await finish.wait()
            await super().push(**kwargs)

    started, finish = asyncio.Event(), asyncio.Event()
    world["portal"] = SlowPortal()
    await queue_message(world)
    worker = PortalOutbox(world["factory"], world["clock"], world["portal"])
    task = asyncio.create_task(worker.drain())
    try:
        await asyncio.wait_for(started.wait(), 1)
        async with world["factory"]() as session:
            session.add(Activity(kind="test", title="Independent write during network wait"))
            await asyncio.wait_for(session.commit(), 1)
    finally:
        finish.set()
        await task
    assert (await delivery_row(world)).status == "sent"


async def test_failed_delivery_survives_worker_restart(world):
    class FailingPortal(MemoryPortal):
        async def push(self, **kwargs):
            raise PortalDeliveryError("temporary outage", retry_after=10)

    world["portal"] = FailingPortal()
    await queue_message(world)
    worker = PortalOutbox(world["factory"], world["clock"], world["portal"])
    assert await worker.drain() == 1
    row = await delivery_row(world)
    assert row.status == "pending" and row.attempts == 1
    assert row.next_attempt_at == world["clock"].now().replace(second=10)
    recovered = MemoryPortal()
    worker = PortalOutbox(world["factory"], world["clock"], recovered)
    assert await worker.drain() == 0
    world["clock"].advance(seconds=10)
    assert await worker.drain() == 1
    row = await delivery_row(world)
    assert row.status == "sent" and row.attempts == 2 and not row.last_error
    assert len(recovered.sent) == 1


async def test_cancelled_send_remains_retryable_after_its_lease(world):
    class InterruptedPortal(MemoryPortal):
        async def push(self, **kwargs):
            started.set()
            await asyncio.Event().wait()

    started = asyncio.Event()
    world["portal"] = InterruptedPortal()
    await queue_message(world)
    worker = PortalOutbox(world["factory"], world["clock"], world["portal"])
    task = asyncio.create_task(worker.drain())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    row = await delivery_row(world)
    assert row.status == "pending" and row.next_part == 0 and row.sent_at is None
    recovered = MemoryPortal()
    worker = PortalOutbox(world["factory"], world["clock"], recovered)
    assert await worker.drain() == 0
    world["clock"].advance(seconds=61)
    assert await worker.drain() == 1
    assert len(recovered.sent) == 1


async def test_queued_message_cannot_switch_recipient_or_bot(world):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    original = TelegramPortal(
        "bot-one", "42", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    world["portal"] = original
    await queue_message(world)
    for token, recipient in (("bot-one", "43"), ("bot-two", "42")):
        portal = TelegramPortal(
            token, recipient, httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        assert await PortalOutbox(world["factory"], world["clock"], portal).drain() == 0
        await portal.close()
    assert calls == []
    assert await PortalOutbox(world["factory"], world["clock"], original).drain() == 1
    await original.close()
    assert len(calls) == 1


async def test_unbound_queue_waits_and_long_messages_resume_at_failed_part(world):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content)["text"])
        return httpx.Response(503 if len(requests) == 2 else 200, json={"ok": len(requests) != 2})

    portal = TelegramPortal(
        "test-bot", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    world["portal"] = portal
    body = "🙂" * 5000
    await queue_message(world, body)
    worker = PortalOutbox(world["factory"], world["clock"], portal)
    assert await worker.drain() == 0
    portal.chat_id = "42"
    assert await worker.drain() == 2
    row = await delivery_row(world)
    assert row.next_part == 1 and row.status == "pending"
    world["clock"].advance(seconds=5)
    assert await PortalOutbox(world["factory"], world["clock"], portal).drain() == 2
    assert (await delivery_row(world)).status == "sent"
    assert requests[1] == requests[2]
    assert all(len(part.encode("utf-16-le")) // 2 <= 4096 for part in requests)
    delivered = [requests[0], *requests[2:]]
    assert "".join(part.removeprefix("chief: ") for part in delivered) == body
    await portal.close()


async def test_permanent_telegram_rejection_is_retained_and_sanitized(world):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                403, json={"ok": False, "error_code": 403, "description": "sensitive response"}
            )
        )
    )
    portal = TelegramPortal("private-token", "42", client)
    world["portal"] = portal
    await queue_message(world)
    assert await PortalOutbox(world["factory"], world["clock"], portal).drain() == 1
    row = await delivery_row(world)
    assert row.status == "failed" and "403" in row.last_error
    assert "private-token" not in row.last_error and "sensitive" not in row.last_error
    await portal.close()


async def test_inbound_receipt_and_message_commit_atomically(world):
    with pytest.raises(RuntimeError):
        async with open_team(world) as team:
            await accept_human_text(
                team, "@chief retry once", "chief", delivery_id="telegram:test:1"
            )
            raise RuntimeError("rollback callback")
    for _ in range(2):
        async with open_team(world) as team:
            assert (
                await accept_human_text(
                    team, "@chief retry once", "chief", delivery_id="telegram:test:1"
                )
                == "chief"
            )
    async with world["factory"]() as session:
        assert await session.scalar(select(func.count()).select_from(PortalReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(Message)) == 1
        assert await session.scalar(select(func.count()).select_from(WakeEvent)) == 1


async def test_listener_retries_ingestion_before_confirming_update_offset(monkeypatch):
    offsets, accepted, attempts = [], [], []
    updates = [
        {"update_id": n, "message": {"chat": {"id": 42}, "text": str(n), "from": {}}}
        for n in (1, 2)
    ]

    async def handler(request):
        offsets.append(int(request.url.params["offset"]))
        if len(offsets) > 1:
            portal.stop()
        return httpx.Response(
            200, json={"ok": True, "result": updates if len(offsets) == 1 else []}
        )

    portal = TelegramPortal(
        "private-token", "42", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    async def callback(body, delivery_id):
        attempts.append(body)
        if len(attempts) == 1:
            raise OSError("temporary database failure")
        accepted.append((body, delivery_id))

    async def no_delay(seconds):
        assert seconds == 2

    monkeypatch.setattr(portal, "_pause", no_delay)
    await asyncio.wait_for(portal.listen(callback), 1)
    await portal.close()
    assert offsets == [0, 3]
    assert attempts == ["1", "1", "2"]
    assert [row[0] for row in accepted] == ["1", "2"]
    assert not portal.last_error


async def test_listener_preserves_cancellation_and_surfaces_stalled_ingestion(monkeypatch):
    update = {"update_id": 1, "message": {"chat": {"id": 42}, "text": "one", "from": {}}}
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": [update]})
        )
    )
    portal = TelegramPortal("private-token", "42", client)
    delays = []

    async def failing_callback(body, delivery_id):
        raise ValueError("private body")

    async def pause(seconds):
        delays.append(seconds)
        if len(delays) == 7:
            portal.stop()

    monkeypatch.setattr(portal, "_pause", pause)
    await asyncio.wait_for(portal.listen(failing_callback), 1)
    assert max(delays) == 30
    assert portal.last_error == "Telegram ingestion failed: ValueError"
    portal._stop.clear()

    async def cancelled_callback(body, delivery_id):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await portal.listen(cancelled_callback)
    await portal.close()
