from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.ids import new_id


def utc_now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Keep SQLite's UTC storage format and restore awareness on every ORM read."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class Agent(Base):
    __tablename__ = "agent"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("agt"))
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # human | hermes
    hermes_profile: Mapped[str | None] = mapped_column(String(80), nullable=True)
    hermes_session_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    role_title: Mapped[str] = mapped_column(String(120), default="")
    job_description: Mapped[str] = mapped_column(Text, default="")
    reports_to_id: Mapped[str | None] = mapped_column(ForeignKey("agent.id"), nullable=True)
    capabilities_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), default="idle")  # idle | running
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    memberships: Mapped[list["Membership"]] = relationship(back_populates="agent")
    manager: Mapped["Agent | None"] = relationship(
        remote_side=[id], foreign_keys=[reports_to_id], back_populates="direct_reports"
    )
    direct_reports: Mapped[list["Agent"]] = relationship(
        foreign_keys=[reports_to_id], back_populates="manager"
    )


class Room(Base):
    __tablename__ = "room"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("rm"))
    type: Mapped[str] = mapped_column(String(16), index=True)  # dm | group | channel | task
    title: Mapped[str] = mapped_column(String(180))
    objective: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("agent.id"), nullable=True)
    lifecycle: Mapped[str] = mapped_column(String(16), default="open", index=True)
    resolved_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    owner: Mapped[Agent | None] = relationship()
    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )
    messages: Mapped[list["Message"]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )


class Membership(Base):
    __tablename__ = "membership"
    __table_args__ = (UniqueConstraint("room_id", "agent_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("mb"))
    room_id: Mapped[str] = mapped_column(ForeignKey("room.id", ondelete="CASCADE"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16), default="member")  # owner | member
    notification_level: Mapped[str] = mapped_column(String(16), default="mentions")
    # Non-message subscriptions remain available for routines and external events.
    subscriptions_json: Mapped[str] = mapped_column(Text, default="[]")

    room: Mapped[Room] = relationship(back_populates="memberships")
    agent: Mapped[Agent] = relationship(back_populates="memberships")


class Message(Base):
    __tablename__ = "message"
    __table_args__ = (Index("ix_message_room_created", "room_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("msg"))
    room_id: Mapped[str] = mapped_column(ForeignKey("room.id", ondelete="CASCADE"), index=True)
    parent_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE"), nullable=True, index=True
    )
    sender_id: Mapped[str] = mapped_column(ForeignKey("agent.id"))
    source_wake_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("wake_event.id", ondelete="SET NULL"), nullable=True, index=True
    )
    body: Mapped[str] = mapped_column(Text)
    mentions_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    room: Mapped[Room] = relationship(back_populates="messages")
    sender: Mapped[Agent] = relationship()
    reactions: Mapped[list["MessageReaction"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )
    parent: Mapped["Message | None"] = relationship(
        remote_side=[id], foreign_keys=[parent_message_id], back_populates="replies"
    )
    replies: Mapped[list["Message"]] = relationship(
        foreign_keys=[parent_message_id], back_populates="parent", cascade="all, delete-orphan"
    )


class Notification(Base):
    __tablename__ = "notification"
    __table_args__ = (
        UniqueConstraint("agent_id", "message_id"),
        Index("ix_notification_queue", "agent_id", "read_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ntf"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id", ondelete="CASCADE"))
    room_id: Mapped[str] = mapped_column(ForeignKey("room.id", ondelete="CASCADE"))
    message_id: Mapped[str] = mapped_column(ForeignKey("message.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # dm | group | mention | channel | thread
    wake_event_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    agent: Mapped[Agent] = relationship()
    room: Mapped[Room] = relationship()
    message: Mapped[Message] = relationship()


class MessageReaction(Base):
    __tablename__ = "message_reaction"
    __table_args__ = (UniqueConstraint("message_id", "agent_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("react"))
    message_id: Mapped[str] = mapped_column(ForeignKey("message.id", ondelete="CASCADE"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id", ondelete="CASCADE"))
    value: Mapped[str] = mapped_column(String(8))  # up | down
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    message: Mapped[Message] = relationship(back_populates="reactions")
    agent: Mapped[Agent] = relationship()


class PortalDelivery(Base):
    __tablename__ = "portal_delivery"
    __table_args__ = (Index("ix_portal_delivery_due", "status", "portal_key", "next_attempt_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("pd"))
    message_id: Mapped[str] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE"), unique=True
    )
    portal_key: Mapped[str] = mapped_column(String(80))
    recipient: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | sent | failed
    next_part: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    last_error: Mapped[str] = mapped_column(String(240), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    message: Mapped[Message] = relationship()


class PortalReceipt(Base):
    __tablename__ = "portal_receipt"

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    peer: Mapped[str] = mapped_column(String(80))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class RoomRead(Base):
    __tablename__ = "room_read"

    room_id: Mapped[str] = mapped_column(
        ForeignKey("room.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agent.id", ondelete="CASCADE"), primary_key=True
    )
    last_read_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("message.id", ondelete="SET NULL"), nullable=True
    )


class WakeEvent(Base):
    __tablename__ = "wake_event"
    __table_args__ = (
        Index("ix_wake_due", "status", "due_at"),
        Index(
            "ix_wake_dedupe_open",
            "dedupe_key",
            unique=True,
            sqlite_where=text("status IN ('pending', 'claimed') AND dedupe_key IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ev"))
    target_id: Mapped[str] = mapped_column(ForeignKey("agent.id"), index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("agent.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(32), index=True)
    context_reference: Mapped[str] = mapped_column(String(80), default="")
    due_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    state_hash: Mapped[str] = mapped_column(String(64), default="")
    execution_lane: Mapped[str] = mapped_column(String(16), default="shared")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, index=True
    )
    claimed_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    target: Mapped[Agent] = relationship(foreign_keys=[target_id])


class Routine(Base):
    __tablename__ = "routine"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("rtn"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id"), index=True)
    reason: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(80), default="")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=0)
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    match_any_json: Mapped[str] = mapped_column(Text, default="[]")
    ignore_any_json: Mapped[str] = mapped_column(Text, default="[]")
    trigger_type: Mapped[str] = mapped_column(String(16), default="event")
    max_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    agent: Mapped[Agent] = relationship()


class RoutineRun(Base):
    __tablename__ = "routine_run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("rrun"))
    routine_id: Mapped[str] = mapped_column(
        ForeignKey("routine.id", ondelete="CASCADE"), index=True
    )
    wake_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("wake_event.id", ondelete="SET NULL"), nullable=True
    )
    trigger: Mapped[str] = mapped_column(String(32))
    verdict: Mapped[str] = mapped_column(String(16), default="yes")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    routine: Mapped[Routine] = relationship()


class Artifact(Base):
    __tablename__ = "artifact"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("art"))
    message_id: Mapped[str] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # workspace | url
    reference: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(180), default="")
    mime_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    message: Mapped[Message] = relationship(back_populates="artifacts")


class HumanRequest(Base):
    __tablename__ = "human_request"
    __table_args__ = (Index("ix_human_request_queue", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("hrq"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id"), index=True)
    room_id: Mapped[str | None] = mapped_column(ForeignKey("room.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # choice | approval | handoff | secret
    prompt: Mapped[str] = mapped_column(Text)
    options_json: Mapped[str] = mapped_column(Text, default="[]")
    secret_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    response_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    agent: Mapped[Agent] = relationship()
    room: Mapped[Room | None] = relationship()


class Demonstration(Base):
    __tablename__ = "demonstration"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("demo"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(24), default="recording")
    recording_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    learned_reference: Mapped[str] = mapped_column(Text, default="")
    verification_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    agent: Mapped[Agent] = relationship()


class OpenLoop(Base):
    __tablename__ = "open_loop"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("loop"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id"), index=True)
    reason: Mapped[str] = mapped_column(String(160))
    target_id: Mapped[str] = mapped_column(ForeignKey("agent.id"))
    due_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    agent: Mapped[Agent] = relationship(foreign_keys=[agent_id])


class KanbanRef(Base):
    __tablename__ = "kanban_ref"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("kref"))
    room_id: Mapped[str] = mapped_column(ForeignKey("room.id"), index=True)
    hermes_task_id: Mapped[str] = mapped_column(String(80), unique=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("agent.id"))
    title: Mapped[str] = mapped_column(String(180))
    last_status: Mapped[str] = mapped_column(String(32), default="todo")
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    room: Mapped[Room] = relationship()
    owner: Mapped[Agent] = relationship()


class Run(Base):
    __tablename__ = "run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("run"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id"), index=True)
    wake_event_id: Mapped[str] = mapped_column(ForeignKey("wake_event.id"))
    status: Mapped[str] = mapped_column(String(16), default="running")
    decision: Mapped[str] = mapped_column(String(32), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    turns_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handoffs_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider: Mapped[str] = mapped_column(String(80), default="")
    model: Mapped[str] = mapped_column(String(160), default="")
    max_turns: Mapped[int] = mapped_column(Integer, default=24)
    max_handoffs: Mapped[int] = mapped_column(Integer, default=6)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    agent: Mapped[Agent] = relationship()


class Activity(Base):
    __tablename__ = "activity"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("act"))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("agent.id"), nullable=True)
    room_id: Mapped[str | None] = mapped_column(ForeignKey("room.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(180))
    detail: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
