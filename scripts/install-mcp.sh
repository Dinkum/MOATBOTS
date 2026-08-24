#!/usr/bin/env bash
# Attach the container-local Moatbots bridge and teammate skill to Hermes profiles.
set -euo pipefail
cd "$(dirname "$0")/.."

root="$(pwd)"
python="$root/.venv/bin/python"
if [[ ! -x "$python" ]]; then
  echo "Moatbots host environment is missing. Run ./install.sh"
  exit 1
fi

profiles=("$@")
if [[ ${#profiles[@]} -eq 0 ]]; then
  profiles=(chief)
fi

for name in "${profiles[@]}"; do
  [[ "$name" =~ ^[a-z0-9_]{2,32}$ ]] || { printf 'Invalid agent handle: %s\n' "$name" >&2; exit 1; }
  "$python" "$root/scripts/configure-agent-profile.py" "$name"
  printf 'team tools + teammate skill -> %s\n' "$name"
done
