from __future__ import annotations

import hashlib
import hmac


def scoped_agent_token(master_token: str, actor: str) -> str:
    """Derive a bearer token that can act only as one named teammate."""
    return hmac.new(
        master_token.encode(),
        f"moatbots-agent:{actor}".encode(),
        hashlib.sha256,
    ).hexdigest()


def valid_agent_token(master_token: str, actor: str, supplied: str) -> bool:
    expected = scoped_agent_token(master_token, actor)
    return hmac.compare_digest(expected, supplied)
