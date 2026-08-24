from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clock import Clock
from app.config import BudgetSettings
from app.errors import BudgetExceeded, NotFound, SelfWakeRejected, TeamError
from app.models import (
    Activity,
    Agent,
    KanbanRef,
    Membership,
    Message,
    MessageReaction,
    Notification,
    OpenLoop,
    Room,
    RoomRead,
    Routine,
    Run,
    WakeEvent,
)
from app.services.filter import decide, mention_names, parse_list
from app.services.kanban import KanbanBoard
from app.services.portal import NullPortal, Portal
from app.services.profiles import ProfileProvisioner, UnavailableProfileProvisioner

_HANDLE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_CHANNEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_CAPABILITY_STOP = {
    "and",
    "for",
    "from",
    "into",
    "that",
    "the",
    "their",
    "this",
    "with",
}


@dataclass(frozen=True)
class InboxItem:
    room: Room
    last_message: Message | None
    unread: bool


class NewEventSignal:
    def __init__(self) -> None:
        self.event = asyncio.Event()

    def notify(self) -> None:
        self.event.set()

    async def wait(self, timeout: float | None) -> None:
        # Do not clear first. A notify that arrived during drain must still wake us.
        try:
            await asyncio.wait_for(self.event.wait(), timeout=timeout)
        except TimeoutError:
            return
        self.event.clear()


class TeamService:
    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        kanban: KanbanBoard,
        signal: NewEventSignal,
        budgets: BudgetSettings | None = None,
        portal: Portal | None = None,
        profiles: ProfileProvisioner | None = None,
    ) -> None:
        self.db = session
        self.clock = clock
        self.kanban = kanban
        self.signal = signal
        self.budgets = budgets or BudgetSettings()
        self.portal = portal or NullPortal()
        self.profiles = profiles or UnavailableProfileProvisioner()

    async def record(
        self,
        kind: str,
        title: str,
        detail: str = "",
        actor_id: str | None = None,
        room_id: str | None = None,
        **payload: Any,
    ) -> Activity:
        row = Activity(
            kind=kind,
            title=title,
            detail=detail,
            actor_id=actor_id,
            room_id=room_id,
            payload_json=json.dumps(payload, default=str),
            created_at=self.clock.now(),
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def get_agent(self, id_or_name: str) -> Agent:
        row = await self.db.get(Agent, id_or_name)
        if row is None:
            row = (
                await self.db.execute(select(Agent).where(Agent.name == id_or_name))
            ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"Unknown agent {id_or_name}")
        return row

    async def list_agents(self, *, include_retired: bool = False) -> list[Agent]:
        statement = select(Agent).order_by(Agent.name)
        if not include_retired:
            statement = statement.where(Agent.retired_at.is_(None))
        return list((await self.db.execute(statement)).scalars())

    async def upsert_agent(
        self,
        name: str,
        *,
        kind: str,
        profile: str | None = None,
        role_title: str = "",
        job_description: str = "",
        reports_to_id: str | None = None,
        capabilities: list[str] | None = None,
        actor: Agent | None = None,
    ) -> Agent:
        existing = (
            await self.db.execute(select(Agent).where(Agent.name == name))
        ).scalar_one_or_none()
        if existing:
            if profile:
                existing.hermes_profile = profile
            if capabilities is not None:
                existing.capabilities_json = json.dumps(capabilities)
            if role_title:
                existing.role_title = role_title
            if job_description:
                existing.job_description = job_description
            if reports_to_id is not None:
                existing.reports_to_id = reports_to_id
            await self.db.flush()
            return existing
        agent = Agent(
            name=name,
            kind=kind,
            hermes_profile=profile,
            role_title=role_title,
            job_description=job_description,
            reports_to_id=reports_to_id,
            capabilities_json=json.dumps(capabilities or []),
            created_at=self.clock.now(),
        )
        self.db.add(agent)
        await self.db.flush()
        await self.record("agent", f"Agent {name}", actor_id=actor.id if actor else None)
        return agent

    async def hire_agent(
        self,
        actor: Agent,
        name: str,
        role_title: str,
        job_description: str,
        reports_to: str | None = None,
    ) -> Agent:
        if actor.name != "chief" or actor.kind != "hermes":
            raise TeamError("Only chief can hire teammates")
        handle = name.strip().lower().removeprefix("@")
        title = role_title.strip()
        description = " ".join(job_description.split())
        if not _HANDLE.fullmatch(handle):
            raise TeamError("Handle must be 2-32 lowercase letters, numbers, or underscores")
        if not title or len(title) > 120:
            raise TeamError("Role title must be 1-120 characters")
        if len(description) < 20 or len(description) > 600:
            raise TeamError("Job description must be 20-600 characters")
        if len([agent for agent in await self.list_agents() if agent.kind == "hermes"]) >= (
            self.budgets.max_active_agents
        ):
            raise BudgetExceeded("Active team size limit reached")
        existing = (
            await self.db.execute(select(Agent).where(Agent.name == handle))
        ).scalar_one_or_none()
        if existing:
            raise TeamError(f"@{handle} already exists")
        manager = await self.get_agent(reports_to or actor.name)
        if manager.kind != "hermes" or manager.retired_at is not None:
            raise TeamError("Manager must be an active teammate")
        profile = await self.profiles.create(handle, title, description)
        agent = await self.upsert_agent(
            handle,
            kind="hermes",
            profile=profile,
            role_title=title,
            job_description=description,
            reports_to_id=manager.id,
            capabilities=_capabilities(title, description),
            actor=actor,
        )
        await self._join_general(agent)
        await self.record(
            "agent.hire",
            f"Chief hired @{handle}",
            description,
            actor_id=actor.id,
            agent_id=agent.id,
            reports_to_id=manager.id,
            role_title=title,
        )
        return agent

    async def update_agent(
        self,
        actor: Agent,
        name: str,
        *,
        role_title: str | None = None,
        job_description: str | None = None,
        reports_to: str | None = None,
    ) -> Agent:
        if actor.name != "chief" or actor.kind != "hermes":
            raise TeamError("Only chief can reorganize teammates")
        agent = await self.get_agent(name.removeprefix("@"))
        if agent.name == "chief":
            raise TeamError("Chief's root position cannot be changed")
        title = role_title.strip() if role_title is not None else agent.role_title
        description = (
            " ".join(job_description.split())
            if job_description is not None
            else agent.job_description
        )
        if not title or len(title) > 120:
            raise TeamError("Role title must be 1-120 characters")
        if len(description) < 20 or len(description) > 600:
            raise TeamError("Job description must be 20-600 characters")
        if reports_to is not None:
            manager = await self.get_agent(reports_to.removeprefix("@"))
            if manager.id == agent.id or await self._reports_to(manager, agent.id):
                raise TeamError("Reporting lines cannot contain a cycle")
            agent.reports_to_id = manager.id
        agent.role_title = title
        agent.job_description = description
        agent.capabilities_json = json.dumps(_capabilities(title, description))
        await self.profiles.describe(agent.name, title, description)
        await self.record(
            "agent.update",
            f"Chief updated @{agent.name}",
            actor_id=actor.id,
            agent_id=agent.id,
        )
        await self.db.flush()
        return agent

    async def retire_agent(self, actor: Agent, name: str) -> Agent:
        if actor.name != "chief" or actor.kind != "hermes":
            raise TeamError("Only chief can retire teammates")
        agent = await self.get_agent(name.removeprefix("@"))
        if agent.name == "chief":
            raise TeamError("Chief cannot retire the root agent")
        for report in await self.list_agents():
            if report.reports_to_id == agent.id:
                report.reports_to_id = actor.id
        agent.retired_at = self.clock.now()
        agent.status = "retired"
        await self.record(
            "agent.retire",
            f"Chief retired @{agent.name}",
            actor_id=actor.id,
            agent_id=agent.id,
        )
        await self.db.flush()
        return agent

    async def _reports_to(self, agent: Agent, possible_manager_id: str) -> bool:
        manager_id = agent.reports_to_id
        seen: set[str] = set()
        while manager_id and manager_id not in seen:
            if manager_id == possible_manager_id:
                return True
            seen.add(manager_id)
            manager = await self.db.get(Agent, manager_id)
            manager_id = manager.reports_to_id if manager else None
        return False

    async def find_agents(self, capabilities: list[str]) -> list[Agent]:
        wanted = {item.lower() for item in capabilities if item}
        found: list[Agent] = []
        for agent in await self.list_agents():
            have = {item.lower() for item in json.loads(agent.capabilities_json or "[]")}
            if (not wanted or wanted <= have or wanted & have) and agent.kind != "human":
                found.append(agent)
        return found

    async def get_or_create_dm(self, left: Agent, right: Agent) -> Room:
        if left.id == right.id:
            raise TeamError("A DM needs two participants")
        rooms = (
            await self.db.execute(
                select(Room)
                .options(selectinload(Room.memberships))
                .where(Room.type == "dm", Room.lifecycle == "open")
            )
        ).scalars()
        pair = {left.id, right.id}
        for room in rooms:
            members = {member.agent_id for member in room.memberships}
            if members == pair:
                return room
        names = sorted([left.name, right.name])
        room = Room(
            type="dm",
            title=f"{names[0]} × {names[1]}",
            objective="Direct message",
            owner_id=left.id if left.kind == "human" else right.id,
            lifecycle="open",
            created_at=self.clock.now(),
        )
        self.db.add(room)
        await self.db.flush()
        self.db.add(
            Membership(room_id=room.id, agent_id=left.id, role="member", notification_level="all")
        )
        self.db.add(
            Membership(room_id=room.id, agent_id=right.id, role="member", notification_level="all")
        )
        await self.db.flush()
        return room

    async def create_room(
        self,
        actor: Agent,
        objective: str,
        participants: list[str],
        lifecycle: str,
        *,
        approved: bool = False,
        room_type: str | None = None,
    ) -> Room:
        del approved
        kind = room_type or ("channel" if lifecycle == "channel" else "task")
        if kind == "channel":
            return await self.create_channel(actor, objective, "", participants)
        if kind == "group":
            return await self.create_group_chat(actor, participants)
        if kind != "task":
            raise TeamError("Conversation type must be dm, group, or channel")
        names = [item for item in participants if item]
        if actor.name not in names and actor.id not in names:
            names.append(actor.name)
        agents = [await self.get_agent(name) for name in names]
        owner = (
            actor
            if actor.kind != "human"
            else next((a for a in agents if a.kind != "human"), actor)
        )
        title = objective.strip()[:80] or "Untitled"
        room = Room(
            type=kind,
            title=title,
            objective=objective,
            owner_id=owner.id,
            lifecycle="open",
            created_at=self.clock.now(),
        )
        self.db.add(room)
        await self.db.flush()
        for agent in agents:
            role = "owner" if agent.id == owner.id else "member"
            self.db.add(
                Membership(
                    room_id=room.id,
                    agent_id=agent.id,
                    role=role,
                    notification_level="mentions",
                )
            )
        await self.db.flush()
        await self.record(
            "room",
            f"Opened {kind} room",
            objective,
            actor_id=actor.id,
            room_id=room.id,
        )
        return room

    async def create_group_chat(self, actor: Agent, participants: list[str]) -> Room:
        names = list(dict.fromkeys([*participants, actor.name]))
        agents_by_id: dict[str, Agent] = {}
        for name in names:
            agent = await self.get_agent(name.removeprefix("@"))
            agents_by_id[agent.id] = agent
        agents = list(agents_by_id.values())
        if len(agents) < 2:
            raise TeamError("A group chat needs at least two participants")
        if len(agents) > self.budgets.max_group_participants:
            raise TeamError(
                f"A group chat can have at most {self.budgets.max_group_participants} people"
            )
        title = ", ".join(sorted(agent.name for agent in agents))
        room = Room(
            type="group",
            title=title,
            objective="Group chat",
            owner_id=actor.id,
            lifecycle="open",
            created_at=self.clock.now(),
        )
        self.db.add(room)
        await self.db.flush()
        for agent in agents:
            self.db.add(
                Membership(
                    room_id=room.id,
                    agent_id=agent.id,
                    role="owner" if agent.id == actor.id else "member",
                    notification_level="all",
                )
            )
        await self.db.flush()
        await self.record(
            "group.create",
            f"Started group chat {title}",
            actor_id=actor.id,
            room_id=room.id,
        )
        return room

    async def create_channel(
        self,
        actor: Agent,
        name: str,
        description: str,
        members: list[str] | None = None,
        owners: list[str] | None = None,
    ) -> Room:
        channel_name = name.strip().lower().removeprefix("#").replace(" ", "-")
        channel_description = " ".join(description.split())
        if not _CHANNEL.fullmatch(channel_name):
            raise TeamError("Channel names use 1-48 lowercase letters, numbers, - or _")
        existing = (
            await self.db.execute(
                select(Room).where(
                    Room.type == "channel",
                    Room.lifecycle == "open",
                    Room.title == channel_name,
                )
            )
        ).scalar_one_or_none()
        if existing:
            raise TeamError(f"#{channel_name} already exists")
        owner_names = list(dict.fromkeys([actor.name, *(owners or [])]))
        member_names = list(dict.fromkeys([*owner_names, *(members or [])]))
        owner_agents = [await self.get_agent(item.removeprefix("@")) for item in owner_names]
        agents = [await self.get_agent(item.removeprefix("@")) for item in member_names]
        owner_ids = {agent.id for agent in owner_agents}
        room = Room(
            type="channel",
            title=channel_name,
            objective=channel_description,
            owner_id=actor.id,
            lifecycle="open",
            created_at=self.clock.now(),
        )
        self.db.add(room)
        await self.db.flush()
        for agent in agents:
            self.db.add(
                Membership(
                    room_id=room.id,
                    agent_id=agent.id,
                    role="owner" if agent.id in owner_ids else "member",
                    notification_level="mentions",
                )
            )
        await self.db.flush()
        await self.record(
            "channel.create",
            f"Opened #{channel_name}",
            channel_description,
            actor_id=actor.id,
            room_id=room.id,
        )
        return room

    async def list_channels(self, query: str = "") -> list[Room]:
        rooms = await self.list_rooms(type="channel", lifecycle="open")
        needle = query.strip().lower().removeprefix("#")
        if not needle:
            return sorted(rooms, key=lambda room: room.title)
        return [
            room
            for room in sorted(rooms, key=lambda room: room.title)
            if needle in room.title.lower() or needle in room.objective.lower()
        ]

    async def join_channel(self, actor: Agent, room_id_or_name: str) -> Membership:
        room = await self._channel(room_id_or_name)
        existing = next(
            (member for member in room.memberships if member.agent_id == actor.id), None
        )
        if existing:
            return existing
        membership = Membership(
            room_id=room.id,
            agent_id=actor.id,
            role="member",
            notification_level="mentions",
        )
        self.db.add(membership)
        await self.db.flush()
        await self.record(
            "channel.join",
            f"@{actor.name} joined #{room.title}",
            actor_id=actor.id,
            room_id=room.id,
        )
        return membership

    async def set_channel_notifications(self, actor: Agent, room_id: str, level: str) -> Membership:
        if level not in {"mentions", "all"}:
            raise TeamError("Channel notifications must be mentions or all")
        room = await self._channel(room_id)
        membership = next(
            (member for member in room.memberships if member.agent_id == actor.id), None
        )
        if membership is None:
            membership = await self.join_channel(actor, room.id)
        membership.notification_level = level
        await self.db.flush()
        return membership

    async def update_channel(
        self,
        actor: Agent,
        room_id: str,
        *,
        name: str,
        description: str,
        owners: list[str],
    ) -> Room:
        room = await self._channel(room_id)
        actor_membership = next(
            (member for member in room.memberships if member.agent_id == actor.id), None
        )
        if actor.name != "you" and (actor_membership is None or actor_membership.role != "owner"):
            raise TeamError("Only channel owners can edit channel settings")
        channel_name = name.strip().lower().removeprefix("#").replace(" ", "-")
        if not _CHANNEL.fullmatch(channel_name):
            raise TeamError("Channel names use 1-48 lowercase letters, numbers, - or _")
        if room.title == "general" and channel_name != "general":
            raise TeamError("#general cannot be renamed")
        duplicate = (
            await self.db.execute(
                select(Room).where(
                    Room.type == "channel", Room.title == channel_name, Room.id != room.id
                )
            )
        ).scalar_one_or_none()
        if duplicate:
            raise TeamError(f"#{channel_name} already exists")
        owner_agents = [await self.get_agent(item.removeprefix("@")) for item in owners]
        owner_ids = {agent.id for agent in owner_agents}
        if not owner_ids:
            raise TeamError("A channel needs at least one owner")
        for owner in owner_agents:
            if not any(member.agent_id == owner.id for member in room.memberships):
                room.memberships.append(
                    Membership(
                        agent_id=owner.id,
                        role="owner",
                        notification_level="mentions",
                    )
                )
        for membership in room.memberships:
            membership.role = "owner" if membership.agent_id in owner_ids else "member"
        room.title = channel_name
        room.objective = " ".join(description.split())
        room.owner_id = owner_agents[0].id
        await self.db.flush()
        return room

    async def invite(self, actor: Agent, room_id: str, agent_name: str) -> Membership:
        room = await self._room(room_id)
        agent = await self.get_agent(agent_name)
        existing = (
            await self.db.execute(
                select(Membership).where(
                    Membership.room_id == room.id, Membership.agent_id == agent.id
                )
            )
        ).scalar_one_or_none()
        if existing:
            return existing
        if room.type == "group" and len(room.memberships) >= self.budgets.max_group_participants:
            raise TeamError(
                f"A group chat can have at most {self.budgets.max_group_participants} people"
            )
        membership = Membership(
            room_id=room.id,
            agent_id=agent.id,
            role="member",
            notification_level="all" if room.type in {"dm", "group"} else "mentions",
        )
        self.db.add(membership)
        await self.db.flush()
        await self.record(
            "invite",
            f"Invited {agent.name}",
            actor_id=actor.id,
            room_id=room.id,
        )
        if room.type == "group":
            await self.enqueue_wake(
                target=agent,
                actor=actor,
                reason="assignment",
                context_reference=room.id,
                payload={"room_id": room.id, "objective": room.objective},
                dedupe_key=f"invite:{room.id}:{agent.id}",
            )
        return membership

    async def resolve_room(self, actor: Agent, room_id: str, result: str) -> Room:
        room = await self._room(room_id)
        room.lifecycle = "archived"
        room.resolved_result = result
        room.archived_at = self.clock.now()
        await self.db.flush()
        await self.record(
            "resolve",
            f"Resolved {room.title}",
            result,
            actor_id=actor.id,
            room_id=room.id,
        )
        return room

    async def list_rooms(self, type: str | None = None, lifecycle: str | None = None) -> list[Room]:
        stmt: Select[tuple[Room]] = select(Room).options(
            selectinload(Room.memberships).selectinload(Membership.agent),
            selectinload(Room.owner),
        )
        if type:
            stmt = stmt.where(Room.type == type)
        if lifecycle:
            stmt = stmt.where(Room.lifecycle == lifecycle)
        stmt = stmt.order_by(Room.created_at.desc())
        return list((await self.db.execute(stmt)).scalars().unique())

    async def list_inbox(self, viewer: Agent) -> list[InboxItem]:
        """Return the DMs, group chats, and joined channels visible to one identity."""
        memberships = list(
            (
                await self.db.execute(
                    select(Membership)
                    .options(
                        selectinload(Membership.room)
                        .selectinload(Room.memberships)
                        .selectinload(Membership.agent)
                    )
                    .where(Membership.agent_id == viewer.id)
                )
            ).scalars()
        )
        unread_room_ids = set(
            (
                await self.db.execute(
                    select(Notification.room_id).where(
                        Notification.agent_id == viewer.id,
                        Notification.read_at.is_(None),
                    )
                )
            ).scalars()
        )
        items: list[InboxItem] = []
        for membership in memberships:
            room = membership.room
            if room.lifecycle != "open" or room.type == "task":
                continue
            history = await self.history(room.id)
            last = history[-1] if history else None
            items.append(InboxItem(room, last, room.id in unread_room_ids))
        return sorted(
            items,
            key=lambda item: (
                item.last_message.created_at if item.last_message else item.room.created_at
            ),
            reverse=True,
        )

    async def mark_room_read(self, human: Agent, room_id: str, last_message_id: str | None) -> None:
        state = await self.db.get(RoomRead, (room_id, human.id))
        if state is None:
            state = RoomRead(room_id=room_id, agent_id=human.id)
            self.db.add(state)
        state.last_read_message_id = last_message_id
        notifications = list(
            (
                await self.db.execute(
                    select(Notification).where(
                        Notification.room_id == room_id,
                        Notification.agent_id == human.id,
                        Notification.read_at.is_(None),
                    )
                )
            ).scalars()
        )
        for notification in notifications:
            notification.read_at = self.clock.now()
        await self.db.flush()

    async def follow_room(self, human: Agent, room_id: str) -> Membership:
        membership = await self.join_channel(human, room_id)
        membership.notification_level = "all"
        await self.db.flush()
        return membership

    async def unfollow_room(self, human: Agent, room_id: str) -> None:
        await self.set_channel_notifications(human, room_id, "mentions")

    async def send_message(
        self,
        actor: Agent,
        room_id: str,
        body: str,
        mentions: list[str] | None = None,
        parent_message_id: str | None = None,
        source_wake_event_id: str | None = None,
    ) -> Message:
        room = await self._room(room_id)
        await self._require_member(actor, room)
        text_body = body.strip()
        if not text_body:
            raise TeamError("Message cannot be empty")
        parent = None
        if parent_message_id:
            parent = await self.db.get(Message, parent_message_id)
            if parent is None or parent.room_id != room.id:
                raise TeamError("Thread root must be a message in this conversation")
            if parent.parent_message_id:
                parent = await self.db.get(Message, parent.parent_message_id)
        names = list(dict.fromkeys((mentions or []) + mention_names(body)))
        if source_wake_event_id:
            source_event = await self.db.get(WakeEvent, source_wake_event_id)
            if source_event is None or source_event.target_id != actor.id:
                raise TeamError("Message source must be this agent's wake event")
        message = Message(
            room_id=room.id,
            parent=parent,
            sender_id=actor.id,
            source_wake_event_id=source_wake_event_id,
            body=text_body,
            mentions_json=json.dumps(names),
            created_at=self.clock.now(),
        )
        self.db.add(message)
        await self.db.flush()
        await self.record(
            "message",
            f"{actor.name} in {room.title}",
            text_body[:180],
            actor_id=actor.id,
            room_id=room.id,
        )
        await self._notify_for_message(actor, room, message, names)
        await self._push_portal(actor, room, text_body)
        return message

    async def history(
        self, room_id: str, limit: int = 80, viewer: Agent | None = None
    ) -> list[Message]:
        if viewer is not None:
            await self._require_member(viewer, await self._room(room_id), allow_owner=True)
        stmt = (
            select(Message)
            .options(
                selectinload(Message.sender),
                selectinload(Message.reactions).selectinload(MessageReaction.agent),
            )
            .where(Message.room_id == room_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        rows = list((await self.db.execute(stmt)).scalars())
        rows.reverse()
        return rows

    async def set_message_reaction(
        self, actor: Agent, message_id: str, value: str
    ) -> tuple[str, str | None]:
        if value not in {"up", "down"}:
            raise TeamError("Reaction must be up or down")
        message = await self.db.get(Message, message_id)
        if message is None:
            raise NotFound(f"Unknown message {message_id}")
        await self._require_member(actor, await self._room(message.room_id), allow_owner=True)
        reaction = (
            await self.db.execute(
                select(MessageReaction).where(
                    MessageReaction.message_id == message.id,
                    MessageReaction.agent_id == actor.id,
                )
            )
        ).scalar_one_or_none()
        if reaction and reaction.value == value:
            await self.db.delete(reaction)
            await self.db.flush()
            return message.room_id, None
        if reaction:
            reaction.value = value
        else:
            self.db.add(MessageReaction(message_id=message.id, agent_id=actor.id, value=value))
        await self.db.flush()
        return message.room_id, value

    async def thread_replies(self, root_message_id: str) -> list[Message]:
        root = await self.db.get(Message, root_message_id)
        if root is None:
            raise NotFound(f"Unknown message {root_message_id}")
        statement = (
            select(Message)
            .options(
                selectinload(Message.sender),
                selectinload(Message.reactions).selectinload(MessageReaction.agent),
            )
            .where(Message.parent_message_id == root.id)
            .order_by(Message.created_at.asc())
        )
        return list((await self.db.execute(statement)).scalars())

    async def schedule_loop(
        self,
        actor: Agent,
        reason: str,
        target: str,
        due_at,
        context: dict | None = None,
    ) -> OpenLoop:
        agent = await self.get_agent(target)
        loop = OpenLoop(
            agent_id=actor.id,
            target_id=agent.id,
            reason=reason,
            due_at=due_at,
            context_json=json.dumps(context or {}),
            status="open",
            created_at=self.clock.now(),
        )
        self.db.add(loop)
        await self.db.flush()
        await self.enqueue_wake(
            target=agent,
            actor=actor,
            reason="open_loop",
            context_reference=loop.id,
            due_at=due_at,
            payload={"loop_id": loop.id, "reason": reason, **(context or {})},
            dedupe_key=f"loop:{loop.id}",
            state_hash=reason,
        )
        await self.record("loop", f"Scheduled {reason}", actor_id=actor.id)
        return loop

    async def create_routine(
        self,
        actor: Agent,
        reason: str,
        *,
        source: str = "",
        interval_seconds: int = 0,
        match_any: list[str] | None = None,
        ignore_any: list[str] | None = None,
        context: dict | None = None,
    ) -> Routine:
        next_due = None
        if interval_seconds > 0:
            next_due = self.clock.now() + timedelta(seconds=interval_seconds)
        routine = Routine(
            agent_id=actor.id,
            reason=reason,
            source=source,
            interval_seconds=interval_seconds,
            match_any_json=json.dumps(match_any or []),
            ignore_any_json=json.dumps(ignore_any or []),
            context_json=json.dumps(context or {}),
            next_due_at=next_due,
            created_at=self.clock.now(),
        )
        self.db.add(routine)
        await self.db.flush()
        await self.record("routine", f"Routine {reason}", source, actor_id=actor.id)
        self.signal.notify()
        return routine

    async def list_routines(self, agent_id: str | None = None) -> list[Routine]:
        stmt = select(Routine).where(Routine.active == 1)
        if agent_id:
            stmt = stmt.where(Routine.agent_id == agent_id)
        return list((await self.db.execute(stmt)).scalars())

    async def list_loops(self, agent_id: str) -> list[OpenLoop]:
        stmt = select(OpenLoop).where(OpenLoop.agent_id == agent_id, OpenLoop.status == "open")
        return list((await self.db.execute(stmt)).scalars())

    async def create_task(
        self, actor: Agent, room_id: str, owner: str, objective: str
    ) -> KanbanRef:
        room = await self._room(room_id)
        assignee = await self.get_agent(owner)
        profile = assignee.hermes_profile or assignee.name
        ticket = await self.kanban.create(objective, profile, objective)
        ref = KanbanRef(
            room_id=room.id,
            hermes_task_id=ticket.id,
            owner_id=assignee.id,
            title=objective[:180],
            last_status=ticket.status,
            last_seen_at=self.clock.now(),
            created_at=self.clock.now(),
        )
        self.db.add(ref)
        await self.db.flush()
        await self.record(
            "task",
            f"Kanban {ticket.id}",
            objective,
            actor_id=actor.id,
            room_id=room.id,
            hermes_task_id=ticket.id,
        )
        await self.enqueue_wake(
            target=assignee,
            actor=actor,
            reason="assignment",
            context_reference=ref.id,
            payload={
                "room_id": room.id,
                "task_id": ref.id,
                "hermes_task_id": ticket.id,
                "objective": objective,
            },
            dedupe_key=f"task:{ticket.id}:{assignee.id}",
        )
        return ref

    async def update_task(self, actor: Agent, task_id: str, status: str) -> KanbanRef:
        ref = await self.db.get(KanbanRef, task_id)
        if ref is None:
            ref = (
                await self.db.execute(select(KanbanRef).where(KanbanRef.hermes_task_id == task_id))
            ).scalar_one_or_none()
        if ref is None:
            raise NotFound(f"Unknown task {task_id}")
        ticket = await self.kanban.update(ref.hermes_task_id, status)
        previous = ref.last_status
        ref.last_status = ticket.status
        ref.last_seen_at = self.clock.now()
        await self.db.flush()
        await self.record(
            "task",
            f"Kanban {ref.hermes_task_id} → {ticket.status}",
            actor_id=actor.id,
            room_id=ref.room_id,
        )
        if previous != ticket.status:
            owner = await self.db.get(Agent, ref.owner_id)
            if owner:
                await self.enqueue_wake(
                    target=owner,
                    actor=actor,
                    reason="kanban",
                    context_reference=ref.id,
                    payload={"task_id": ref.id, "status": ticket.status, "room_id": ref.room_id},
                    dedupe_key=f"kanban:{ref.hermes_task_id}:{ticket.status}",
                    state_hash=ticket.status,
                )
        return ref

    async def list_tasks(self, room_id: str | None = None) -> list[KanbanRef]:
        stmt = select(KanbanRef).options(
            selectinload(KanbanRef.owner), selectinload(KanbanRef.room)
        )
        if room_id:
            stmt = stmt.where(KanbanRef.room_id == room_id)
        return list((await self.db.execute(stmt)).scalars())

    async def enqueue_wake(
        self,
        *,
        target: Agent,
        actor: Agent | None,
        reason: str,
        context_reference: str = "",
        payload: dict | None = None,
        due_at=None,
        dedupe_key: str | None = None,
        state_hash: str = "",
    ) -> WakeEvent | None:
        if target.retired_at is not None:
            raise TeamError(f"@{target.name} is retired")
        when = due_at or self.clock.now()
        body = payload or {}
        digest = state_hash or _hash(body)
        await self._reject_recursive_self_wake(actor, target, when, digest)
        if dedupe_key:
            existing = (
                await self.db.execute(
                    select(WakeEvent).where(
                        WakeEvent.dedupe_key == dedupe_key,
                        WakeEvent.status.in_(("pending", "claimed")),
                    )
                )
            ).scalar_one_or_none()
            if existing:
                return existing
        event = WakeEvent(
            target_id=target.id,
            actor_id=actor.id if actor else None,
            reason=reason,
            context_reference=context_reference,
            due_at=when,
            dedupe_key=dedupe_key,
            status="pending",
            payload_json=json.dumps(body, default=str),
            state_hash=digest,
            created_at=self.clock.now(),
        )
        self.db.add(event)
        await self.db.flush()
        await self.record(
            "wake",
            f"Wake {target.name} ({reason})",
            actor_id=actor.id if actor else None,
            event_id=event.id,
        )
        self.signal.notify()
        return event

    async def claim_due_events(self, limit: int = 16) -> list[WakeEvent]:
        now = self.clock.now()
        rows = list(
            (
                await self.db.execute(
                    select(WakeEvent)
                    .where(WakeEvent.status == "pending", WakeEvent.due_at <= now)
                    .order_by(WakeEvent.due_at.asc())
                    .limit(limit)
                )
            ).scalars()
        )
        claimed: list[WakeEvent] = []
        seen: set[str] = set()
        for event in rows:
            if event.dedupe_key and event.dedupe_key in seen:
                event.status = "ignored"
                continue
            if event.dedupe_key:
                seen.add(event.dedupe_key)
            busy = await self._running_count(event.target_id)
            if busy >= self.budgets.max_concurrency:
                continue
            event.status = "claimed"
            event.claimed_at = now
            claimed.append(event)
        await self.db.flush()
        return claimed

    async def complete_event(
        self, event: WakeEvent, run: Run, decision: str, note: str = ""
    ) -> None:
        event.status = "done"
        run.status = "done"
        run.decision = decision
        run.note = note
        run.ended_at = self.clock.now()
        agent = await self.db.get(Agent, event.target_id)
        if agent:
            agent.status = "idle"
        await self.db.flush()

    async def ignore_event(self, event: WakeEvent, run: Run | None, note: str) -> None:
        event.status = "ignored"
        if run:
            run.status = "ignored"
            run.decision = "ignore"
            run.note = note
            run.ended_at = self.clock.now()
        agent = await self.db.get(Agent, event.target_id)
        if agent:
            agent.status = "idle"
        await self.db.flush()

    async def earliest_due(self):
        wake = (
            await self.db.execute(
                select(WakeEvent.due_at)
                .where(WakeEvent.status == "pending")
                .order_by(WakeEvent.due_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        routine = (
            await self.db.execute(
                select(Routine.next_due_at)
                .where(Routine.active == 1, Routine.next_due_at.is_not(None))
                .order_by(Routine.next_due_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        candidates = [item for item in (wake, routine) if item is not None]
        return min(candidates) if candidates else None

    async def start_run(self, event: WakeEvent) -> Run:
        run = Run(
            agent_id=event.target_id,
            wake_event_id=event.id,
            status="running",
            max_turns=self.budgets.max_turns,
            max_handoffs=self.budgets.max_handoffs,
            started_at=self.clock.now(),
        )
        self.db.add(run)
        event.claimed_run_id = run.id
        agent = await self.db.get(Agent, event.target_id)
        if agent:
            agent.status = "running"
        await self.db.flush()
        return run

    async def ingest_webhook(
        self,
        source: str,
        payload: dict,
        *,
        agent_name: str | None = None,
        dedupe_key: str | None = None,
    ) -> list[WakeEvent]:
        routines = await self.list_routines()
        matching = [row for row in routines if not row.source or row.source == source]
        if agent_name:
            agent = await self.get_agent(agent_name)
            matching = [row for row in matching if row.agent_id == agent.id]
            if not matching:
                await self.record(
                    "filter",
                    f"{source} → no routine",
                    actor_id=agent.id,
                    verdict="no",
                )
                return []
        created: list[WakeEvent] = []
        for routine in matching:
            verdict = decide(
                payload,
                match_any=parse_list(routine.match_any_json),
                ignore_any=parse_list(routine.ignore_any_json),
            )
            await self.record(
                "filter",
                f"{source} → {verdict}",
                json.dumps(payload, default=str)[:240],
                actor_id=routine.agent_id,
                verdict=verdict,
            )
            if verdict == "no":
                continue
            agent = await self.db.get(Agent, routine.agent_id)
            if agent is None:
                continue
            event = await self.enqueue_wake(
                target=agent,
                actor=None,
                reason="webhook",
                context_reference=routine.id,
                payload={"source": source, "verdict": verdict, "routine_id": routine.id, **payload},
                dedupe_key=dedupe_key or f"hook:{source}:{routine.id}:{_hash(payload)}",
            )
            if event:
                created.append(event)
        return created

    async def fire_due_routines_and_loops(self) -> int:
        now = self.clock.now()
        count = 0
        for routine in await self.list_routines():
            if not routine.next_due_at or routine.next_due_at > now:
                continue
            agent = await self.db.get(Agent, routine.agent_id)
            if agent is None:
                continue
            await self.enqueue_wake(
                target=agent,
                actor=agent,
                reason="routine",
                context_reference=routine.id,
                payload={
                    "routine_id": routine.id,
                    "reason": routine.reason,
                    "source": routine.source,
                },
                dedupe_key=f"routine:{routine.id}:{int(now.timestamp())}",
                due_at=now,
                state_hash=f"{routine.id}:{routine.next_due_at.isoformat()}",
            )
            routine.last_fired_at = now
            if routine.interval_seconds:
                routine.next_due_at = now + timedelta(seconds=routine.interval_seconds)
            count += 1
        loops = list(
            (
                await self.db.execute(
                    select(OpenLoop).where(OpenLoop.status == "open", OpenLoop.due_at <= now)
                )
            ).scalars()
        )
        for loop in loops:
            loop.status = "fired"
            count += 1
        await self.db.flush()
        return count

    async def load_context(self, agent: Agent) -> dict:
        memberships = list(
            (
                await self.db.execute(
                    select(Membership)
                    .options(selectinload(Membership.room))
                    .where(Membership.agent_id == agent.id)
                )
            ).scalars()
        )
        rooms = []
        for membership in memberships:
            room = membership.room
            if room.lifecycle == "archived" and room.type != "task":
                continue
            history = await self.history(room.id, limit=20)
            rooms.append(
                {
                    "id": room.id,
                    "type": room.type,
                    "title": room.title,
                    "objective": room.objective,
                    "lifecycle": room.lifecycle,
                    "role": membership.role,
                    "messages": [
                        {
                            "id": message.id,
                            "sender": message.sender.name if message.sender else "",
                            "body": message.body,
                            "at": message.created_at.isoformat(),
                        }
                        for message in history
                    ],
                }
            )
        notifications = list(
            (
                await self.db.execute(
                    select(Notification)
                    .options(
                        selectinload(Notification.room),
                        selectinload(Notification.message).selectinload(Message.sender),
                    )
                    .where(
                        Notification.agent_id == agent.id,
                        Notification.delivered_at.is_(None),
                    )
                    .order_by(Notification.created_at.asc())
                    .limit(50)
                )
            ).scalars()
        )
        for notification in notifications:
            notification.delivered_at = self.clock.now()
        await self.db.flush()
        return {
            "agent": {
                "id": agent.id,
                "name": agent.name,
                "kind": agent.kind,
                "profile": agent.hermes_profile,
                "role_title": agent.role_title,
                "job_description": agent.job_description,
                "reports_to_id": agent.reports_to_id,
                "capabilities": json.loads(agent.capabilities_json or "[]"),
            },
            "rooms": rooms,
            "notifications": [
                {
                    "id": notification.id,
                    "kind": notification.kind,
                    "room_id": notification.room_id,
                    "room": notification.room.title,
                    "message_id": notification.message_id,
                    "parent_message_id": notification.message.parent_message_id,
                    "sender": notification.message.sender.name,
                    "body": notification.message.body,
                    "at": notification.created_at.isoformat(),
                }
                for notification in notifications
            ],
            "routines": [
                {
                    "id": row.id,
                    "reason": row.reason,
                    "source": row.source,
                    "match_any": parse_list(row.match_any_json),
                }
                for row in await self.list_routines(agent.id)
            ],
            "open_loops": [
                {"id": row.id, "reason": row.reason, "due_at": row.due_at.isoformat()}
                for row in await self.list_loops(agent.id)
            ],
            "tasks": [
                {
                    "id": row.id,
                    "hermes_task_id": row.hermes_task_id,
                    "title": row.title,
                    "status": row.last_status,
                    "room_id": row.room_id,
                }
                for row in await self.list_tasks()
                if row.owner_id == agent.id
            ],
        }

    async def list_activity(self, limit: int = 80) -> list[Activity]:
        stmt = select(Activity).order_by(Activity.created_at.desc()).limit(limit)
        return list((await self.db.execute(stmt)).scalars())

    async def _notify_for_message(
        self, actor: Agent, room: Room, message: Message, mention_names_list: list[str]
    ) -> None:
        members = list(
            (
                await self.db.execute(
                    select(Membership)
                    .options(selectinload(Membership.agent))
                    .where(Membership.room_id == room.id)
                )
            ).scalars()
        )
        mentioned = {name.lower() for name in mention_names_list}
        notifications_by_agent: dict[str, Notification] = {}
        source_event = (
            await self.db.get(WakeEvent, message.source_wake_event_id)
            if message.source_wake_event_id
            else None
        )
        is_group_round_response = bool(
            room.type == "group"
            and source_event
            and source_event.reason == "group_round"
            and source_event.context_reference == room.id
        )
        thread_participants: set[str] = set()
        if message.parent_message_id:
            thread_messages = list(
                (
                    await self.db.execute(
                        select(Message).where(
                            (Message.id == message.parent_message_id)
                            | (Message.parent_message_id == message.parent_message_id)
                        )
                    )
                ).scalars()
            )
            thread_participants.update(item.sender_id for item in thread_messages)
            thread_names = {
                name.lower()
                for item in thread_messages
                for name in json.loads(item.mentions_json or "[]")
            }
            thread_participants.update(
                member.agent_id for member in members if member.agent.name.lower() in thread_names
            )
        for membership in members:
            agent = membership.agent
            if agent.id == actor.id or agent.retired_at is not None:
                continue
            kind = None
            if room.type == "dm":
                kind = "dm"
            elif room.type == "group":
                kind = "group"
            elif message.parent_message_id and agent.id in thread_participants:
                kind = "thread"
            elif agent.name.lower() in mentioned:
                kind = "mention"
            elif room.type == "channel" and membership.notification_level == "all":
                kind = "channel"
            elif room.type == "task" and agent.name.lower() in mentioned:
                kind = "mention"
            if kind is None:
                continue
            notification = Notification(
                agent_id=agent.id,
                room_id=room.id,
                message_id=message.id,
                kind=kind,
                created_at=self.clock.now(),
            )
            self.db.add(notification)
            await self.db.flush()
            notifications_by_agent[agent.id] = notification
            if room.type == "group":
                continue
            if agent.kind == "human":
                continue
            event = await self.enqueue_wake(
                target=agent,
                actor=actor,
                reason="human_dm" if kind == "dm" and actor.kind == "human" else kind,
                context_reference=room.id,
                payload={
                    "notification_id": notification.id,
                    "room_id": room.id,
                    "message_id": message.id,
                    "parent_message_id": message.parent_message_id,
                    "body": message.body,
                    "from": actor.name,
                },
                dedupe_key=f"msg:{message.id}:{agent.id}",
            )
            notification.wake_event_id = event.id if event else None
        if room.type == "group" and not is_group_round_response:
            events = await self._enqueue_group_round(
                room,
                [message],
                round_id=message.id,
                sequence=1,
                actor=actor,
                target_ids=set(notifications_by_agent),
            )
            for agent_id, event in events.items():
                notification = notifications_by_agent.get(agent_id)
                if notification:
                    notification.wake_event_id = event.id
        await self.db.flush()

    async def _enqueue_group_round(
        self,
        room: Room,
        messages: list[Message],
        *,
        round_id: str,
        sequence: int,
        actor: Agent | None,
        target_ids: set[str],
    ) -> dict[str, WakeEvent]:
        members = list(
            (
                await self.db.execute(
                    select(Membership)
                    .options(selectinload(Membership.agent))
                    .where(Membership.room_id == room.id)
                )
            ).scalars()
        )
        names_by_id = {membership.agent.id: membership.agent.name for membership in members}
        snapshot = [
            {
                "id": item.id,
                "from": names_by_id.get(item.sender_id, item.sender_id),
                "body": item.body[:4000],
            }
            for item in messages
        ]
        events: dict[str, WakeEvent] = {}
        for membership in members:
            target = membership.agent
            if (
                target.id not in target_ids
                or target.kind == "human"
                or target.retired_at is not None
            ):
                continue
            event = await self.enqueue_wake(
                target=target,
                actor=actor,
                reason="group_round",
                context_reference=room.id,
                payload={
                    "room_id": room.id,
                    "round_id": round_id,
                    "round": sequence,
                    "participants": sorted(names_by_id.values()),
                    "messages": snapshot,
                },
                dedupe_key=f"group-round:{round_id}:{sequence}:{target.id}",
                state_hash=_hash(snapshot),
            )
            if event:
                events[target.id] = event
        if events:
            await self.record(
                "group.round",
                f"Group round {sequence} in {room.title}",
                actor_id=actor.id if actor else None,
                room_id=room.id,
                round_id=round_id,
                recipients=sorted(names_by_id[target_id] for target_id in events),
                messages=[item.id for item in messages],
            )
        return events

    async def settle_group_round(self, event: WakeEvent) -> None:
        if event.reason != "group_round":
            return
        payload = json.loads(event.payload_json or "{}")
        round_id = str(payload.get("round_id") or "")
        sequence = int(payload.get("round") or 0)
        room_id = str(payload.get("room_id") or event.context_reference)
        if not round_id or not sequence or not room_id:
            return
        prefix = f"group-round:{round_id}:{sequence}:"
        siblings = list(
            (
                await self.db.execute(
                    select(WakeEvent).where(WakeEvent.dedupe_key.like(f"{prefix}%"))
                )
            ).scalars()
        )
        if any(item.status in {"pending", "claimed"} for item in siblings):
            return
        sibling_ids = [item.id for item in siblings]
        responses = list(
            (
                await self.db.execute(
                    select(Message)
                    .where(
                        Message.room_id == room_id,
                        Message.source_wake_event_id.in_(sibling_ids),
                    )
                    .order_by(Message.created_at.asc())
                )
            ).scalars()
        )
        if not responses:
            await self.record(
                "group.round.end",
                f"Group conversation settled after round {sequence}",
                room_id=room_id,
                round_id=round_id,
            )
            return
        if sequence >= self.budgets.max_group_rounds:
            await self.record(
                "group.round.cap",
                f"Group conversation stopped at round {sequence}",
                room_id=room_id,
                round_id=round_id,
            )
            return
        room = await self._room(room_id)
        responders = {message.sender_id for message in responses}
        target_ids = {
            membership.agent_id
            for membership in room.memberships
            if membership.agent_id not in responders
            and membership.agent.kind == "hermes"
            and membership.agent.retired_at is None
        }
        if not target_ids:
            await self.record(
                "group.round.end",
                f"Group conversation settled after round {sequence}",
                room_id=room_id,
                round_id=round_id,
            )
            return
        events = await self._enqueue_group_round(
            room,
            responses,
            round_id=round_id,
            sequence=sequence + 1,
            actor=None,
            target_ids=target_ids,
        )
        if not events:
            return
        notifications = list(
            (
                await self.db.execute(
                    select(Notification).where(
                        Notification.agent_id.in_(list(events)),
                        Notification.message_id.in_([message.id for message in responses]),
                    )
                )
            ).scalars()
        )
        for notification in notifications:
            notification.wake_event_id = events[notification.agent_id].id
        await self.db.flush()

    async def _push_portal(self, actor: Agent, room: Room, body: str) -> None:
        if room.type != "dm" or actor.kind == "human":
            return
        members = list(
            (
                await self.db.execute(
                    select(Membership)
                    .options(selectinload(Membership.agent))
                    .where(Membership.room_id == room.id)
                )
            ).scalars()
        )
        if not any(member.agent.kind == "human" for member in members):
            return
        await self.portal.push(sender=actor.name, body=body, room_title=room.title)

    async def get_room(self, room_id: str) -> Room:
        room = (
            await self.db.execute(
                select(Room)
                .options(
                    selectinload(Room.memberships).selectinload(Membership.agent),
                    selectinload(Room.owner),
                )
                .where(Room.id == room_id)
            )
        ).scalar_one_or_none()
        if room is None:
            raise NotFound(f"Unknown room {room_id}")
        return room

    async def organization(self) -> list[dict[str, Any]]:
        agents = [agent for agent in await self.list_agents() if agent.kind != "human"]
        by_id = {agent.id: agent for agent in agents}
        return [
            {
                "id": agent.id,
                "name": agent.name,
                "role_title": agent.role_title,
                "job_description": agent.job_description,
                "reports_to": (
                    by_id[agent.reports_to_id].name if agent.reports_to_id in by_id else None
                ),
                "direct_reports": sorted(
                    row.name for row in agents if row.reports_to_id == agent.id
                ),
            }
            for agent in sorted(agents, key=lambda row: row.name)
        ]

    async def _channel(self, room_id_or_name: str) -> Room:
        value = room_id_or_name.strip().removeprefix("#")
        room = await self.db.get(Room, value)
        if room is None:
            room = (
                await self.db.execute(
                    select(Room).where(Room.type == "channel", Room.title == value.lower())
                )
            ).scalar_one_or_none()
        if room is None or room.type != "channel" or room.lifecycle != "open":
            raise NotFound(f"Unknown channel {room_id_or_name}")
        return await self.get_room(room.id)

    async def _require_member(
        self, actor: Agent, room: Room, *, allow_owner: bool = False
    ) -> Membership | None:
        membership = next(
            (member for member in room.memberships if member.agent_id == actor.id), None
        )
        if membership is not None:
            return membership
        if allow_owner and actor.name == "you":
            return None
        raise TeamError(f"@{actor.name} is not a participant in {room.title}")

    async def _join_general(self, agent: Agent) -> None:
        general = (
            await self.db.execute(
                select(Room)
                .options(selectinload(Room.memberships))
                .where(Room.type == "channel", Room.title == "general")
            )
        ).scalar_one_or_none()
        if general is None or any(member.agent_id == agent.id for member in general.memberships):
            return
        general.memberships.append(
            Membership(agent_id=agent.id, role="member", notification_level="mentions")
        )
        await self.db.flush()

    async def _room(self, room_id: str) -> Room:
        return await self.get_room(room_id)

    async def _running_count(self, agent_id: str) -> int:
        rows = (
            await self.db.execute(
                select(Run).where(Run.agent_id == agent_id, Run.status == "running")
            )
        ).scalars()
        return len(list(rows))

    async def _reject_recursive_self_wake(
        self, actor: Agent | None, target: Agent, due_at, state_hash: str
    ) -> None:
        if actor is None or actor.id != target.id:
            return
        if due_at > self.clock.now():
            return
        previous = (
            await self.db.execute(
                select(WakeEvent)
                .where(WakeEvent.target_id == target.id, WakeEvent.actor_id == actor.id)
                .order_by(WakeEvent.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if previous and previous.state_hash == state_hash:
            raise SelfWakeRejected(
                "An agent cannot wake itself without a future time or changed state."
            )


def _hash(payload: object) -> str:
    blob = json.dumps(payload, default=str, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _capabilities(role_title: str, job_description: str) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9]+", f"{role_title} {job_description}".lower())
    terms: list[str] = []
    for word in words:
        if len(word) < 3 or word in _CAPABILITY_STOP:
            continue
        terms.append(word)
        if len(word) > 4 and word.endswith("s"):
            terms.append(word[:-1])
    return list(dict.fromkeys(terms))[:24]
