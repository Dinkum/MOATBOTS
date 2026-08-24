from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from app.config import Settings
from app.models import Agent, Run, WakeEvent
from app.runtime.base import Runtime, RuntimeResult
from app.services.auth import AuthService
from app.services.team import TeamService

SESSION_MARK = "session_id:"


class HermesRuntime(Runtime):
    """Start or resume one Hermes profile. We do not run the agent loop ourselves."""

    def __init__(self, settings: Settings, auth: AuthService) -> None:
        self.settings = settings
        self.auth = auth

    async def run(
        self,
        team: TeamService,
        agent: Agent,
        event: WakeEvent,
        run: Run,
    ) -> RuntimeResult:
        binary = Path(self.settings.hermes_binary)
        if not binary.is_file():
            return RuntimeResult(
                "error",
                "The agent computer adapter is missing. Run ./install.sh",
            )
        home = Path(self.settings.hermes_home).expanduser().resolve()
        root = Path.cwd().resolve()
        if not self.settings.allow_host_hermes and not str(home).startswith(str(root)):
            return RuntimeResult(
                "error",
                "Agent state is outside this repo. Use .moatbots-agents/",
            )
        if not agent.hermes_profile:
            return RuntimeResult("ignore", "agent has no Hermes profile")
        context = await team.load_context(agent)
        # Loading context marks delivered notifications. Commit those writes before
        # Hermes starts so its HTTP tool calls are never blocked by this session.
        await team.db.commit()
        preferred = next(
            (
                spec.provider
                for spec in self.settings.agents
                if spec.name == agent.name and spec.provider != "auto"
            ),
            None,
        )
        provider, model, extra_env = self.auth.hermes_invocation(preferred)
        prompt = _wake_prompt(agent, event, context)
        command = [
            str(binary.resolve()),
            "-p",
            agent.hermes_profile,
            "chat",
            "--query",
            prompt,
            "--quiet",
            "--source",
            "moatbots",
            "--max-turns",
            str(run.max_turns),
            "--provider",
            provider,
            "--model",
            model,
        ]
        if agent.hermes_session_id:
            command.extend(["--resume", agent.hermes_session_id])
        environment = os.environ.copy()
        home = str(Path(self.settings.hermes_home).expanduser())
        environment["HERMES_HOME"] = home
        environment["MOATBOTS_ACTOR"] = agent.name
        environment["MOATBOTS_RUN_ID"] = run.id
        environment.update(extra_env)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=Path(self.settings.agent_workspace).expanduser().resolve(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.settings.hermes_timeout_seconds,
            )
        except TimeoutError:
            process.terminate()
            await process.wait()
            return RuntimeResult("error", "Hermes exceeded the run timeout")
        text = stdout.decode("utf-8", errors="replace").strip()
        diagnostic = stderr.decode("utf-8", errors="replace")
        session_id = _session_id(diagnostic) or _session_id(text) or agent.hermes_session_id
        if session_id:
            agent.hermes_session_id = session_id
        if process.returncode != 0:
            return RuntimeResult("error", (diagnostic or text)[-800:])
        return RuntimeResult("acted", text[-800:] or "hermes returned", turns=run.max_turns)


def _session_id(diagnostic: str) -> str | None:
    for line in diagnostic.splitlines():
        lower = line.lower().strip()
        if lower.startswith(SESSION_MARK) or lower.startswith("session:"):
            return line.split(":", 1)[1].strip().split()[0]
    return None


def _wake_prompt(agent: Agent, event: WakeEvent, context: dict) -> str:
    payload = json.loads(event.payload_json or "{}")
    round_instruction = ""
    if event.reason == "group_round":
        round_instruction = (
            "\nThis is a private group-chat round. Consider only Payload.messages as the new "
            "snapshot for this turn. Other recent room messages may be concurrent responses from "
            "the same round. Do not answer those yet. Send at most one useful response to this "
            "group; sending no group message means pass.\n"
        )
    return (
        f"{round_instruction}"
        f"Current teammate: {agent.name}.\n"
        f"Wake reason: {event.reason}\n"
        f"Context ref: {event.context_reference}\n"
        f"Payload: {json.dumps(payload, default=str)}\n\n"
        f"Office state:\n{json.dumps(context, default=str)[:6000]}\n\n"
        "Act or stay silent. Then stop."
    )
