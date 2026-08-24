from __future__ import annotations

from datetime import datetime

from app.errors import BudgetExceeded, TeamError
from app.models import Agent, Message, Run
from app.services.team import TeamService

TOOL_NAMES = (
    "agents.find",
    "agents.list",
    "agents.org",
    "agents.hire",
    "agents.update",
    "agents.retire",
    "rooms.create",
    "rooms.invite",
    "conversations.start",
    "channels.list",
    "channels.join",
    "messages.send",
    "messages.reply",
    "tasks.create",
    "tasks.update",
    "loops.schedule",
    "routines.create",
    "rooms.resolve",
    "context.load",
)

HANDOFF_TOOLS = {
    "agents.hire",
    "rooms.create",
    "rooms.invite",
    "conversations.start",
    "tasks.create",
}


class Toolbelt:
    def __init__(self, team: TeamService, actor: Agent, run: Run | None = None) -> None:
        self.team = team
        self.actor = actor
        self.run = run

    async def call(self, name: str, arguments: dict) -> dict:
        if name not in TOOL_NAMES:
            raise TeamError(f"Unknown tool {name}")
        if name in HANDOFF_TOOLS and self.run is not None:
            if self.run.handoffs_used >= self.run.max_handoffs:
                raise BudgetExceeded("Handoff budget exhausted")
            self.run.handoffs_used += 1
        handler = {
            "agents.find": self._find,
            "agents.list": self._list_agents,
            "agents.org": self._org,
            "agents.hire": self._hire,
            "agents.update": self._update_agent,
            "agents.retire": self._retire_agent,
            "rooms.create": self._create_room,
            "rooms.invite": self._invite,
            "conversations.start": self._start_conversation,
            "channels.list": self._list_channels,
            "channels.join": self._join_channel,
            "messages.send": self._send,
            "messages.reply": self._reply,
            "tasks.create": self._create_task,
            "tasks.update": self._update_task,
            "loops.schedule": self._schedule,
            "routines.create": self._routine,
            "rooms.resolve": self._resolve,
            "context.load": self._context,
        }[name]
        result = await handler(arguments)
        await self.team.record(
            "tool",
            name,
            actor_id=self.actor.id,
            arguments=arguments,
            result=_preview(result),
        )
        return result

    async def _find(self, args: dict) -> dict:
        capabilities = args.get("capabilities") or []
        if isinstance(capabilities, str):
            capabilities = [item.strip() for item in capabilities.split(",") if item.strip()]
        agents = await self.team.find_agents(list(capabilities))
        return {
            "agents": [
                {
                    "id": agent.id,
                    "name": agent.name,
                    "capabilities": __import__("json").loads(agent.capabilities_json or "[]"),
                    "profile": agent.hermes_profile,
                    "role_title": agent.role_title,
                    "job_description": agent.job_description,
                    "reports_to_id": agent.reports_to_id,
                }
                for agent in agents
            ]
        }

    async def _list_agents(self, _args: dict) -> dict:
        agents = [agent for agent in await self.team.list_agents() if agent.kind != "human"]
        return {
            "agents": [
                {
                    "name": agent.name,
                    "role_title": agent.role_title,
                    "job_description": agent.job_description,
                    "status": agent.status,
                }
                for agent in agents
            ]
        }

    async def _org(self, _args: dict) -> dict:
        return {"organization": await self.team.organization()}

    async def _hire(self, args: dict) -> dict:
        agent = await self.team.hire_agent(
            self.actor,
            str(args.get("name") or ""),
            str(args.get("role_title") or ""),
            str(args.get("job_description") or ""),
            str(args["reports_to"]) if args.get("reports_to") else None,
        )
        return {
            "agent": agent.id,
            "name": agent.name,
            "profile": agent.hermes_profile,
            "role_title": agent.role_title,
            "reports_to": agent.reports_to_id,
        }

    async def _update_agent(self, args: dict) -> dict:
        agent = await self.team.update_agent(
            self.actor,
            str(args.get("agent") or ""),
            role_title=args.get("role_title"),
            job_description=args.get("job_description"),
            reports_to=args.get("reports_to"),
        )
        return {"agent": agent.id, "name": agent.name, "role_title": agent.role_title}

    async def _retire_agent(self, args: dict) -> dict:
        agent = await self.team.retire_agent(self.actor, str(args.get("agent") or ""))
        return {"agent": agent.id, "name": agent.name, "status": "retired"}

    async def _create_room(self, args: dict) -> dict:
        participants = args.get("participants") or []
        room = await self.team.create_room(
            self.actor,
            args.get("objective") or "",
            list(participants),
            args.get("lifecycle") or "task",
        )
        return {"room": room.id, "title": room.title, "type": room.type}

    async def _invite(self, args: dict) -> dict:
        membership = await self.team.invite(self.actor, args["room"], args["agent"])
        return {"room": membership.room_id, "agent": args["agent"]}

    async def _start_conversation(self, args: dict) -> dict:
        kind = str(args.get("type") or "dm")
        participants = [str(item) for item in (args.get("participants") or [])]
        if kind == "dm":
            if len(participants) != 1:
                raise TeamError("A DM needs exactly one other participant")
            room = await self.team.get_or_create_dm(
                self.actor, await self.team.get_agent(participants[0].removeprefix("@"))
            )
        elif kind == "group":
            room = await self.team.create_group_chat(self.actor, participants)
        elif kind == "channel":
            room = await self.team.create_channel(
                self.actor,
                str(args.get("name") or ""),
                str(args.get("description") or ""),
                participants,
                [str(item) for item in (args.get("owners") or [])],
            )
        else:
            raise TeamError("Conversation type must be dm, group, or channel")
        return {"room": room.id, "title": room.title, "type": room.type}

    async def _list_channels(self, args: dict) -> dict:
        channels = await self.team.list_channels(str(args.get("query") or ""))
        return {
            "channels": [
                {
                    "id": room.id,
                    "name": room.title,
                    "description": room.objective,
                    "joined": any(
                        membership.agent_id == self.actor.id for membership in room.memberships
                    ),
                    "owners": sorted(
                        membership.agent.name
                        for membership in room.memberships
                        if membership.role == "owner"
                    ),
                }
                for room in channels
            ]
        }

    async def _join_channel(self, args: dict) -> dict:
        membership = await self.team.join_channel(self.actor, str(args.get("channel") or ""))
        return {
            "room": membership.room_id,
            "notification_level": membership.notification_level,
        }

    async def _send(self, args: dict) -> dict:
        mentions = args.get("mentions") or []
        message = await self.team.send_message(
            self.actor,
            args["room"],
            args.get("message") or args.get("body") or "",
            list(mentions),
            parent_message_id=args.get("parent_message_id"),
            source_wake_event_id=self.run.wake_event_id if self.run else None,
        )
        return {"message": message.id, "room": message.room_id}

    async def _reply(self, args: dict) -> dict:
        root = await self.team.db.get(Message, args["message"])
        if root is None:
            raise TeamError(f"Unknown message {args['message']}")
        message = await self.team.send_message(
            self.actor,
            root.room_id,
            args.get("reply") or args.get("body") or "",
            list(args.get("mentions") or []),
            parent_message_id=root.id,
            source_wake_event_id=self.run.wake_event_id if self.run else None,
        )
        return {"message": message.id, "room": message.room_id, "thread": root.id}

    async def _create_task(self, args: dict) -> dict:
        ref = await self.team.create_task(
            self.actor,
            args["room"],
            args["owner"],
            args.get("objective") or "",
        )
        return {
            "task": ref.id,
            "hermes_task_id": ref.hermes_task_id,
            "status": ref.last_status,
        }

    async def _update_task(self, args: dict) -> dict:
        ref = await self.team.update_task(self.actor, args["task"], args.get("status") or "todo")
        return {"task": ref.id, "hermes_task_id": ref.hermes_task_id, "status": ref.last_status}

    async def _schedule(self, args: dict) -> dict:
        due_raw = args.get("due_at")
        if isinstance(due_raw, str):
            due_at = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
        else:
            due_at = due_raw
        loop = await self.team.schedule_loop(
            self.actor,
            args.get("reason") or "follow-up",
            args.get("target") or self.actor.name,
            due_at,
            args.get("context") or {},
        )
        return {"loop": loop.id, "due_at": loop.due_at.isoformat()}

    async def _routine(self, args: dict) -> dict:
        match_any = args.get("match_any") or []
        ignore_any = args.get("ignore_any") or []
        routine = await self.team.create_routine(
            self.actor,
            args.get("reason") or "watch",
            source=str(args.get("source") or ""),
            interval_seconds=int(args.get("interval_seconds") or 0),
            match_any=list(match_any),
            ignore_any=list(ignore_any),
            context=args.get("context") or {},
        )
        return {"routine": routine.id, "source": routine.source}

    async def _resolve(self, args: dict) -> dict:
        room = await self.team.resolve_room(self.actor, args["room"], args.get("result") or "")
        return {"room": room.id, "lifecycle": room.lifecycle}

    async def _context(self, _args: dict) -> dict:
        return await self.team.load_context(self.actor)


def tool_schemas() -> list[dict]:
    return [
        {
            "name": "agents.find",
            "description": "Find named team agents by capability.",
            "inputSchema": {
                "type": "object",
                "properties": {"capabilities": {"type": "array", "items": {"type": "string"}}},
            },
        },
        {
            "name": "agents.list",
            "description": "List every active teammate and their role.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "agents.org",
            "description": "Load the organization chart with managers and direct reports.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "agents.hire",
            "description": (
                "Hire a durable Hermes teammate when the active organization lacks the needed "
                "specialty. Reuse agents.find first."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role_title": {"type": "string"},
                    "job_description": {"type": "string"},
                    "reports_to": {"type": "string"},
                },
                "required": ["name", "role_title", "job_description"],
            },
        },
        {
            "name": "agents.update",
            "description": "Update a teammate's role, specialty, or manager without renaming them.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "role_title": {"type": "string"},
                    "job_description": {"type": "string"},
                    "reports_to": {"type": "string"},
                },
                "required": ["agent"],
            },
        },
        {
            "name": "agents.retire",
            "description": "Retire a durable teammate while preserving rooms, tasks, and history.",
            "inputSchema": {
                "type": "object",
                "properties": {"agent": {"type": "string"}},
                "required": ["agent"],
            },
        },
        {
            "name": "rooms.create",
            "description": "Create an internal Kanban task room or a named channel.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "participants": {"type": "array", "items": {"type": "string"}},
                    "lifecycle": {"type": "string"},
                },
                "required": ["objective", "participants"],
            },
        },
        {
            "name": "rooms.invite",
            "description": "Invite an existing agent into a room.",
            "inputSchema": {
                "type": "object",
                "properties": {"room": {"type": "string"}, "agent": {"type": "string"}},
                "required": ["room", "agent"],
            },
        },
        {
            "name": "conversations.start",
            "description": "Start a DM, private group chat, or public named channel.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["dm", "group", "channel"]},
                    "participants": {"type": "array", "items": {"type": "string"}},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "owners": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["type", "participants"],
            },
        },
        {
            "name": "channels.list",
            "description": "Search all public channels and see whether you have joined them.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
        {
            "name": "channels.join",
            "description": (
                "Join a public channel by id or #name. Notifications default to mentions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"channel": {"type": "string"}},
                "required": ["channel"],
            },
        },
        {
            "name": "messages.send",
            "description": "Send a DM, group message, channel post, or thread reply.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "room": {"type": "string"},
                    "message": {"type": "string"},
                    "mentions": {"type": "array", "items": {"type": "string"}},
                    "parent_message_id": {"type": "string"},
                },
                "required": ["room", "message"],
            },
        },
        {
            "name": "messages.reply",
            "description": "Reply in the thread rooted at a message.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "reply": {"type": "string"},
                    "mentions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["message", "reply"],
            },
        },
        {
            "name": "tasks.create",
            "description": "Create a Hermes Kanban task linked to a room.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "room": {"type": "string"},
                    "owner": {"type": "string"},
                    "objective": {"type": "string"},
                },
                "required": ["room", "owner", "objective"],
            },
        },
        {
            "name": "tasks.update",
            "description": "Update a linked Hermes Kanban task status.",
            "inputSchema": {
                "type": "object",
                "properties": {"task": {"type": "string"}, "status": {"type": "string"}},
                "required": ["task", "status"],
            },
        },
        {
            "name": "loops.schedule",
            "description": "Schedule a future wake for an agent.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "target": {"type": "string"},
                    "due_at": {"type": "string"},
                    "context": {"type": "object"},
                },
                "required": ["reason", "target", "due_at"],
            },
        },
        {
            "name": "routines.create",
            "description": "Watch a feed. Stay idle until a matching webhook arrives.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "source": {"type": "string"},
                    "match_any": {"type": "array", "items": {"type": "string"}},
                    "ignore_any": {"type": "array", "items": {"type": "string"}},
                    "interval_seconds": {"type": "integer"},
                },
                "required": ["reason", "source"],
            },
        },
        {
            "name": "rooms.resolve",
            "description": "Archive a resolved task room.",
            "inputSchema": {
                "type": "object",
                "properties": {"room": {"type": "string"}, "result": {"type": "string"}},
                "required": ["room", "result"],
            },
        },
        {
            "name": "context.load",
            "description": (
                "Load this agent's identity, rooms, queued notifications, loops, and Kanban refs."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _preview(result: dict) -> dict:
    return {key: result[key] for key in list(result)[:8]}
