from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.clock import utc_iso
from app.deps import get_team
from app.errors import NotFound, TeamError
from app.services.team import TeamService

router = APIRouter()


class HookBody(BaseModel):
    payload: dict = Field(default_factory=dict)
    agent: str | None = None
    dedupe_key: str | None = None


class EventBody(BaseModel):
    action: str = ""
    resource: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    agent: str | None = None
    dedupe_key: str | None = None


class OpenRouterBody(BaseModel):
    key: str


class RoomBody(BaseModel):
    objective: str
    participants: list[str]
    lifecycle: str = "task"


def _require_token(request: Request, authorization: str | None) -> None:
    expected = request.app.state.settings.api_token
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="bad token")


@router.get("/health")
async def health(request: Request):
    snap = request.app.state.auth.snapshot()
    inference = snap.method(snap.selected)
    return {
        "ok": True,
        "version": request.app.state.version,
        "hermes": Path(request.app.state.settings.hermes_binary).is_file(),
        "inference": snap.selected,
        "inference_ready": inference.ready,
    }


@router.get("/api/auth")
async def auth_status(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _require_token(request, authorization)
    snap = request.app.state.auth.snapshot()
    return {
        "selected": snap.selected,
        "methods": [
            {
                "id": method.id,
                "label": method.label,
                "kind": method.kind,
                "signed_in": method.signed_in,
                "hermes_ready": method.hermes_ready,
                "detail": method.detail,
                "account": method.account,
            }
            for method in (snap.grok_build, snap.codex, snap.openrouter)
        ],
    }


@router.post("/api/auth/openrouter")
async def save_openrouter(
    body: OpenRouterBody,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _require_token(request, authorization)
    request.app.state.auth.save_openrouter_key(body.key)
    return {"ok": True}


@router.post("/api/auth/{method_id}/login")
async def browser_login(
    method_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _require_token(request, authorization)
    try:
        note = await request.app.state.auth.start_browser_login(method_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "detail": note}


@router.post("/api/hooks/{source}")
async def ingest_hook(
    source: str,
    body: HookBody,
    request: Request,
    team: TeamService = Depends(get_team),
    authorization: str | None = Header(default=None),
):
    _require_token(request, authorization)
    events = await team.ingest_webhook(
        source, body.payload, agent_name=body.agent, dedupe_key=body.dedupe_key
    )
    return {"accepted": len(events), "ids": [event.id for event in events if event]}


@router.post("/api/events/{source}/{event_type}")
async def ingest_typed_event(
    source: str,
    event_type: str,
    body: EventBody,
    request: Request,
    team: TeamService = Depends(get_team),
    authorization: str | None = Header(default=None),
):
    _require_token(request, authorization)
    payload = {
        "event_type": event_type,
        "action": body.action,
        "resource": body.resource,
        "metadata": body.metadata,
    }
    events = await team.ingest_webhook(
        source,
        payload,
        agent_name=body.agent,
        dedupe_key=body.dedupe_key,
    )
    return {"accepted": len(events), "ids": [event.id for event in events if event]}


@router.post("/api/rooms")
async def create_room(
    body: RoomBody,
    request: Request,
    team: TeamService = Depends(get_team),
):
    del request
    you = await team.get_agent("you")
    try:
        room = await team.create_room(
            you, body.objective, body.participants, body.lifecycle, approved=True
        )
    except TeamError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": room.id, "title": room.title}


@router.get("/api/rooms/{room_id}/messages")
async def room_messages(room_id: str, request: Request, team: TeamService = Depends(get_team)):
    handle = request.cookies.get("moatbots_as", "you")
    try:
        viewer = await team.get_agent(handle)
    except NotFound:
        viewer = await team.get_agent("you")
    messages = await team.history(room_id, viewer=viewer)
    return serialize_messages(messages, viewer.id)


def serialize_messages(messages, viewer_id: str) -> dict:
    """Keep polling responses identical to the server-rendered message rows."""
    return {
        "messages": [
            {
                "id": message.id,
                "sender": message.sender.name if message.sender else "",
                "body": message.body,
                "parent_message_id": message.parent_message_id,
                "at": utc_iso(message.created_at),
                "reactions": [
                    {
                        "value": reaction.value,
                        "agent": reaction.agent.name,
                        "mine": reaction.agent_id == viewer_id,
                    }
                    for reaction in sorted(message.reactions, key=lambda row: row.agent.name)
                ],
                "artifacts": [
                    {
                        "id": artifact.id,
                        "label": artifact.label,
                        "mime_type": artifact.mime_type,
                    }
                    for artifact in message.artifacts
                ],
            }
            for message in messages
        ]
    }
