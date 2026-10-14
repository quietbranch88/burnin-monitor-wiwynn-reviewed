#!/bin/sh
# Store rate for a simulated fleet size: restarts only the API with REALTIME_SERVERS=$1,
# waits, then reads the batch writer's counters twice, 60 s apart.
#   sh load/fleet.sh 200           (from the repository root, with the stack running)
# Uses the README's Compose project name and env file; override with COMPOSE_PROJECT and ENV_FILE.
# Afterwards `docker compose -p burnin-demo --env-file .env up -d --wait api` restores the configured fleet.
set -e
SERVERS=${1:?usage: load/fleet.sh SERVERS}
# The API refuses fleets above 2,000 servers at startup (REALTIME_SERVERS, app/core/config.py).
[ "$SERVERS" -ge 1 ] && [ "$SERVERS" -le 2000 ] || { echo "SERVERS must be 1 to 2000" >&2; exit 2; }
PROJECT=${COMPOSE_PROJECT:-burnin-demo}
ENV_FILE=${ENV_FILE:-.env}
trap 'echo "If the API did not come back: docker compose -p \"$PROJECT\" --env-file \"$ENV_FILE\" up -d --wait api" >&2' EXIT
API=${API_URL:-http://localhost:${API_PORT:-8000}}
# Quoted, so a path with spaces in it (an env file under "Application Support") still works.
REALTIME_SERVERS=$SERVERS docker compose -p "$PROJECT" --env-file "$ENV_FILE" up -d --no-deps --wait api >/dev/null
sleep 20
TOKEN=$(curl -s -X POST "$API/api/v1/auth/login" -d "username=admin&password=${ADMIN_PASSWORD:-Admin@12345}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
writer() { curl -s "$API/api/v1/admin/db-status" -H "Authorization: Bearer $TOKEN"; }
RATE=$(curl -s "$API/api/v1/realtime/config" -H "Authorization: Bearer $TOKEN" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["readings_per_second"])')
FIRST=$(writer); sleep 60; LAST=$(writer)
python3 - "$SERVERS" "$RATE" "$FIRST" "$LAST" <<'PY'
import json, sys
servers, rate = int(sys.argv[1]), float(sys.argv[2])
a, b = (json.loads(x)["realtime_writer"] for x in sys.argv[3:])
print(f"servers={servers} generated={rate:,.0f}/s stored={(b['written'] - a['written']) / 60:,.0f}/s "
      f"dropped={b['dropped'] - a['dropped']:,} waiting={b['pending']:,}")
PY
trap - EXIT  # finished normally: no hint needed
