from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("agent handle required")
    handle = sys.argv[1]
    settings = Settings()
    profile = Path(settings.agent_state_dir).expanduser().resolve() / handle / "profiles" / handle
    config_path = profile / "config.yaml"
    if not profile.is_dir():
        raise SystemExit(f"Hermes profile does not exist: {handle}")
    loaded = (
        yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if config_path.is_file()
        else {}
    )
    loaded.setdefault("mcp_servers", {})["team"] = {
        "command": "/opt/moatbots/mcp-entry.sh",
        "enabled": True,
        # Hermes intentionally sanitizes the ambient environment for MCP children.
        # Explicit references preserve its protection while forwarding only this bridge's scope.
        "env": {
            "MOATBOTS_API_URL": "${MOATBOTS_API_URL}",
            "MOATBOTS_ACTOR": "${MOATBOTS_ACTOR}",
            "MOATBOTS_AGENT_TOKEN": "${MOATBOTS_AGENT_TOKEN}",
            "MOATBOTS_RUN_ID": "${MOATBOTS_RUN_ID}",
        },
    }
    loaded.setdefault("agent", {})["reasoning_effort"] = settings.inference.reasoning_effort
    skills = loaded.setdefault("skills", {})
    always_load = skills.setdefault("always_load", [])
    if "moatbots-team" not in always_load:
        always_load.append("moatbots-team")
    config_path.write_text(yaml.safe_dump(loaded, sort_keys=False), encoding="utf-8")
    skill_dir = profile / "skills" / "moatbots-team"
    skill_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "share/moatbots-team/SKILL.md", skill_dir / "SKILL.md")
    learning_dir = profile / "skills" / "moatbots-learn-demonstration"
    learning_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        ROOT / "share/moatbots-learn-demonstration/SKILL.md",
        learning_dir / "SKILL.md",
    )


if __name__ == "__main__":
    main()
