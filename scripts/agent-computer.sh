#!/usr/bin/env bash
# Own the single persistent Ubuntu XFCE computer shared by all teammates.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$root/.venv/bin/python"
[[ -x "$python" ]] || { printf 'Moatbots host environment is missing. Run ./install.sh\n' >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { printf 'Docker is required for the agent computer.\n' >&2; exit 1; }

setting() { "$python" "$root/scripts/agent-setting.py" "$@"; }
state_root="$(setting state_dir)"
shared_root="$(setting shared_dir)"
auth_root="$(setting auth_dir)"
desktop_root="$(setting desktop_dir)"
workspace="$(setting workspace)"
image="$(setting image)"
container="$(setting container_name)"
desktop_host="$(setting desktop_host)"
desktop_port="$(setting desktop_port)"
computer_spec="2"
workspace_port="43129"

exists() { docker container inspect "$container" >/dev/null 2>&1; }
running() { [[ "$(docker container inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" == true ]]; }

start_workspace_server() {
  if docker exec --user abc "$container" \
      curl -fsS --max-time 2 "http://127.0.0.1:$workspace_port/" >/dev/null 2>&1; then
    return
  fi
  docker exec --user abc "$container" \
    pkill -f "^python3 -m http.server $workspace_port " >/dev/null 2>&1 || true
  docker exec --detach --user abc --workdir /workspace "$container" \
    sh -lc "mkdir -p /config/log && exec python3 -m http.server $workspace_port --bind 127.0.0.1 --directory /workspace >>/config/log/moatbots-workspace-http.log 2>&1"
  for _ in {1..20}; do
    if docker exec --user abc "$container" \
        curl -fsS --max-time 2 "http://127.0.0.1:$workspace_port/" >/dev/null 2>&1; then
      return
    fi
    sleep 0.1
  done
  printf 'Workspace preview server failed to start.\n' >&2
  return 1
}

create() {
  mkdir -p "$state_root" "$shared_root" "$auth_root" "$desktop_root" "$workspace"
  chmod 700 "$state_root" "$shared_root" "$auth_root" "$desktop_root"

  local current_image current_spec desired_image
  desired_image="$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null)" || {
    printf 'Agent computer image is missing. Run ./install.sh\n' >&2
    exit 1
  }
  if exists; then
    current_image="$(docker container inspect -f '{{.Image}}' "$container")"
    current_spec="$(docker container inspect -f '{{index .Config.Labels "io.moatbots.computer-spec"}}' "$container")"
    if [[ "$current_image" != "$desired_image" || "$current_spec" != "$computer_spec" ]]; then
      docker stop "$container" >/dev/null 2>&1 || true
      docker rm "$container" >/dev/null
    fi
  fi
  exists && return

  local uid gid timezone
  uid="$(id -u)"
  gid="$(id -g)"
  timezone="${TZ:-America/New_York}"
  local -a args=(
    create
    --name "$container"
    --label "io.moatbots.computer-spec=$computer_spec"
    --restart unless-stopped
    --cpus 2
    --memory 2304m
    --pids-limit 512
    --shm-size 1g
    --cap-drop NET_RAW
    --add-host host.docker.internal:host-gateway
    --publish "$desktop_host:$desktop_port:3000"
    --mount "type=bind,src=$desktop_root,dst=/config"
    --mount "type=bind,src=$state_root,dst=/agents"
    --mount "type=bind,src=$shared_root,dst=/agent/shared"
    --mount "type=bind,src=$auth_root,dst=/agent/auth,readonly"
    --mount "type=bind,src=$workspace,dst=/workspace"
    --env "PUID=$uid"
    --env "PGID=$gid"
    --env "TZ=$timezone"
    --env "TITLE=Moatbots Computer"
    --env START_DOCKER=false
  )
  if [[ -d "$HOME/.grok" ]]; then
    args+=(--mount "type=bind,src=$HOME/.grok,dst=/config/.grok,readonly")
  fi
  if [[ -d "$HOME/.codex" ]]; then
    args+=(--mount "type=bind,src=$HOME/.codex,dst=/config/.codex,readonly")
  fi
  docker "${args[@]}" "$image" >/dev/null
}

start() {
  create
  running || docker start "$container" >/dev/null
  start_workspace_server
}

case "${1:-status}" in
  start)
    start
    printf 'http://%s:%s/\n' "$desktop_host" "$desktop_port"
    ;;
  stop)
    running && docker stop "$container" >/dev/null
    ;;
  status)
    if running; then
      printf 'running http://%s:%s/\n' "$desktop_host" "$desktop_port"
    elif exists; then
      printf 'stopped\n'
    else
      printf 'missing\n'
    fi
    ;;
  exec)
    actor="${2:-}"
    shift 2 || true
    [[ "$actor" =~ ^[a-z0-9_]{2,32}$ ]] || { printf 'Invalid agent handle: %s\n' "$actor" >&2; exit 1; }
    [[ "$#" -gt 0 ]] || { printf 'Agent command required\n' >&2; exit 1; }
    start
    local_home="$state_root/$actor"
    mkdir -p "$local_home"
    chmod 700 "$local_home"

    exec_args=(
      exec --user abc --workdir /workspace
      --env HOME=/config
      --env "HERMES_HOME=/agents/$actor"
      --env HERMES_KANBAN_DB=/agent/shared/kanban.db
      --env "HERMES_PROFILE=$actor"
      --env "MOATBOTS_ACTOR=$actor"
      --env "MOATBOTS_API_URL=$(setting api_url)"
      --env "MOATBOTS_WORKSPACE_URL=http://127.0.0.1:$workspace_port"
      --env "MOATBOTS_AGENT_TOKEN=$(setting token "$actor")"
      --env XDG_CACHE_HOME=/config/.cache
    )
    for name in MOATBOTS_RUN_ID OPENROUTER_API_KEY XAI_API_KEY; do
      if [[ -n "${!name:-}" ]]; then
        exec_args+=(--env "$name=${!name}")
      fi
    done
    docker "${exec_args[@]}" "$container" "$@"
    ;;
  *)
    printf 'usage: %s {start|stop|status|exec HANDLE COMMAND...}\n' "$0" >&2
    exit 2
    ;;
esac
