from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from sqlalchemy import select

from app.errors import NotFound, TeamError
from app.models import Agent, Demonstration
from app.services.team import TeamService


class DemonstrationService:
    """Record the one shared desktop and hand learning back to native Hermes skills."""

    def __init__(self, computer_script: str, workspace: str) -> None:
        self.computer_script = Path(computer_script).expanduser().resolve()
        self.workspace = Path(workspace).expanduser().resolve()

    async def start(self, team: TeamService, agent: Agent, title: str) -> Demonstration:
        active = (
            await team.db.execute(select(Demonstration).where(Demonstration.status == "recording"))
        ).scalar_one_or_none()
        if active:
            raise TeamError("The shared computer is already being recorded")
        clean_title = " ".join(title.split())
        if not clean_title:
            raise TeamError("A demonstration needs a title")
        row = Demonstration(
            agent_id=agent.id,
            title=clean_title[:180],
            recording_path="",
            created_at=team.clock.now(),
        )
        team.db.add(row)
        await team.db.flush()
        row.recording_path = f".moatbots-demonstrations/{row.id}/recording.mp4"
        script = (
            'set -eu; d=/workspace/.moatbots-demonstrations/$1; mkdir -p "$d"; '
            "size=$(xdpyinfo -display :1 | awk '/dimensions:/{print $2; exit}'); "
            'test -n "$size"; '
            'nohup ffmpeg -nostdin -y -f x11grab -framerate 12 -video_size "$size" '
            '-i :1.0 -c:v libx264 -preset ultrafast -crf 24 "$d/recording.mp4" '
            '>"$d/recorder.log" 2>&1 & echo $! >"$d/recorder.pid"'
        )
        await self._run(agent.name, "sh", "-c", script, "record", row.id)
        await team.record(
            "demonstration.start",
            f"Recording {row.title}",
            actor_id=agent.id,
            demonstration_id=row.id,
        )
        return row

    async def stop(self, team: TeamService, demonstration_id: str) -> Demonstration:
        row = await team.db.get(Demonstration, demonstration_id)
        if row is None:
            raise NotFound(f"Unknown demonstration {demonstration_id}")
        if row.status != "recording":
            raise TeamError("Demonstration is not recording")
        agent = await team.get_agent(row.agent_id)
        script = (
            "set -eu; d=/workspace/.moatbots-demonstrations/$1; "
            'test -f "$d/recorder.pid"; pid=$(cat "$d/recorder.pid"); '
            'kill -INT "$pid" 2>/dev/null || true; '
            'i=0; while kill -0 "$pid" 2>/dev/null && [ $i -lt 50 ]; do '
            "sleep .1; i=$((i+1)); done; "
            'mkdir -p "$d/frames"; '
            "ffmpeg -nostdin -y -i \"$d/recording.mp4\" -vf 'fps=1/5,scale=1280:-2' "
            '"$d/frames/frame-%04d.jpg" >"$d/frames.log" 2>&1'
        )
        await self._run(agent.name, "sh", "-c", script, "stop", row.id)
        recording = (self.workspace / row.recording_path).resolve()
        if not recording.is_file():
            raise TeamError("Recorder stopped without producing a video")
        row.sha256 = _sha256(recording)
        row.status = "learning"
        row.stopped_at = team.clock.now()
        await team.enqueue_wake(
            target=agent,
            actor=None,
            reason="demonstration",
            context_reference=row.id,
            payload={
                "demonstration_id": row.id,
                "title": row.title,
                "recording": f"/workspace/{row.recording_path}",
                "frames": f"/workspace/.moatbots-demonstrations/{row.id}/frames",
                "sha256": row.sha256,
                "skills": ["moatbots-learn-demonstration"],
            },
            dedupe_key=f"demonstration:{row.id}",
        )
        await team.record(
            "demonstration.stop",
            f"Learning {row.title}",
            actor_id=agent.id,
            demonstration_id=row.id,
            sha256=row.sha256,
        )
        return row

    async def _run(self, actor: str, *args: str) -> None:
        process = await asyncio.create_subprocess_exec(
            str(self.computer_script),
            "exec",
            actor,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        if process.returncode:
            detail = (stderr or stdout).decode("utf-8", errors="replace")
            raise TeamError(detail[-500:] or "Shared-computer recorder failed")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
