from __future__ import annotations

import asyncio
import os
from pathlib import Path

from app.errors import TeamError


class ProfileProvisioner:
    async def create(self, handle: str, role_title: str, job_description: str) -> str:
        raise NotImplementedError

    async def describe(self, handle: str, role_title: str, job_description: str) -> None:
        raise NotImplementedError


class UnavailableProfileProvisioner(ProfileProvisioner):
    async def create(self, handle: str, role_title: str, job_description: str) -> str:
        del handle, role_title, job_description
        raise TeamError("Hermes profile provisioning is unavailable")

    async def describe(self, handle: str, role_title: str, job_description: str) -> None:
        del handle, role_title, job_description


class MemoryProfileProvisioner(ProfileProvisioner):
    def __init__(self) -> None:
        self.profiles: dict[str, str] = {}

    async def create(self, handle: str, role_title: str, job_description: str) -> str:
        self.profiles[handle] = _description(role_title, job_description)
        return handle

    async def describe(self, handle: str, role_title: str, job_description: str) -> None:
        self.profiles[handle] = _description(role_title, job_description)


class HermesProfileProvisioner(ProfileProvisioner):
    """Provision durable profiles inside isolated Hermes agent computers."""

    def __init__(
        self,
        binary: str,
        home: str,
        root: Path | None = None,
        state_root: str | None = None,
    ) -> None:
        self.binary = Path(binary).expanduser().resolve()
        self.home = Path(home).expanduser().resolve()
        self.state_root = (
            Path(state_root).expanduser().resolve() if state_root else self.home.parent
        )
        self.root = (root or Path.cwd()).resolve()

    async def create(self, handle: str, role_title: str, job_description: str) -> str:
        if not self.binary.is_file():
            raise TeamError("The agent computer adapter is not installed")
        description = _description(role_title, job_description)
        shown = await self._run(handle, "profile", "show", handle, check=False)
        if shown[0] != 0:
            created = await self._run(
                handle,
                "profile",
                "create",
                handle,
                "--description",
                description,
                "--yes",
                check=False,
            )
            if created[0] != 0:
                fallback = await self._run(
                    handle, "profile", "create", handle, "--description", description, check=False
                )
                if fallback[0] != 0:
                    raise TeamError(f"Hermes could not create @{handle}: {fallback[2][-400:]}")
        await self._install_team(handle)
        self._write_soul(handle, role_title, job_description)
        return handle

    async def describe(self, handle: str, role_title: str, job_description: str) -> None:
        result = await self._run(
            handle,
            "profile",
            "describe",
            handle,
            "--description",
            _description(role_title, job_description),
            check=False,
        )
        if result[0] != 0:
            # Older pinned Hermes versions lack the noninteractive describe form.
            pass
        self._write_soul(handle, role_title, job_description)

    def _write_soul(self, handle: str, role_title: str, job_description: str) -> None:
        profile = self.state_root / handle / "profiles" / handle
        profile.mkdir(parents=True, exist_ok=True)
        if handle == "chief":
            soul = (self.root / "app" / "prompts" / "chief.md").read_text(encoding="utf-8")
        else:
            title = role_title.strip() or handle
            description = " ".join(job_description.split())
            soul = (
                f"You are @{handle}, a {title}.\n"
                f"Your specialty: {description}\n"
                "Do your specialty work, report blockers, and keep team coordination in Moatbots.\n"
            )
        (profile / "SOUL.md").write_text(soul.rstrip() + "\n", encoding="utf-8")

    async def _install_team(self, handle: str) -> None:
        script = self.root / "scripts" / "install-mcp.sh"
        process = await asyncio.create_subprocess_exec(
            str(script),
            handle,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._environment(handle),
            cwd=self.root,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        if process.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="replace")
            raise TeamError(f"Could not attach team tools to @{handle}: {detail[-400:]}")

    async def _run(self, handle: str, *args: str, check: bool = True) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            str(self.binary),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=self._environment(handle),
            cwd=self.root,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        if check and process.returncode != 0:
            raise TeamError((err or out)[-400:])
        return process.returncode or 0, out, err

    def _environment(self, handle: str) -> dict[str, str]:
        environment = os.environ.copy()
        profile_home = self.state_root / handle
        environment["HERMES_HOME"] = str(profile_home)
        environment["MOATBOTS_HERMES_BINARY"] = str(self.binary)
        environment["MOATBOTS_HERMES_HOME"] = str(profile_home)
        return environment


def _description(role_title: str, job_description: str) -> str:
    return f"{role_title}. {job_description}".strip()
