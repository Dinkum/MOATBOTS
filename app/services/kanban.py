from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class KanbanTask:
    id: str
    title: str
    assignee: str
    body: str
    status: str = "todo"


class KanbanBoard:
    """Kanban is Hermes' durable task board. We only store references."""

    async def create(self, title: str, assignee: str, body: str) -> KanbanTask:
        raise NotImplementedError

    async def update(self, task_id: str, status: str) -> KanbanTask:
        raise NotImplementedError

    async def show(self, task_id: str) -> KanbanTask | None:
        raise NotImplementedError

    async def list_all(self) -> list[KanbanTask]:
        raise NotImplementedError


class MemoryKanban(KanbanBoard):
    """In-memory board for tests. Production uses HermesKanban."""

    def __init__(self) -> None:
        self._tasks: dict[str, KanbanTask] = {}
        self._seq = 0

    async def create(self, title: str, assignee: str, body: str) -> KanbanTask:
        self._seq += 1
        task = KanbanTask(id=f"t_{self._seq:04d}", title=title, assignee=assignee, body=body)
        self._tasks[task.id] = task
        return task

    async def update(self, task_id: str, status: str) -> KanbanTask:
        task = self._tasks[task_id]
        task.status = status
        return task

    async def show(self, task_id: str) -> KanbanTask | None:
        return self._tasks.get(task_id)

    async def list_all(self) -> list[KanbanTask]:
        return list(self._tasks.values())


class HermesKanban(KanbanBoard):
    def __init__(self, binary: str = "hermes", home: str | None = None) -> None:
        self.binary = binary
        self.home = Path(home).expanduser() if home else None

    async def create(self, title: str, assignee: str, body: str) -> KanbanTask:
        command = [
            "kanban",
            "create",
            title,
            "--body",
            body,
            "--created-by",
            "moatbots",
            "--json",
        ]
        if assignee:
            command.extend(["--assignee", assignee])
        raw = await self._run(*command)
        payload = _first_json(raw)
        task_id = str(payload.get("id") or payload.get("task_id") or payload.get("task"))
        status = str(payload.get("status") or "todo")
        return KanbanTask(id=task_id, title=title, assignee=assignee, body=body, status=status)

    async def update(self, task_id: str, status: str) -> KanbanTask:
        verb = {
            "done": "complete",
            "complete": "complete",
            "blocked": "block",
            "block": "block",
            "archived": "archive",
            "archive": "archive",
        }.get(status, "")
        if verb == "complete":
            await self._run("kanban", "complete", task_id, "--result", status)
        elif verb == "block":
            await self._run("kanban", "block", task_id, "updated by team runtime")
        elif verb == "archive":
            await self._run("kanban", "archive", task_id)
        else:
            await self._run("kanban", "edit", task_id, "--status", status)
        shown = await self.show(task_id)
        if shown is None:
            return KanbanTask(id=task_id, title="", assignee="", body="", status=status)
        shown.status = status
        return shown

    async def show(self, task_id: str) -> KanbanTask | None:
        raw = await self._run("kanban", "show", task_id, "--json")
        payload = _first_json(raw)
        if not payload:
            return None
        return KanbanTask(
            id=str(payload.get("id") or task_id),
            title=str(payload.get("title") or ""),
            assignee=str(payload.get("assignee") or ""),
            body=str(payload.get("body") or ""),
            status=str(payload.get("status") or ""),
        )

    async def list_all(self) -> list[KanbanTask]:
        raw = await self._run("kanban", "list", "--json")
        payload = _first_json(raw)
        rows = payload if isinstance(payload, list) else payload.get("tasks") or []
        tasks: list[KanbanTask] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            tasks.append(
                KanbanTask(
                    id=str(row.get("id") or ""),
                    title=str(row.get("title") or ""),
                    assignee=str(row.get("assignee") or ""),
                    body=str(row.get("body") or ""),
                    status=str(row.get("status") or ""),
                )
            )
        return tasks

    async def _run(self, *args: str) -> str:
        binary = shutil.which(self.binary) or self.binary
        env = None
        if self.home is not None:
            import os

            env = os.environ.copy()
            env["HERMES_HOME"] = str(self.home)
        process = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace") or "hermes kanban failed")
        return stdout.decode("utf-8", errors="replace")


def _first_json(raw: str) -> dict | list:
    text = raw.strip()
    if not text:
        return {}
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict | list):
            return loaded
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, dict | list):
                return loaded
    return {}
