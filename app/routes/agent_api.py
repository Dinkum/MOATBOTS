from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.deps import get_team
from app.errors import TeamError
from app.models import Run
from app.services.agent_auth import valid_agent_token
from app.services.team import TeamService
from app.services.tools import TOOL_NAMES, Toolbelt, tool_schemas

router = APIRouter()


class ToolBody(BaseModel):
    arguments: dict = Field(default_factory=dict)


def _bearer(authorization: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return ""


def _require_agent(request: Request, authorization: str | None, actor: str | None) -> str:
    if not actor or not valid_agent_token(
        request.app.state.settings.api_token,
        actor,
        _bearer(authorization),
    ):
        raise HTTPException(status_code=401, detail="bad agent identity")
    return actor


@router.get("/health")
async def agent_health():
    return {"ok": True, "surface": "agent-tools"}


@router.get("/api/tools")
async def list_tools(
    request: Request,
    authorization: str | None = Header(default=None),
    x_moatbots_actor: str | None = Header(default=None),
):
    _require_agent(request, authorization, x_moatbots_actor)
    return {"tools": tool_schemas()}


@router.post("/api/tools/{name}")
async def call_tool(
    name: str,
    body: ToolBody,
    request: Request,
    team: TeamService = Depends(get_team),
    authorization: str | None = Header(default=None),
    x_moatbots_actor: str | None = Header(default=None),
    x_moatbots_run: str | None = Header(default=None),
):
    actor_name = _require_agent(request, authorization, x_moatbots_actor)
    if name not in TOOL_NAMES:
        raise HTTPException(status_code=404, detail="unknown tool")
    try:
        actor = await team.get_agent(actor_name)
        run = await team.db.get(Run, x_moatbots_run) if x_moatbots_run else None
        if run and (run.agent_id != actor.id or run.status != "running"):
            raise TeamError("Run does not belong to the active agent")
        result = await Toolbelt(team, actor, run).call(name, body.arguments)
    except TeamError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result
