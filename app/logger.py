from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)

_log = logging.getLogger("moatbots")
_event_path: Path | None = None


def configure_logging(debug: bool = False) -> None:
    global _event_path
    Path("data").mkdir(mode=0o700, parents=True, exist_ok=True)
    _event_path = Path("data/app.events.jsonl")
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        filename="data/app.log",
        filemode="a",
    )
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    _log.addHandler(console)
    _log.setLevel(level)


def stop_logging() -> None:
    _log.handlers.clear()


def emit(topic: str, event: str, message: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "topic": topic,
        "event": event,
        "message": message,
        "request_id": request_id_context.get(),
        **fields,
    }
    _log.info("%s %s %s", topic, event, message)
    if _event_path is None:
        return
    with _event_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")
