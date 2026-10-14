# Load scripts

Scripts for repeating the workload described in [docs/DESIGN.md](../docs/DESIGN.md#capacity). That document's results came from an earlier revision; raw benchmark artifacts are not included. Run from the repository root with the stack up (`docker compose -p burnin-demo --env-file .env up -d --wait`).

| Script | Measures | Run |
| --- | --- | --- |
| `fleet.sh` | Configured reading rate, measured writer-counter delta, drops and final pending count | `sh load/fleet.sh 200` (POSIX shell; Git Bash or WSL on Windows) |
| `api.js` | REST latency at a fixed request rate | `docker run --rm --add-host=host.docker.internal:host-gateway -v "$PWD/load:/s" -e API_URL -e VIEWER_PASSWORD -e RATE=200 grafana/k6:1.3.0 run /s/api.js` |
| `ws.js` | Messages per WebSocket client | `docker run --rm --add-host=host.docker.internal:host-gateway -v "$PWD/load:/s" -e API_URL -e VIEWER_PASSWORD -e CLIENTS=20 grafana/k6:1.3.0 run /s/ws.js` |
| `login.js` | Logins per second (bcrypt cost 12) | `docker run --rm --add-host=host.docker.internal:host-gateway -v "$PWD/load:/s" -e API_URL -e VIEWER_PASSWORD -e RATE=10 grafana/k6:1.3.0 run /s/login.js` |

- The scripts use the demo accounts; export `VIEWER_PASSWORD` or `ADMIN_PASSWORD` if you changed them.
- Export `API_URL` if the API is not on port 8000. The k6 container reaches the host as `host.docker.internal`, not `localhost`.
- `fleet.sh` uses the project name `burnin-demo` and `.env` (override with `COMPOSE_PROJECT` and `ENV_FILE`). It restarts the API with another fleet size; `docker compose -p burnin-demo --env-file .env up -d --wait api` restores it.
- For the capacity runs in DESIGN.md, also set `REALTIME_RETENTION_HOURS=0.05`, so the purge runs while readings are written.

`fleet.sh` prints `generated`, but that value comes from configuration, not an actual tick counter. Its `stored` value is the writer-counter delta over 60 seconds; it is not an independent persisted-row audit. The current `waiting` counter includes queued and in-flight items. k6 checks are recorded by the scripts, but no thresholds are configured to turn every failed check into a failing process exit.

For a changed host port, use `API_URL=http://localhost:18000 sh load/fleet.sh 200` for the host shell. For k6, set `API_URL=http://host.docker.internal:18000` before its Docker command. Do not pass a host-side `localhost` URL into the k6 container.
