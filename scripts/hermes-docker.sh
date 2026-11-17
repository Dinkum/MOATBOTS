#!/usr/bin/env bash
# Run one Hermes identity inside the persistent shared Linux desktop.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

actor="${MOATBOTS_ACTOR:-}"
previous=""
for argument in "$@"; do
  if [[ "$previous" == "-p" || "$previous" == "--profile" ]]; then
    actor="$argument"
    break
  fi
  case "$argument" in
    --profile=*) actor="${argument#--profile=}"; break ;;
  esac
  previous="$argument"
done
if [[ -z "$actor" && "${1:-}" == "profile" && -n "${3:-}" ]]; then
  actor="$3"
fi
actor="${actor:-chief}"
exec "$root/scripts/agent-computer.sh" hermes "$actor" "$@"
