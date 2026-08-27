from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.clock import utc_iso
from app.config import get_settings
from app.deps import get_team
from app.errors import NotFound, TeamError
from app.models import Demonstration
from app.services.auth import upsert_env
from app.services.portal import build_portal
from app.services.team import TeamService

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["from_json"] = json.loads
templates.env.filters["utc_iso"] = utc_iso


async def _viewer(request: Request, team: TeamService):
    handle = request.cookies.get("moatbots_as", "you")
    try:
        viewer = await team.get_agent(handle)
    except NotFound:
        viewer = await team.get_agent("you")
    if viewer.retired_at is not None:
        return await team.get_agent("you")
    return viewer


async def _ctx(
    request: Request,
    team: TeamService,
    *,
    inbox_items=None,
    viewer=None,
    show_heading: bool = False,
    **extra,
):
    viewer = viewer or await _viewer(request, team)
    if inbox_items is None:
        inbox_items = await team.list_inbox(viewer)
    human_requests = await team.list_human_requests() if viewer.kind == "human" else []
    return {
        "request": request,
        "app_version": request.app.state.version,
        "asset_version": request.app.state.asset_version,
        "inbox_unread": sum(item.unread for item in inbox_items),
        "viewer": viewer,
        "impersonating": viewer.name != "you",
        "show_heading": show_heading,
        "human_requests_pending": len(human_requests),
        **extra,
    }


def _room_label(room: Any, human_id: str) -> str:
    if room.type == "channel":
        return f"#{room.title}"
    if room.type == "group":
        peers = [
            f"@{member.agent.name}" for member in room.memberships if member.agent_id != human_id
        ]
        return ", ".join(sorted(peers)) or room.title
    if room.type == "task":
        return room.title
    peer = next(
        (member.agent.name for member in room.memberships if member.agent_id != human_id),
        room.title,
    )
    return f"@{peer}"


def _conversation_rows(items, human_id: str) -> list[dict[str, Any]]:
    return [
        {
            "room": item.room,
            "label": _room_label(item.room, human_id),
            "last": item.last_message,
            "unread": item.unread,
        }
        for item in items
    ]


def _org_rows(agents: list[Any], primary_agent: str) -> list[dict[str, Any]]:
    live = {agent.name: agent for agent in agents}
    by_id = {agent.id: agent for agent in agents}
    children: dict[str, list[Any]] = {agent.id: [] for agent in agents}
    for agent in agents:
        if agent.reports_to_id in children:
            children[agent.reports_to_id].append(agent)

    rows: list[dict[str, Any]] = []

    def visit(agent: Any, depth: int) -> None:
        rows.append(
            {
                "name": agent.name,
                "title": agent.role_title,
                "description": agent.job_description,
                "profile": agent.hermes_profile,
                "status": agent.status,
                "prefix": "    " * max(depth - 1, 0) + ("+-- " if depth else ""),
                "description_prefix": "    " * depth + "    ",
            }
        )
        for child in sorted(children[agent.id], key=lambda row: row.name):
            visit(child, depth + 1)

    root = live.get(primary_agent)
    if root:
        visit(root, 0)
    for orphan in sorted(
        (
            agent
            for agent in agents
            if agent.name != primary_agent and agent.reports_to_id not in by_id
        ),
        key=lambda row: row.name,
    ):
        visit(orphan, 1)
    return rows


@router.get("/", response_class=HTMLResponse)
async def inbox(request: Request, team: TeamService = Depends(get_team)):
    viewer = await _viewer(request, team)
    items = await team.list_inbox(viewer)
    new_mode = "new" in request.query_params or "to" in request.query_params or not items
    active_room = None if new_mode else items[0].room
    messages = []
    replies_by_root = {}
    tasks = []
    following = False
    can_manage_channel = False
    if active_room:
        history = await team.history(active_room.id, viewer=viewer)
        messages = [message for message in history if message.parent_message_id is None]
        replies_by_root = {
            message.id: [reply for reply in history if reply.parent_message_id == message.id]
            for message in messages
        }
        tasks = await team.list_tasks(active_room.id)
        await team.mark_room_read(viewer, active_room.id, history[-1].id if history else None)
        membership = next(
            (member for member in active_room.memberships if member.agent_id == viewer.id), None
        )
        following = membership.notification_level == "all" if membership else False
        can_manage_channel = active_room.type == "channel" and (
            viewer.name == "you" or (membership is not None and membership.role == "owner")
        )
        items = await team.list_inbox(viewer)
    conversations = _conversation_rows(items, viewer.id)
    teammates = [agent for agent in await team.list_agents() if agent.id != viewer.id]
    selected = request.query_params.get("to", request.app.state.settings.primary_agent)
    return templates.TemplateResponse(
        "inbox.html",
        await _ctx(
            request,
            team,
            viewer=viewer,
            inbox_items=items,
            conversations=conversations,
            teammates=teammates,
            selected=selected,
            new_mode=new_mode,
            active_room=active_room,
            room=active_room,
            room_label=_room_label(active_room, viewer.id) if active_room else "",
            messages=messages,
            replies_by_root=replies_by_root,
            tasks=tasks,
            following=following,
            can_manage_channel=can_manage_channel,
            viewer_id=viewer.id,
            section="inbox",
        ),
    )


@router.post("/handoff")
async def start_handoff(request: Request, team: TeamService = Depends(get_team)):
    form = await request.form()
    body = str(form.get("body") or "").strip()
    lead = str(form.get("agent") or "").strip().removeprefix("@").strip()
    if not lead or not body:
        return RedirectResponse("/?new=1", status_code=303)
    viewer = await _viewer(request, team)
    teammate_names = {agent.name for agent in await team.list_agents() if agent.id != viewer.id}
    if lead not in teammate_names or lead == viewer.name:
        return RedirectResponse("/?new=1", status_code=303)
    other = await team.get_agent(lead)
    room = await team.get_or_create_dm(viewer, other)
    await team.send_message(viewer, room.id, body)
    return RedirectResponse(f"/rooms/{room.id}", status_code=303)


@router.get("/rooms/{room_id}", response_class=HTMLResponse)
async def room_page(room_id: str, request: Request, team: TeamService = Depends(get_team)):
    room = await team.get_room(room_id)
    viewer = await _viewer(request, team)
    messages = await team.history(room_id, viewer=viewer)
    tasks = await team.list_tasks(room_id)
    await team.mark_room_read(viewer, room_id, messages[-1].id if messages else None)
    membership = next((member for member in room.memberships if member.agent_id == viewer.id), None)
    following = membership.notification_level == "all" if membership else False
    can_manage_channel = room.type == "channel" and (
        viewer.name == "you" or (membership is not None and membership.role == "owner")
    )
    inbox_items = await team.list_inbox(viewer)
    top_messages = [message for message in messages if message.parent_message_id is None]
    replies_by_root = {
        message.id: [reply for reply in messages if reply.parent_message_id == message.id]
        for message in top_messages
    }
    return templates.TemplateResponse(
        "room.html",
        await _ctx(
            request,
            team,
            viewer=viewer,
            inbox_items=inbox_items,
            conversations=_conversation_rows(inbox_items, viewer.id),
            new_mode=False,
            active_room=room,
            room=room,
            messages=top_messages,
            replies_by_root=replies_by_root,
            tasks=tasks,
            room_label=_room_label(room, viewer.id),
            following=following,
            can_manage_channel=can_manage_channel,
            viewer_id=viewer.id,
            section=room.title,
        ),
    )


@router.post("/rooms/{room_id}/messages")
async def post_message(room_id: str, request: Request, team: TeamService = Depends(get_team)):
    form = await request.form()
    body = str(form.get("body") or "").strip()
    if body:
        viewer = await _viewer(request, team)
        parent_message_id = str(form.get("parent_message_id") or "").strip() or None
        await team.send_message(viewer, room_id, body, parent_message_id=parent_message_id)
    suffix = f"#thread-{parent_message_id}" if body and parent_message_id else ""
    return RedirectResponse(f"/rooms/{room_id}{suffix}", status_code=303)


@router.post("/messages/{message_id}/reaction")
async def react_to_message(
    message_id: str, request: Request, team: TeamService = Depends(get_team)
):
    form = await request.form()
    value = str(form.get("value") or "")
    room_id, _ = await team.set_message_reaction(await _viewer(request, team), message_id, value)
    return RedirectResponse(f"/rooms/{room_id}", status_code=303)


@router.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request, team: TeamService = Depends(get_team)):
    viewer = await _viewer(request, team)
    query = request.query_params.get("q", "")
    channels = await team.list_channels(query)
    joined = [
        room
        for room in channels
        if any(member.agent_id == viewer.id for member in room.memberships)
    ]
    directory = [room for room in channels if room not in joined]
    group_chats = [
        {"room": item.room, "label": _room_label(item.room, viewer.id)}
        for item in await team.list_inbox(viewer)
        if item.room.type == "group"
    ]
    teammates = [agent for agent in await team.list_agents() if agent.id != viewer.id]
    return templates.TemplateResponse(
        "groups.html",
        await _ctx(
            request,
            team,
            viewer=viewer,
            group_chats=group_chats,
            joined=joined,
            directory=directory,
            teammates=teammates,
            query=query,
            section="groups",
        ),
    )


@router.post("/groups")
async def create_group(request: Request, team: TeamService = Depends(get_team)):
    form = await request.form()
    kind = str(form.get("kind") or "group")
    title = str(form.get("title") or "").strip()
    description = str(form.get("description") or "").strip()
    participants = [str(name) for name in form.getlist("participants")]
    owners = [str(name) for name in form.getlist("owners")]
    viewer = await _viewer(request, team)
    if kind == "channel" and title:
        room = await team.create_channel(viewer, title, description, participants, owners)
        return RedirectResponse(f"/rooms/{room.id}", status_code=303)
    if kind == "group" and participants:
        room = await team.create_group_chat(viewer, participants)
        return RedirectResponse(f"/rooms/{room.id}", status_code=303)
    return RedirectResponse("/groups", status_code=303)


@router.post("/groups/{room_id}/follow")
async def follow_group(room_id: str, request: Request, team: TeamService = Depends(get_team)):
    viewer = await _viewer(request, team)
    await team.join_channel(viewer, room_id)
    return RedirectResponse(f"/rooms/{room_id}", status_code=303)


@router.post("/groups/{room_id}/mute")
async def mute_group(room_id: str, request: Request, team: TeamService = Depends(get_team)):
    await team.set_channel_notifications(await _viewer(request, team), room_id, "mentions")
    return RedirectResponse(f"/rooms/{room_id}", status_code=303)


@router.post("/channels/{room_id}/notifications")
async def channel_notifications(
    room_id: str, request: Request, team: TeamService = Depends(get_team)
):
    form = await request.form()
    level = str(form.get("level") or "mentions")
    await team.set_channel_notifications(await _viewer(request, team), room_id, level)
    return RedirectResponse(f"/rooms/{room_id}", status_code=303)


@router.get("/channels/{room_id}/settings", response_class=HTMLResponse)
async def channel_settings_page(
    room_id: str, request: Request, team: TeamService = Depends(get_team)
):
    viewer = await _viewer(request, team)
    room = await team.get_room(room_id)
    membership = next((item for item in room.memberships if item.agent_id == viewer.id), None)
    if room.type != "channel" or (
        viewer.name != "you" and (membership is None or membership.role != "owner")
    ):
        return RedirectResponse(f"/rooms/{room_id}", status_code=303)
    agents = await team.list_agents()
    owner_ids = {item.agent_id for item in room.memberships if item.role == "owner"}
    return templates.TemplateResponse(
        "channel_settings.html",
        await _ctx(
            request,
            team,
            viewer=viewer,
            room=room,
            agents=agents,
            owner_ids=owner_ids,
            section="groups",
        ),
    )


@router.post("/channels/{room_id}/settings")
async def update_channel_settings(
    room_id: str, request: Request, team: TeamService = Depends(get_team)
):
    form = await request.form()
    await team.update_channel(
        await _viewer(request, team),
        room_id,
        name=str(form.get("name") or ""),
        description=str(form.get("description") or ""),
        owners=[str(name) for name in form.getlist("owners")],
    )
    return RedirectResponse(f"/rooms/{room_id}", status_code=303)


@router.get("/kanban", response_class=HTMLResponse)
async def kanban_page(request: Request, team: TeamService = Depends(get_team)):
    tasks = await team.list_tasks()
    statuses = ["todo", "in_progress", "done", "blocked"]
    statuses.extend(sorted({task.last_status for task in tasks} - set(statuses)))
    columns = [
        {"status": status, "tasks": [task for task in tasks if task.last_status == status]}
        for status in statuses
    ]
    return templates.TemplateResponse(
        "kanban.html",
        await _ctx(request, team, columns=columns, section="kanban"),
    )


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request, team: TeamService = Depends(get_team)):
    activity = await team.list_activity(limit=120)
    return templates.TemplateResponse(
        "activity.html",
        await _ctx(
            request,
            team,
            activity=activity,
            section="Activity",
            show_heading=True,
        ),
    )


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, team: TeamService = Depends(get_team)):
    viewer = await _viewer(request, team)
    query = request.query_params.get("q", "").strip()
    messages = await team.search_messages(viewer, query) if query else []
    rooms = {room.id: room for room in await team.list_rooms()}
    return templates.TemplateResponse(
        "search.html",
        await _ctx(
            request,
            team,
            viewer=viewer,
            query=query,
            messages=messages,
            rooms=rooms,
            section="search",
            show_heading=True,
        ),
    )


@router.get("/requests", response_class=HTMLResponse)
async def requests_page(request: Request, team: TeamService = Depends(get_team)):
    viewer = await _viewer(request, team)
    if viewer.kind != "human":
        return RedirectResponse("/", status_code=303)
    requests = await team.list_human_requests(include_resolved=True)
    return templates.TemplateResponse(
        "requests.html",
        await _ctx(
            request,
            team,
            viewer=viewer,
            human_requests=requests,
            section="requests",
            show_heading=True,
        ),
    )


@router.post("/requests/{request_id}/resolve")
async def resolve_request(request_id: str, request: Request, team: TeamService = Depends(get_team)):
    form = await request.form()
    await team.resolve_human_request(
        await _viewer(request, team), request_id, str(form.get("response") or "")
    )
    return RedirectResponse("/requests", status_code=303)


@router.get("/artifacts/{artifact_id}")
async def artifact_file(artifact_id: str, request: Request, team: TeamService = Depends(get_team)):
    artifact = await team.get_artifact(await _viewer(request, team), artifact_id)
    if artifact.kind == "url":
        return RedirectResponse(artifact.reference, status_code=303)
    path = (team.workspace_dir / artifact.reference).resolve()
    if not path.is_relative_to(team.workspace_dir) or not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file is unavailable")
    return FileResponse(path, media_type=artifact.mime_type, filename=artifact.label)


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request, team: TeamService = Depends(get_team)):
    viewer = await _viewer(request, team)
    query = request.query_params.get("q", "").strip().lower()
    agents = [
        agent
        for agent in await team.list_agents()
        if agent.kind != "human"
        and (
            not query
            or query in agent.name.lower()
            or query in agent.role_title.lower()
            or query in agent.job_description.lower()
        )
    ]
    return templates.TemplateResponse(
        "agents.html",
        await _ctx(
            request,
            team,
            viewer=viewer,
            org_rows=_org_rows(agents, request.app.state.settings.primary_agent),
            query=query,
            section="agents",
        ),
    )


@router.get("/agents/{agent_name}", response_class=HTMLResponse)
async def agent_profile(agent_name: str, request: Request, team: TeamService = Depends(get_team)):
    viewer = await _viewer(request, team)
    agent = await team.get_agent(agent_name)
    manager = await team.get_agent(agent.reports_to_id) if agent.reports_to_id else None
    direct_reports = [row for row in await team.list_agents() if row.reports_to_id == agent.id]
    return templates.TemplateResponse(
        "agent.html",
        await _ctx(
            request,
            team,
            viewer=viewer,
            agent=agent,
            manager=manager,
            direct_reports=direct_reports,
            section="agents",
        ),
    )


@router.get("/computer")
async def shared_computer(request: Request):
    try:
        await request.app.state.desktop.start()
    except TeamError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(request.app.state.desktop.url, status_code=303)


@router.get("/teach", response_class=HTMLResponse)
async def teach_page(request: Request, team: TeamService = Depends(get_team)):
    viewer = await _viewer(request, team)
    agents = [agent for agent in await team.list_agents() if agent.kind != "human"]
    demonstrations = list(
        (
            await team.db.execute(
                select(Demonstration).order_by(Demonstration.created_at.desc()).limit(40)
            )
        ).scalars()
    )
    return templates.TemplateResponse(
        "teach.html",
        await _ctx(
            request,
            team,
            viewer=viewer,
            agents=agents,
            demonstrations=demonstrations,
            section="teach",
            show_heading=True,
        ),
    )


@router.post("/teach/start")
async def start_teach(request: Request, team: TeamService = Depends(get_team)):
    form = await request.form()
    agent = await team.get_agent(str(form.get("agent") or "chief"))
    await request.app.state.desktop.start()
    await request.app.state.demonstrations.start(team, agent, str(form.get("title") or ""))
    return RedirectResponse("/teach", status_code=303)


@router.post("/teach/{demonstration_id}/stop")
async def stop_teach(
    demonstration_id: str, request: Request, team: TeamService = Depends(get_team)
):
    await request.app.state.demonstrations.stop(team, demonstration_id)
    return RedirectResponse("/teach", status_code=303)


@router.post("/agents/{agent_name}/impersonate")
async def impersonate_agent(
    agent_name: str, request: Request, team: TeamService = Depends(get_team)
):
    agent = await team.get_agent(agent_name)
    if agent.kind == "human" or agent.retired_at is not None:
        return RedirectResponse("/agents", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "moatbots_as",
        agent.name,
        httponly=True,
        samesite="strict",
        max_age=60 * 60 * 12,
    )
    return response


@router.post("/impersonation/end")
async def end_impersonation():
    response = RedirectResponse("/agents", status_code=303)
    response.delete_cookie("moatbots_as")
    return response


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, team: TeamService = Depends(get_team)):
    snap = request.app.state.auth.snapshot()
    settings = request.app.state.settings
    portal = request.app.state.portal
    return templates.TemplateResponse(
        "settings.html",
        await _ctx(
            request,
            team,
            auth=snap,
            portal_kind=portal.kind,
            portal_ready=portal.kind == "telegram",
            telegram_chat_id=settings.telegram_chat_id or "",
            section="settings",
        ),
    )


@router.post("/settings/openrouter")
async def settings_openrouter(request: Request):
    form = await request.form()
    key = str(form.get("key") or "").strip()
    if key:
        request.app.state.auth.save_openrouter_key(key)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/login/{method_id}")
async def settings_login(method_id: str, request: Request):
    await request.app.state.auth.start_browser_login(method_id)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/portal")
async def settings_portal(request: Request):
    form = await request.form()
    token = str(form.get("token") or "").strip()
    chat_id = str(form.get("chat_id") or "").strip()
    env_path = Path(".env")
    if token:
        upsert_env(env_path, "MOATBOTS_TELEGRAM_BOT_TOKEN", token)
        os.environ["MOATBOTS_TELEGRAM_BOT_TOKEN"] = token
    if chat_id:
        upsert_env(env_path, "MOATBOTS_TELEGRAM_CHAT_ID", chat_id)
        os.environ["MOATBOTS_TELEGRAM_CHAT_ID"] = chat_id
    get_settings.cache_clear()
    settings = get_settings()
    request.app.state.settings = settings
    await request.app.state.portal.replace(build_portal(settings))
    return RedirectResponse("/settings", status_code=303)
