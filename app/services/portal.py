from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.config import Settings
from app.logger import emit
from app.services.auth import upsert_env

TELEGRAM_API = "https://api.telegram.org"
OnText = Callable[[str], Awaitable[None]]
_NAME = re.compile(r"^@?([A-Za-z0-9_-]+)[:\s]+(.*)$", re.S)


class Portal:
    """One pipe to the human. Rooms stay the record."""

    kind = "none"

    async def push(self, *, sender: str, body: str, room_title: str) -> None:
        return None

    async def listen(self, on_text: OnText) -> None:
        return None

    def stop(self) -> None:
        return None

    async def close(self) -> None:
        self.stop()


class NullPortal(Portal):
    kind = "none"


@dataclass
class MemoryPortal(Portal):
    kind = "memory"
    sent: list[dict] = field(default_factory=list)
    inbox: list[tuple[str, str]] = field(default_factory=list)

    async def push(self, *, sender: str, body: str, room_title: str) -> None:
        self.sent.append({"sender": sender, "body": body, "room_title": room_title})


class ManagedPortal(Portal):
    """Keep one portal reference while safely replacing its active adapter."""

    def __init__(self, portal: Portal) -> None:
        self._portal = portal
        self._on_text: OnText | None = None
        self._listener: asyncio.Task[None] | None = None

    @property
    def kind(self) -> str:
        return self._portal.kind

    @property
    def last_peer(self) -> str:
        return getattr(self._portal, "last_peer", "chief")

    @last_peer.setter
    def last_peer(self, peer: str) -> None:
        if hasattr(self._portal, "last_peer"):
            self._portal.last_peer = peer

    async def push(self, *, sender: str, body: str, room_title: str) -> None:
        await self._portal.push(sender=sender, body=body, room_title=room_title)

    async def start(self, on_text: OnText) -> None:
        self._on_text = on_text
        self._start_listener()

    async def replace(self, portal: Portal) -> None:
        await self._stop_listener()
        await self._portal.close()
        self._portal = portal
        self._start_listener()

    async def close(self) -> None:
        await self._stop_listener()
        await self._portal.close()

    def _start_listener(self) -> None:
        if self._on_text is None or self._portal.kind == "none":
            return
        self._listener = asyncio.create_task(self._portal.listen(self._on_text))

    async def _stop_listener(self) -> None:
        self._portal.stop()
        if self._listener is None:
            return
        self._listener.cancel()
        with suppress(asyncio.CancelledError):
            await self._listener
        self._listener = None


class TelegramPortal(Portal):
    kind = "telegram"

    def __init__(
        self,
        token: str,
        chat_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token = token
        self.chat_id = chat_id or ""
        self.last_peer = "chief"
        self._stop = asyncio.Event()
        self._client = client or httpx.AsyncClient(timeout=40.0)

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        self.stop()
        await self._client.aclose()

    async def push(self, *, sender: str, body: str, room_title: str) -> None:
        if not self.chat_id:
            emit("portal", "telegram.skip", "no chat_id")
            return
        self.last_peer = sender
        await self._post(
            "sendMessage",
            {"chat_id": self.chat_id, "text": f"{sender}: {body}"},
        )

    async def listen(self, on_text: OnText) -> None:
        offset = 0
        emit("portal", "telegram.listen", "polling")
        while not self._stop.is_set():
            try:
                data = await self._get(
                    "getUpdates",
                    {
                        "timeout": 25,
                        "offset": offset,
                        "allowed_updates": json.dumps(["message"]),
                    },
                )
            except (httpx.HTTPError, ValueError) as exc:
                emit("portal", "telegram.poll", _error_detail(exc))
                await asyncio.sleep(2)
                continue
            for update in data.get("result") or []:
                offset = int(update.get("update_id", 0)) + 1
                await self._take(update, on_text)

    async def _take(self, update: dict, on_text: OnText) -> None:
        message = update.get("message") or {}
        sender = message.get("from") or {}
        if sender.get("is_bot"):
            return
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            return
        if not self.chat_id:
            self.chat_id = chat_id
            upsert_env(Path(".env"), "MOATBOTS_TELEGRAM_CHAT_ID", chat_id)
            await self._post(
                "sendMessage",
                {"chat_id": chat_id, "text": "portal bound. text @chief …"},
            )
            emit("portal", "telegram.bound", chat_id)
            return
        if chat_id != str(self.chat_id):
            return
        if text in {"/start", "/start@moatbots"}:
            await self._post(
                "sendMessage",
                {"chat_id": chat_id, "text": "office is up. @name then the message."},
            )
            return
        await on_text(text)

    async def _get(self, method: str, params: dict) -> dict:
        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def _post(self, method: str, payload: dict) -> None:
        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        try:
            response = await self._client.post(url, json=payload, timeout=12.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            emit("portal", "telegram.fail", _error_detail(exc))


def _error_detail(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Telegram returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return f"Telegram request failed: {type(exc).__name__}"
    return f"Telegram returned invalid JSON: {type(exc).__name__}"


def parse_inbound(text: str, names: list[str], default: str) -> tuple[str, str]:
    """Pick which teammate a portal message is for. @chief hello → (chief, hello)."""
    raw = text.strip()
    known = {name.lower(): name for name in names}
    match = _NAME.match(raw)
    if match:
        hint, rest = match.group(1), match.group(2).strip()
        if hint.lower() in known and rest:
            return known[hint.lower()], rest
    return default, raw


def build_portal(settings: Settings) -> Portal:
    if settings.portal.kind != "telegram":
        return NullPortal()
    token = settings.telegram_bot_token
    if not token:
        emit("portal", "telegram.unconfigured", "token missing")
        return NullPortal()
    return TelegramPortal(token, settings.telegram_chat_id)
