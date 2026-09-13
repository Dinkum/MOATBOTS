from __future__ import annotations

import asyncio
import hashlib
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
OnText = Callable[[str, str], Awaitable[None]]
_NAME = re.compile(r"^@?([A-Za-z0-9_-]+)[:\s]+(.*)$", re.S)


class PortalDeliveryError(Exception):
    def __init__(self, detail: str, *, retryable: bool = True, retry_after: int = 0) -> None:
        super().__init__(detail)
        self.retryable = retryable
        self.retry_after = retry_after


class Portal:
    """One pipe to the human. Rooms stay the record."""

    kind = "none"

    @property
    def adapter(self) -> Portal:
        return self

    @property
    def delivery_key(self) -> str:
        return self.kind

    @property
    def recipient(self) -> str:
        return self.kind if self.kind != "none" else ""

    def parts(self, sender: str, body: str) -> list[str]:
        return [body]

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
    def adapter(self) -> Portal:
        return self._portal

    @property
    def delivery_key(self) -> str:
        return self._portal.delivery_key

    @property
    def recipient(self) -> str:
        return self._portal.recipient

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
            try:
                await self._listener
            except Exception as exc:
                emit("portal", "listener.error", f"Listener stopped: {type(exc).__name__}")
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
        self.last_error = ""
        self._stop = asyncio.Event()
        self._client = client or httpx.AsyncClient(timeout=40.0)

    @property
    def delivery_key(self) -> str:
        # Bind queued work to its configured bot without persisting the credential.
        return "telegram:" + hashlib.sha256(self.token.encode()).hexdigest()

    @property
    def recipient(self) -> str:
        return str(self.chat_id)

    def parts(self, sender: str, body: str) -> list[str]:
        # Telegram counts UTF-16 units. Keep every chunk, including its sender, in bounds.
        available = 4096 - len(f"{sender}: ".encode("utf-16-le")) // 2
        chunks: list[str] = []
        current: list[str] = []
        used = 0
        for character in body:
            width = 2 if ord(character) > 0xFFFF else 1
            if used + width > available:
                chunks.append("".join(current))
                current, used = [], 0
            current.append(character)
            used += width
        if current:
            chunks.append("".join(current))
        return chunks

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        self.stop()
        await self._client.aclose()

    async def push(self, *, sender: str, body: str, room_title: str) -> None:
        if not self.chat_id:
            raise PortalDeliveryError("Telegram chat is not bound")
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
                self.last_error = _error_detail(exc)
                emit("portal", "telegram.poll", self.last_error)
                await self._pause(2)
                continue
            self.last_error = ""
            for update in data.get("result") or []:
                failures = 0
                while not self._stop.is_set():
                    try:
                        await self._take(update, on_text)
                    except Exception as exc:
                        # Keep the failed update unacknowledged. The callback's durable
                        # receipt makes a retry safe even after an uncertain commit.
                        failures += 1
                        self.last_error = f"Telegram ingestion failed: {type(exc).__name__}"
                        emit("portal", "telegram.ingest", self.last_error)
                        await self._pause(min(30, 2 ** min(failures, 5)))
                    else:
                        offset = int(update["update_id"]) + 1
                        self.last_error = ""
                        break

    async def _pause(self, seconds: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

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
        await on_text(text, f"{self.delivery_key}:{update['update_id']}")

    async def _get(self, method: str, params: dict) -> dict:
        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ValueError("Invalid Telegram response")
        updates = data.get("result")
        if not isinstance(updates, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("update_id"), int)
            for row in updates
        ):
            raise ValueError("Invalid Telegram updates")
        return data

    async def _post(self, method: str, payload: dict) -> None:
        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        try:
            response = await self._client.post(url, json=payload, timeout=12.0)
        except httpx.HTTPError as exc:
            raise PortalDeliveryError(_error_detail(exc)) from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.is_success and isinstance(data, dict) and data.get("ok") is True:
            return
        code = data.get("error_code", response.status_code) if isinstance(data, dict) else 502
        if not isinstance(code, int):
            code = response.status_code
        retry_after = 0
        if isinstance(data, dict):
            parameters = data.get("parameters") or {}
            if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), int):
                retry_after = max(0, parameters["retry_after"])
        raise PortalDeliveryError(
            f"Telegram rejected delivery (status {code})",
            retryable=code == 429 or code >= 500 or response.is_success,
            retry_after=retry_after,
        )


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
