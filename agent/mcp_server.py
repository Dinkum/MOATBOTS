"""stdio MCP bridge from an isolated Hermes computer to the host office API."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROTOCOL = "2024-11-05"


def _api() -> str:
    return os.environ.get("MOATBOTS_API_URL", "http://host.docker.internal:43127").rstrip("/")


def _headers() -> dict[str, str]:
    token = os.environ.get("MOATBOTS_AGENT_TOKEN", "")
    actor = os.environ.get("MOATBOTS_ACTOR", "")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Moatbots-Actor": actor,
        "Content-Type": "application/json",
    }
    run_id = os.environ.get("MOATBOTS_RUN_ID")
    if run_id and not run_id.startswith("${"):
        headers["X-Moatbots-Run"] = run_id
    return headers


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(f"{_api()}{path}", data=body, headers=_headers(), method=method)
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed operator URL
        return json.loads(response.read().decode("utf-8"))


def _result(req_id: Any, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def _error(req_id: Any, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": message}}


def handle(message: dict) -> dict | None:
    method = message.get("method")
    req_id = message.get("id")
    if method == "notifications/initialized" or req_id is None:
        return None
    if method == "initialize":
        return _result(
            req_id,
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "moatbots-team", "version": "0.1.0"},
            },
        )
    try:
        if method == "tools/list":
            return _result(req_id, {"tools": _request("GET", "/api/tools")["tools"]})
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            payload = _request("POST", f"/api/tools/{name}", {"arguments": arguments})
            return _result(
                req_id,
                {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]},
            )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        return _error(req_id, detail)
    except (URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
        return _error(req_id, str(exc))
    if method == "ping":
        return _result(req_id, {})
    return _error(req_id, f"unknown method {method}")


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
