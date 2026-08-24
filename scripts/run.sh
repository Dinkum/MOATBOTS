#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
if [[ ! -f config.yaml ]]; then
  echo "Run ./install.sh first"
  exit 1
fi
if ! docker image inspect moatbots-agent:local >/dev/null 2>&1; then
  echo "Agent computer image is missing. Run ./install.sh"
  exit 1
fi
uv run alembic upgrade head
./scripts/agent-computer.sh start >/dev/null
./scripts/bootstrap-hermes.sh
uv run python scripts/reconcile-agent-profiles.py
exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
