import asyncio
import json

import httpx
import pytest

from app.services.portal import ManagedPortal, Portal, TelegramPortal
from tests.conftest import open_team


class ListeningPortal(Portal):
    kind = "telegram"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = False
        self.closed = False

    async def listen(self, _on_text) -> None:
        self.started.set()
        await asyncio.Event().wait()

    def stop(self) -> None:
        self.stopped = True

    async def close(self) -> None:
        self.stop()
        self.closed = True


async def ignore_text(_text: str) -> None:
    return None


@pytest.mark.asyncio
async def test_agent_dm_reaches_portal_human_does_not(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        dm = await team.get_or_create_dm(you, chief)
        await team.send_message(you, dm.id, "hi")
        assert world["portal"].sent == []
        await team.send_message(chief, dm.id, "billing is down")
        assert world["portal"].sent == [
            {"sender": "chief", "body": "billing is down", "room_title": dm.title}
        ]


@pytest.mark.asyncio
async def test_group_chat_does_not_hit_portal(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        chief = await team.get_agent("chief")
        await team.hire_agent(
            chief,
            "researcher",
            "Researcher",
            "Investigates operational evidence. Specializes in careful primary-source review.",
        )
        room = await team.create_room(you, "work", ["chief", "researcher"], "task", approved=True)
        await team.send_message(chief, room.id, "working")
        assert world["portal"].sent == []


@pytest.mark.asyncio
async def test_managed_portal_replaces_one_live_listener():
    first = ListeningPortal()
    second = ListeningPortal()
    managed = ManagedPortal(first)

    await managed.start(ignore_text)
    await asyncio.wait_for(first.started.wait(), timeout=1)
    await managed.replace(second)
    await asyncio.wait_for(second.started.wait(), timeout=1)

    assert first.stopped
    assert first.closed
    assert managed.kind == "telegram"

    await managed.close()
    assert second.stopped
    assert second.closed


@pytest.mark.asyncio
async def test_telegram_long_poll_serializes_allowed_updates():
    requests: list[httpx.Request] = []
    portal = None

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        portal.stop()
        return httpx.Response(200, json={"ok": True, "result": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    portal = TelegramPortal("secret-token", client=client)

    await portal.listen(ignore_text)
    await portal.close()

    assert len(requests) == 1
    assert requests[0].url.params["allowed_updates"] == json.dumps(["message"])


@pytest.mark.asyncio
async def test_telegram_http_error_backs_off_without_leaking_token(monkeypatch):
    calls = 0
    logs: list[tuple] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="unauthorized")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    portal = TelegramPortal("secret-token", client=client)

    async def stop_after_backoff(delay: float) -> None:
        assert delay == 2
        portal.stop()

    monkeypatch.setattr("app.services.portal.asyncio.sleep", stop_after_backoff)
    monkeypatch.setattr("app.services.portal.emit", lambda *args, **_kwargs: logs.append(args))

    await portal.listen(ignore_text)
    await portal.close()

    assert calls == 1
    assert logs[-1] == ("portal", "telegram.poll", "Telegram returned HTTP 401")
    assert "secret-token" not in str(logs)


@pytest.mark.asyncio
async def test_telegram_accepts_only_the_bound_human_chat():
    received: list[str] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: None))
    portal = TelegramPortal("secret-token", chat_id="42", client=client)

    async def receive(text: str) -> None:
        received.append(text)

    await portal._take({"message": {"chat": {"id": 41}, "from": {}, "text": "wrong chat"}}, receive)
    await portal._take(
        {"message": {"chat": {"id": 42}, "from": {"is_bot": True}, "text": "bot"}},
        receive,
    )
    await portal._take(
        {"message": {"chat": {"id": 42}, "from": {}, "text": "@chief inspect"}},
        receive,
    )
    await portal.close()

    assert received == ["@chief inspect"]


@pytest.mark.asyncio
async def test_telegram_push_uses_one_office_bot_message():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    portal = TelegramPortal("secret-token", chat_id="42", client=client)

    await portal.push(sender="chief", body="deploy failed", room_title="you x chief")
    await portal.close()

    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "chat_id": "42",
        "text": "chief: deploy failed",
    }
