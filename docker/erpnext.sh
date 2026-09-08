#!/usr/bin/env bash
# Start/stop the local ERPNext demo stack (frappe_docker clone kept pristine:
# all local tweaks live in this dir as an external compose override).
# Uses a project-local Docker config that skips the macOS keychain
# (credsStore) which fails non-interactively with "Keychain Error (-67674)".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "$SCRIPT_DIR/../../frappe_docker" && pwd)"
OVERRIDE="$SCRIPT_DIR/pwd.override.yml"

export DOCKER_CONFIG="$SCRIPT_DIR/docker-config"
export DOCKER_HOST="unix://$HOME/.orbstack/run/docker.sock"

cd "$BENCH_DIR"
COMPOSE=(docker compose -f pwd.yml -f "$OVERRIDE")

case "${1:-up}" in
  up)
    "${COMPOSE[@]}" up -d
    echo "ERPNext will be at http://localhost:8082 once site creation finishes."
    ;;
  down)
    "${COMPOSE[@]}" down
    ;;
  logs)
    shift || true
    "${COMPOSE[@]}" logs -f "$@"
    ;;
  ps)
    "${COMPOSE[@]}" ps
    ;;
  *)
    echo "usage: $0 [up|down|logs|ps]" >&2
    exit 1
    ;;
esac
