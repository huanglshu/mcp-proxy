#!/usr/bin/env sh

set -eu

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/config_example.json}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

if [ ! -f "$CONFIG_FILE" ]; then
  printf 'Config file not found: %s\n' "$CONFIG_FILE" >&2
  exit 1
fi

cd "$SCRIPT_DIR"

printf 'Starting mcp-proxy on %s:%s\n' "$HOST" "$PORT"
printf 'Named server config: %s\n' "$CONFIG_FILE"

# Align with k8s deployment args; use config_example.json locally.
# Override HOST/PORT/LOG_LEVEL/CONFIG_FILE via env if needed.
exec uv run mcp-proxy \
  --host "$HOST" \
  --port "$PORT" \
  --named-server-config "$CONFIG_FILE" \
  --pass-environment \
  --allow-origin '*' \
  --log-level "$LOG_LEVEL" \
  --stateless
