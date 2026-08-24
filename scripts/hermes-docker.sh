#!/usr/bin/env bash
# Run one Hermes identity inside the persistent shared Linux desktop.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$root/.venv/bin/python"
[[ -x "$python" ]] || { printf 'Moatbots host environment is missing. Run ./install.sh\n' >&2; exit 1; }

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
[[ "$actor" =~ ^[a-z0-9_]{2,32}$ ]] || { printf 'Invalid agent handle: %s\n' "$actor" >&2; exit 1; }

setting() { "$python" "$root/scripts/agent-setting.py" "$@"; }
state_root="$(setting state_dir)"
auth_root="$(setting auth_dir)"
mkdir -p "$state_root/$actor"
if [[ -f "$auth_root/auth.json" ]]; then
  install -m 600 "$auth_root/auth.json" "$state_root/$actor/auth.json"
fi

set +e
"$root/scripts/agent-computer.sh" exec "$actor" /opt/hermes-venv/bin/hermes "$@"
status=$?
set -e
if [[ "${1:-}" == "auth" && -f "$state_root/$actor/auth.json" ]]; then
  install -m 600 "$state_root/$actor/auth.json" "$auth_root/auth.json"
fi
exit "$status"
