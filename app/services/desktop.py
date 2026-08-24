from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import Settings
from app.errors import TeamError


class DesktopService:
    """Start and locate the one persistent computer shared by the team."""

    def __init__(self, settings: Settings, root: Path | None = None) -> None:
        self.settings = settings
        self.root = (root or Path.cwd()).resolve()
        self.script = self.root / "scripts" / "agent-computer.sh"
        self._lock = asyncio.Lock()

    @property
    def url(self) -> str:
        return f"http://{self.settings.agent_desktop_host}:{self.settings.agent_desktop_port}/"

    async def start(self) -> None:
        if not self.script.is_file():
            raise TeamError("The shared agent computer is not installed")
        async with self._lock:
            process = await asyncio.create_subprocess_exec(
                str(self.script),
                "start",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.root,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
            if process.returncode != 0:
                detail = (stderr or stdout).decode("utf-8", errors="replace")
                raise TeamError(f"The shared agent computer did not start: {detail[-400:]}")
