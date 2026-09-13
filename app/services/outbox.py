from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import selectinload

from app.clock import Clock
from app.logger import emit
from app.models import Message, PortalDelivery
from app.services.portal import Portal, PortalDeliveryError


class PortalOutbox:
    """Deliver committed messages, keeping network waits outside SQLite transactions."""

    def __init__(self, session_factory, clock: Clock, portal: Portal) -> None:
        self.session_factory = session_factory
        self.clock = clock
        self.portal = portal
        self.last_error = ""
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.drain()
                self.last_error = ""
            except Exception as exc:
                # A transient DB failure must not leave a dead delivery task behind.
                self.last_error = f"Outbox failed: {type(exc).__name__}"
                emit("portal", "outbox.error", self.last_error)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=1)

    async def drain(self, limit: int = 20) -> int:
        handled = 0
        for _ in range(limit):
            if self._stopped.is_set() or not await self._deliver_one():
                break
            handled += 1
        return handled

    async def _deliver_one(self) -> bool:
        # Capture the adapter, so replacing settings cannot redirect an in-flight send.
        adapter = self.portal.adapter
        if adapter.kind == "none" or not adapter.recipient:
            return False
        now = self.clock.now()
        async with self.session_factory() as session:
            eligible = (
                select(PortalDelivery.id)
                .where(
                    PortalDelivery.status == "pending",
                    PortalDelivery.portal_key == adapter.delivery_key,
                    or_(
                        PortalDelivery.recipient.is_(None),
                        PortalDelivery.recipient == adapter.recipient,
                    ),
                    PortalDelivery.next_attempt_at <= now,
                )
                .order_by(PortalDelivery.created_at, PortalDelivery.id)
                .limit(1)
                .scalar_subquery()
            )
            delivery_id = await session.scalar(
                update(PortalDelivery)
                .where(PortalDelivery.id == eligible)
                .values(
                    recipient=adapter.recipient,
                    attempts=PortalDelivery.attempts + 1,
                    # A crash leaves the attempt retryable. This exceeds the 12s send timeout.
                    next_attempt_at=now + timedelta(seconds=60),
                )
                .returning(PortalDelivery.id)
            )
            if delivery_id is None:
                return False
            delivery = (
                await session.execute(
                    select(PortalDelivery)
                    .options(
                        selectinload(PortalDelivery.message).selectinload(Message.sender),
                        selectinload(PortalDelivery.message).selectinload(Message.room),
                    )
                    .where(PortalDelivery.id == delivery_id)
                )
            ).scalar_one()
            sender = delivery.message.sender.name
            title = delivery.message.room.title
            parts = adapter.parts(sender, delivery.message.body)
            part, attempts = delivery.next_part, delivery.attempts
            await session.commit()

        values: dict = {}
        try:
            await adapter.push(sender=sender, body=parts[part], room_title=title)
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, PortalDeliveryError)
                else PortalDeliveryError(f"Portal send failed: {type(exc).__name__}")
            )
            delay = max(error.retry_after, min(300, 2 ** min(attempts, 9)))
            values = {
                "last_error": str(error)[:240],
                "status": "pending" if error.retryable else "failed",
                "next_attempt_at": self.clock.now() + timedelta(seconds=delay),
            }
            emit("portal", "delivery.error", str(error), delivery_id=delivery_id)
        else:
            complete = part + 1 == len(parts)
            values = {
                "next_part": part + 1,
                "last_error": "",
                "next_attempt_at": self.clock.now(),
                "status": "sent" if complete else "pending",
                "sent_at": self.clock.now() if complete else None,
            }
        # Cancellation leaves the persisted attempt lease intact; never mark an
        # uncertain send successful. A retry can duplicate the last unacknowledged part.
        async with self.session_factory() as session:
            await session.execute(
                update(PortalDelivery).where(PortalDelivery.id == delivery_id).values(**values)
            )
            await session.commit()
        self.last_error = ""
        return True
