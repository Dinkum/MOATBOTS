from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.services.agent_auth import scoped_agent_token  # noqa: E402


def main() -> None:
    settings = Settings()
    actor = sys.argv[1]
    paths = (
        settings.agent_state_dir,
        settings.agent_shared_dir,
        settings.agent_auth_dir,
        settings.agent_desktop_dir,
        settings.agent_workspace,
    )
    values = [str(Path(path).expanduser().resolve()) for path in paths]
    values.extend(
        (
            settings.agent_image,
            settings.agent_container_name,
            settings.agent_desktop_host,
            str(settings.agent_desktop_port),
            settings.agent_api_url,
            scoped_agent_token(settings.api_token, actor) if actor else "",
        )
    )
    if any("\0" in value for value in values):
        raise SystemExit("Agent computer settings cannot contain NUL bytes")
    sys.stdout.buffer.write(b"\0".join(os.fsencode(value) for value in values) + b"\0")


if __name__ == "__main__":
    main()
