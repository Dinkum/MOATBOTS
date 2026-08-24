from __future__ import annotations

import json
import re
from typing import Literal

Verdict = Literal["yes", "no", "ambiguous"]


def _blob(payload: object) -> str:
    if isinstance(payload, str):
        return payload.lower()
    return json.dumps(payload, default=str).lower()


def decide(
    payload: object,
    *,
    match_any: list[str] | None = None,
    ignore_any: list[str] | None = None,
) -> Verdict:
    """Deterministic first pass for a feed event.

    Keyword hits against ignore_any win. match_any hits are a yes.
    An empty match list means "wake and let the agent decide."
    Mixed or unclear text is ambiguous. The agent is allowed to stay silent.
    """
    text = _blob(payload)
    ignore = [item.lower() for item in (ignore_any or []) if item]
    match = [item.lower() for item in (match_any or []) if item]

    ignored = [item for item in ignore if item and item in text]
    matched = [item for item in match if item and item in text]

    if ignored and not matched:
        return "no"
    if matched and not ignored:
        return "yes"
    if matched and ignored:
        return "ambiguous"
    if match:
        return "no"
    return "ambiguous"


def parse_list(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded]


_MENTION = re.compile(r"@([A-Za-z0-9_-]+)")


def mention_names(body: str) -> list[str]:
    return list(dict.fromkeys(_MENTION.findall(body)))
