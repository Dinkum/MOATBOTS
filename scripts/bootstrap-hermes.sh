#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

root="$(pwd)"
hermes="${MOATBOTS_HERMES_BINARY:-$root/scripts/hermes-docker.sh}"

if [[ ! -x "$hermes" ]]; then
  printf 'Agent computer adapter missing at %s\n' "$hermes" >&2
  exit 1
fi

if ! "$hermes" profile show chief >/dev/null 2>&1; then
  "$hermes" profile create chief \
    --description "Chief of Staff. Runs the organization and makes durable hires." --yes 2>/dev/null \
    || "$hermes" profile create chief \
      --description "Chief of Staff. Runs the organization and makes durable hires."
fi

"$root/scripts/install-mcp.sh" chief
