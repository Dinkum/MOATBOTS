from __future__ import annotations

import json
from typing import Any


def encode_context(context: dict) -> str:
    return json.dumps(context, default=str, ensure_ascii=False, separators=(",", ":"))


class ContextBudget:
    """Fit complete entries, retaining references when full content cannot fit."""

    def __init__(self, agent: dict, limit: int | None) -> None:
        self.limit = limit
        self.data: dict[str, Any] = {
            "agent": agent,
            "notifications": [],
            "human_requests": [],
            "open_loops": [],
            "tasks": [],
            "routines": [],
            "rooms": [],
            "more_available": False,
        }

    @property
    def remaining(self) -> int:
        return self.limit - len(encode_context(self.data)) if self.limit is not None else 2**63

    def append(self, rows: list, item: dict, reference: dict | None = None) -> bool:
        rows.append(item)
        if self.remaining >= 0:
            return True
        rows.pop()
        self.data["more_available"] = True
        if reference:
            rows.append({**reference, "reference_only": True})
            if self.remaining < 0:
                rows.pop()
        return False
