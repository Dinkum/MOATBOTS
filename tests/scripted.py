from __future__ import annotations

import json
import re

from app.models import Agent, Run, WakeEvent
from app.runtime.base import Runtime, RuntimeResult
from app.services.team import TeamService
from app.services.tools import Toolbelt

_STOP = {"the", "a", "an", "for", "my", "our", "to", "on", "of"}
_FAIL = ("fail", "failure", "crash", "error", "down", "outage", "critical", "sev")


class ScriptedRuntime(Runtime):
    """Test-only dummy teammate. Not a product runtime."""

    async def run(
        self,
        team: TeamService,
        agent: Agent,
        event: WakeEvent,
        run: Run,
    ) -> RuntimeResult:
        tools = Toolbelt(team, agent, run)
        payload = json.loads(event.payload_json or "{}")
        body = str(payload.get("body") or payload.get("message") or "")
        if event.reason in {"human_dm", "mention"} and "monitor" in body.lower():
            return await self._watch(team, agent, event, payload, body)
        if event.reason == "webhook":
            return await self._webhook(tools, team, agent, payload)
        if event.reason in {"assignment", "mention"} and _has_capability(agent, "investigate"):
            return await self._investigate(tools, team, agent, payload)
        if event.reason == "mention" and _has_capability(agent, "coordinate"):
            return await self._wrap_up(tools, team, agent, payload)
        if event.reason in {"routine", "open_loop"}:
            return RuntimeResult("ignore", "nothing worth reporting")
        return RuntimeResult("ignore", "nothing worth reporting")

    async def _watch(
        self,
        team: TeamService,
        agent: Agent,
        event: WakeEvent,
        payload: dict,
        body: str,
    ) -> RuntimeResult:
        source = _infer_source(body)
        await team.create_routine(
            agent,
            reason=f"monitor {source}",
            source=source,
            match_any=list(_FAIL),
            ignore_any=["ok", "green", "healthy", "success"],
            context={"requested": body},
        )
        room_id = payload.get("room_id") or event.context_reference
        await team.send_message(
            agent,
            room_id,
            f"Watching {source}. I'll stay quiet unless something breaks.",
        )
        return RuntimeResult("idle", f"watching {source}")

    async def _webhook(
        self,
        tools: Toolbelt,
        team: TeamService,
        agent: Agent,
        payload: dict,
    ) -> RuntimeResult:
        verdict = payload.get("verdict")
        blob = json.dumps(payload).lower()
        meaningful = verdict == "yes" or (verdict != "no" and any(word in blob for word in _FAIL))
        if not meaningful:
            return RuntimeResult("ignore", "nothing worth reporting")
        found = await tools.call("agents.find", {"capabilities": ["investigate"]})
        names = [row["name"] for row in found.get("agents", []) if row["name"] != agent.name]
        if names:
            specialist = names[0]
        else:
            hired = await tools.call(
                "agents.hire",
                {
                    "name": "researcher",
                    "role_title": "Researcher",
                    "job_description": (
                        "Investigates operational failures and verifies likely root causes. "
                        "Specializes in tracing deploy incidents from primary evidence."
                    ),
                },
            )
            specialist = hired["name"]
        summary = payload.get("message") or payload.get("service") or "incoming signal"
        room = await tools.call(
            "rooms.create",
            {
                "objective": f"Investigate {summary}",
                "participants": [agent.name, specialist],
                "lifecycle": "task",
            },
        )
        task = await tools.call(
            "tasks.create",
            {
                "room": room["room"],
                "owner": specialist,
                "objective": str(summary),
            },
        )
        await tools.call(
            "messages.send",
            {
                "room": room["room"],
                "message": f"Assigned to {specialist}. Kanban {task['hermes_task_id']}.",
            },
        )
        return RuntimeResult("delegated", f"room {room['room']}")

    async def _investigate(
        self,
        tools: Toolbelt,
        team: TeamService,
        agent: Agent,
        payload: dict,
    ) -> RuntimeResult:
        room_id = payload.get("room_id")
        task_id = payload.get("task_id")
        if not task_id:
            refs = await team.list_tasks(room_id)
            task_id = refs[-1].id if refs else None
        if task_id:
            await tools.call("tasks.update", {"task": task_id, "status": "done"})
        if room_id:
            coordinator = await _room_coordinator(team, room_id, agent)
            await tools.call(
                "messages.send",
                {
                    "room": room_id,
                    "message": (
                        f"@{coordinator} investigation done. "
                        "Root cause looks like a bad migrate. Rollback is the move."
                    ),
                    "mentions": [coordinator],
                },
            )
        return RuntimeResult("done", "investigation complete")

    async def _wrap_up(
        self,
        tools: Toolbelt,
        team: TeamService,
        agent: Agent,
        payload: dict,
    ) -> RuntimeResult:
        room_id = payload.get("room_id")
        if not room_id:
            return RuntimeResult("ignore", "no room")
        result = "Investigation complete. Rollback recommended."
        await tools.call("rooms.resolve", {"room": room_id, "result": result})
        you = await team.get_agent("you")
        dm = await team.get_or_create_dm(you, agent)
        await tools.call(
            "messages.send",
            {"room": dm.id, "message": result},
        )
        return RuntimeResult("resolved", result)


def _infer_source(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    if "feed" in words:
        index = words.index("feed")
        if index:
            return words[index - 1]
    if "monitor" in words:
        index = words.index("monitor")
        for word in words[index + 1 :]:
            if word not in _STOP:
                return word
    return "feed"


def _has_capability(agent: Agent, name: str) -> bool:
    have = {item.lower() for item in json.loads(agent.capabilities_json or "[]")}
    return name.lower() in have


async def _room_coordinator(team: TeamService, room_id: str, specialist: Agent) -> str:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models import Membership

    members = list(
        (
            await team.db.execute(
                select(Membership)
                .options(selectinload(Membership.agent))
                .where(Membership.room_id == room_id)
            )
        ).scalars()
    )
    for membership in members:
        if membership.role == "owner" and membership.agent_id != specialist.id:
            return membership.agent.name
        if membership.agent.name != specialist.name and membership.agent.kind != "human":
            name = membership.agent.name
            if _has_capability(membership.agent, "coordinate"):
                return name
    return "chief"
