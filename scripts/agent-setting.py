from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.services.agent_auth import scoped_agent_token  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("setting name required")
    settings = Settings()
    name = sys.argv[1]
    paths = {
        "state_dir": settings.agent_state_dir,
        "shared_dir": settings.agent_shared_dir,
        "auth_dir": settings.agent_auth_dir,
        "desktop_dir": settings.agent_desktop_dir,
        "workspace": settings.agent_workspace,
    }
    if name in paths:
        print(Path(paths[name]).expanduser().resolve())
        return
    if name == "image":
        print(settings.agent_image)
        return
    if name == "container_name":
        print(settings.agent_container_name)
        return
    if name == "desktop_host":
        print(settings.agent_desktop_host)
        return
    if name == "desktop_port":
        print(settings.agent_desktop_port)
        return
    if name == "api_url":
        print(settings.agent_api_url)
        return
    if name == "token" and len(sys.argv) == 3:
        print(scoped_agent_token(settings.api_token, sys.argv[2]))
        return
    raise SystemExit(f"unknown setting {name}")


if __name__ == "__main__":
    main()
