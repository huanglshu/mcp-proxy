#!/usr/bin/env sh

set -eu

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
DB_CONFIG_FILE="${DB_CONFIG_FILE:-$SCRIPT_DIR/db_config_example.json}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

if [ ! -f "$DB_CONFIG_FILE" ]; then
  printf 'DB config file not found: %s\n' "$DB_CONFIG_FILE" >&2
  exit 1
fi

cd "$SCRIPT_DIR"

printf 'Starting mcp-proxy on %s:%s\n' "$HOST" "$PORT"
printf 'MySQL named server config: %s\n' "$DB_CONFIG_FILE"

# Use db_config_example.json locally. Override DB_CONFIG_FILE/HOST/PORT/LOG_LEVEL
# via env if needed.
exec uv run mcp-proxy \
  --host "$HOST" \
  --port "$PORT" \
  --named-server-mysql-config "$DB_CONFIG_FILE" \
  --pass-environment \
  --allow-origin '*' \
  --log-level "$LOG_LEVEL" \
  --stateless
