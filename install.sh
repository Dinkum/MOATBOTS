#!/usr/bin/env bash
# Moatbots installer for macOS and Ubuntu/Debian.
# The office runs on the host; Hermes runs in one persistent shared Docker desktop.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_dir"

say() { printf '%s\n' "$*"; }
die() { say "error: $*" >&2; exit 1; }

os="$(uname -s)"
case "$os" in
  Darwin|Linux) ;;
  *) die "install.sh supports macOS and Linux only (found $os)" ;;
esac

if [[ "$os" == Linux && -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}:${ID_LIKE:-}" in
    ubuntu:*|debian:*|*:ubuntu*|*:debian*) ;;
    *) say "warning: this script is tested on Ubuntu/Debian. Continuing anyway." ;;
  esac
fi

need_python() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

if ! need_python; then
  if [[ "$os" == Darwin ]]; then
    die "Python 3.11+ is required. brew install python@3.12"
  fi
  if command -v apt-get >/dev/null 2>&1; then
    say "Installing python3.11 via apt..."
    sudo apt-get update
    sudo apt-get install -y python3.11 python3.11-venv python3.11-dev git || \
      sudo apt-get install -y python3 python3-venv python3-pip git
  fi
  need_python || die "Python 3.11+ is required"
fi

command -v docker >/dev/null 2>&1 || die "Docker is required for agent computers"
docker info >/dev/null 2>&1 || die "Docker is installed but its engine is not running"

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || die "uv is not on PATH. Open a new shell and rerun ./install.sh"

if [[ -d .hermes-home && ! -d .moatbots-agents/chief ]]; then
  mkdir -p .moatbots-agents
  mv .hermes-home .moatbots-agents/chief
fi
if [[ -d .moatbots-home && ! -d .moatbots-agents/chief ]]; then
  mkdir -p .moatbots-agents
  mv .moatbots-home .moatbots-agents/chief
fi
mkdir -p data workspace .moatbots-agents .moatbots-auth .moatbots-desktop .moatbots-kanban
chmod 700 data .moatbots-agents .moatbots-auth .moatbots-desktop .moatbots-kanban
if [[ ! -f .moatbots-auth/auth.json ]]; then
  printf '{}\n' > .moatbots-auth/auth.json
  chmod 600 .moatbots-auth/auth.json
fi

if [[ ! -f config.yaml ]]; then
  cp config.example.yaml config.yaml
  say "Wrote config.yaml"
fi
if [[ ! -f .env ]]; then
  umask 077
  token="$(openssl rand -hex 16 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(16))')"
  printf 'MOATBOTS_API_TOKEN=%s\n' "$token" > .env
  say "Wrote .env with a local API token"
fi

uv sync --locked

say "Building the pinned Hermes agent computer..."
docker compose --profile build build agent-computer
"$repo_dir/scripts/agent-computer.sh" start >/dev/null

import="n"
if [[ "${MOATBOTS_IMPORT_AUTH:-}" == "1" ]]; then
  import="y"
elif [[ -t 0 ]] && { [[ -f "$HOME/.hermes/auth.json" ]] || [[ -f "$HOME/.hermes/.env" ]] || [[ -f "$HOME/.grok/auth.json" ]] || [[ -f "$HOME/.codex/auth.json" ]]; }; then
  say "Found existing Grok / Codex / Hermes logins on this machine."
  read -r -p "Import them into the office Hermes copy? [Y/n] " ans || ans="y"
  case "${ans:-y}" in
    n|N|no|NO) import="n" ;;
    *) import="y" ;;
  esac
fi
if [[ "$import" == "y" ]]; then
  uv run python -m app.import_auth
  say "Grok Build (~/.grok) and Codex (~/.codex) sessions are still read in place."
fi

"$repo_dir/scripts/bootstrap-hermes.sh"

say
say "Moatbots is ready."
say "Office:         macOS/Linux host"
say "Agent computer: moatbots-agent:local"
say "Shared desktop: http://127.0.0.1:43128"
say "Agent state:    .moatbots-agents/"
say "Run:           ./scripts/run.sh"
say "Open:          http://127.0.0.1:8080"
say "Agent tools:   host callback on port 43127"
say "Portal:        settings → telegram, or portal.kind: telegram"
