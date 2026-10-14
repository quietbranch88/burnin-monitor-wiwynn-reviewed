# Burn-in Monitor

A real-time monitoring system for server burn-in tests: FastAPI (REST and WebSocket), MariaDB 11.7 through the SQLAlchemy async ORM, a multi-page Streamlit frontend, all run with Docker Compose.

**The scenario.** A factory burns in servers before shipping them. Each server reports 50 sensors every second (temperatures, fan speeds, power), as an 8-GPU server's BMC would. A reading past its threshold raises an alert. Test engineers manage and analyse test records; administrators manage accounts, logs and the database. The demo simulates 20 servers so it runs comfortably on a laptop; the design target is one burn-in line of 200 servers, 10,000 readings/s ([how that was derived and measured](docs/DESIGN.md#capacity)). Set `REALTIME_SERVERS=200` in `.env` to run the target size; growing beyond one line is covered in [Scaling, up and out](docs/DESIGN.md#scaling-up-and-out).

## Quick start

Requirements: Docker with Compose v2.20 or later (Docker Desktop 4.22+ on Windows and macOS, Intel or Apple silicon), and ports 8000 and 8501 free.

Get the source and enter the repository:

```sh
git clone https://github.com/quietbranch88/burnin-monitor-wiwynn-reviewed.git
cd burnin-monitor-wiwynn-reviewed
```

Create `.env` from the example (it does not overwrite an existing file), then start:

```powershell
# PowerShell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

```sh
# macOS / Linux
test -f .env || cp .env.example .env
```

Then, in either shell:

```sh
docker compose -p burnin-demo --env-file .env up --build -d --wait
```

The first build takes a few minutes, and on a busy machine MariaDB's first initialisation of a new volume can add a few more; if `--wait` reports `db` unhealthy, wait a minute and run the same command again (the volume keeps what was done). Compose starts MariaDB, runs a one-shot `migrate` step (Alembic schema, demo accounts, 112 sample records; it shows `Exited (0)`), then the API and the frontend.

| What | URL |
| --- | --- |
| App | http://localhost:8501 |
| Swagger UI | http://localhost:8000/docs |
| ReDoc / OpenAPI | http://localhost:8000/redoc / http://localhost:8000/openapi.json |
| Health (with database) | http://localhost:8000/health |

Open http://localhost:8501 and sign in as `admin` (accounts below).

### Test accounts

| Username | Password | Role | Can do |
| --- | --- | --- | --- |
| `admin` | `Admin@12345` | Admin (shown as Administrator) | Everything, including the admin pages |
| `engineer` | `User@12345` | User (shown as Engineer) | Create and import records; edit or delete only their own |
| `viewer` | `Viewer@12345` | Viewer | Read records, live telemetry and analytics |

Self-registration creates a Viewer; an admin assigns other roles. In Swagger, click **Authorize** and enter a username and password (leave `client_id` and `client_secret` empty).

`.env.example` binds both ports to `127.0.0.1` because its passwords and `JWT_SECRET` are public placeholders.

```sh
docker compose -p burnin-demo --env-file .env ps -a        # service states
docker compose -p burnin-demo --env-file .env logs -f api  # API log
docker compose -p burnin-demo --env-file .env stop         # stop, keeping the data
docker compose -p burnin-demo --env-file .env down -v      # remove containers and the database volume
```

## A five-minute tour

1. **Live telemetry** (as `admin`). One tile per metric shows the sensor nearest its limit, a grid has one cell per server, alerts are listed beside it. Click a cell to see that server's 50 sensors; **Reading trends** has the live line and bar charts.
2. **Alert events.** Alerts lasting three or more readings are stored as events (start, end, peak) when they end, and kept for 30 days. Engineers and admins can acknowledge them.
3. **Roles.** As `viewer`, records are read-only and the Import and admin pages are absent. As `engineer`, you can create and import, and change only your own records; the API answers `403` otherwise.
4. **Import and analytics.** As `engineer`, import `data/sample_records.json`, then `data/sample_records_invalid.csv` to see every bad row listed. On **Analytics**, pick a range and category and download the Excel report.
5. **Admin.** Users, system logs, database status, settings, and telemetry history (listed under Monitoring, admin only). Lower a threshold in **Settings**: new readings use it within a second and the change appears in **System logs**.

| Live telemetry | Analytics |
| --- | --- |
| ![Live telemetry](docs/screenshots/live.png) | ![Analytics](docs/screenshots/analytics.png) |

## Assignment checklist

Backend paths are under `backend/app/`, frontend paths under `frontend/`.

| Required | Where to see it | Code |
| --- | --- | --- |
| Users: register, JWT login, roles Admin / User / Viewer, permissions per role | Log in page (Register tab); Swagger `/api/v1/auth/*` | `api/auth.py`, `api/deps.py` (`require_roles`), `services/users.py` |
| Records: create; read with paging, filters, sorting; update and delete by the owner or an admin; CSV and JSON import | Test records and Import pages | `api/records.py`, `services/records.py` (`can_modify`), `services/importer.py` |
| Real-time: a generator every second, WebSocket push, line and bar charts, alerts past a threshold | Live telemetry and Alert events pages | `services/realtime.py`, `services/batch.py`, `api/realtime.py`; `views/realtime.py`, `lib/stream.py` |
| Analytics: total, average, min, max; time range; per category; trend; Excel | Analytics page | `services/analytics.py`, `services/excel.py` |
| Admin: users and permissions, system logs, database status, telemetry history | The admin pages | `api/admin.py`, `services/admin.py`, `services/system_logs.py` |
| FastAPI: JWT, Pydantic validation, Swagger, error handling and logging, async | http://localhost:8000/docs | `core/security.py`, `schemas/`, `main.py` |
| MariaDB 11.7, SQLAlchemy, Alembic, connection pool, asyncmy, batched writes | Database status page | `db/session.py`, `alembic/versions/`, `services/batch.py` |
| Streamlit: login and logout, live charts, multi-page, session state, API client | Every page | `app.py`, `lib/session.py`, `lib/api.py` |
| Docker: multi-stage builds, Compose, container networks, a volume, environment variables | `docker compose -p burnin-demo ps -a` | `backend/Dockerfile`, `frontend/Dockerfile`, `docker-compose.yml`, `.env.example` |
| Deliverables: README, `.env.example`, CSV sample, architecture diagram | This file | `.env.example`, `data/sample_records.csv`, `docs/architecture.png` |

## Architecture

![Architecture](docs/architecture.png)

The browser talks to Streamlit (8501) and, for Swagger, to the API (8000). Streamlit calls FastAPI over REST and keeps one WebSocket per session. Inside the API, a generator creates readings every second, a broadcaster pushes them to clients, and batch writers store them in MariaDB. Only the API and the migrate step are on the database network.

### Tech stack

| Layer | Choice |
| --- | --- |
| API | FastAPI, Pydantic 2, Uvicorn, PyJWT, bcrypt |
| Database | MariaDB 11.7, SQLAlchemy 2.1 async ORM, asyncmy, Alembic |
| Frontend | Streamlit 1.64, pandas, websockets |
| Reports | openpyxl |
| Tests | pytest on a real MariaDB 11.7, Streamlit AppTest, k6 for load |

## Beyond the assignment

Each addition solves a specific problem; the reasoning and measurements are in [DESIGN.md](docs/DESIGN.md).

- **Capacity measured, not estimated.** In 60-second load runs on an Apple M3 laptop (on an earlier revision of this code, not re-run on the final one), one API process kept up with 400 simulated servers (20,000 readings/s) with no dropped readings; the target is 10,000/s. Writes go in 5,000-row chunks, four in parallel. [Capacity](docs/DESIGN.md#capacity)
- **Retries reuse a stored result.** A unique key per reading makes a re-sent batch safe. Record creates and imports reuse the result for the same user, key and body while the key is retained (24 hours before cleanup). [Duplicates](docs/DESIGN.md#retried-batches-must-not-create-duplicates)
- **Defined outage behavior.** Database-dependent REST requests answer `503`, while `/health/live` continues to answer without querying the database. A new WebSocket closes with `1013` (try again later); open connections stay open, generation pauses when the last confirmed lease expires, and failed batches are retried from bounded memory. [Failure handling](docs/DESIGN.md#failure-handling)
- **Runtime settings.** Admins change thresholds, the warning level, the interface language (English or Traditional Chinese) and the plant time zone without a restart; every change is logged.
- **Alert events with acknowledgement.** Alerts are stored as events per server and sensor, outliving the 12-hour raw readings, and engineers and admins can acknowledge them.

## Tests

```sh
docker compose -p burnin-demo --env-file .env --profile test run --rm --build tests pytest -q --cov
docker compose -p burnin-demo --env-file .env --profile test rm -sf test-db
```

The backend suite runs against a separate, disposable MariaDB 11.7 and runs the Alembic migration down and up first. It covers roles and record ownership, paging/filters/sorting, CSV/JSON import, analytics and the Excel file, the real-time pipeline (ticks, batch writes, retries, retention), the WebSocket stream, database-error classification, idempotency and time zones. The frontend suite (Streamlit AppTest with a mocked API) and lint/type checks are in [TESTING.md](docs/TESTING.md), with a manual acceptance checklist per module.

Latest container run (14 October 2026): 392 backend tests passed with 96% reported coverage on MariaDB 11.7; 115 frontend tests collected, 108 passed and 7 cookie-script tests skipped because Node is absent. Ruff and backend mypy passed; the subsequent document review also ran frontend mypy successfully on 19 source files. The exact-clock login edge case remains documented in the Node-dependent tests. A reliability review added 14 regression tests; see [AI_USAGE.md](docs/AI_USAGE.md#a-review-cycle-and-what-it-caught).

## Known limits

- **One API worker.** The broadcaster and write buffers live in process memory; a database lease keeps a second process from generating a second fleet. Scaling out needs a shared broadcast (for example Redis pub/sub). [Scaling](docs/DESIGN.md#scaling-up-and-out)
- **No login rate limit.** In production this belongs at the gateway.
- **The login cookie is not `HttpOnly`.** Streamlit can only set cookies from page script.
- **No table partitioning.** Retention deletes old rows in chunks. Full-size retention still needs load testing before selecting a partitioning or storage strategy.
- **A crash loses buffered readings** (normally under 10 seconds).
- **No automated browser E2E suite.** The UI is covered by AppTest and the manual checklist.

All limits: [DESIGN.md](docs/DESIGN.md#known-limits).

## Sample data and configuration

| File | Contents |
| --- | --- |
| `data/sample_records.csv` | 112 test records, imported on the first start |
| `data/sample_records_b.csv` | 2 more records, to try a multi-file import |
| `data/sample_records.json` | 10 records in the JSON import format |
| `data/sample_records_invalid.csv` | Rows with errors, to see the validation messages |

[.env.example](.env.example) lists every variable Compose passes to the containers; other backend settings and their defaults are in `backend/app/core/config.py`.

## Run locally without Docker

For development, on macOS or Linux (on Windows, use WSL). Requirements: Python 3.12 and a MariaDB 11.7 server. To start one in Docker:

```sh
docker run -d --name burnin-db -p 127.0.0.1:3306:3306 \
  -e MARIADB_ROOT_PASSWORD=root -e MARIADB_DATABASE=monitor \
  -e MARIADB_USER=monitor -e MARIADB_PASSWORD=monitor mariadb:11.7
```

Backend, from `backend/`:

```sh
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt -c constraints.txt
export DATABASE_URL="mariadb+asyncmy://monitor:monitor@127.0.0.1:3306/monitor?charset=utf8mb4"
export JWT_SECRET="local-development-secret-of-32-chars-or-more"
export SEED_SAMPLE_CSV=../data/sample_records.csv
alembic upgrade head
python -m app.seed
uvicorn app.main:app --reload --port 8000
```

Frontend, from `frontend/` in a second terminal:

```sh
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -c constraints.txt
export API_BASE_URL=http://localhost:8000/api/v1 API_WS_URL=ws://localhost:8000/api/v1/ws/realtime
export SAMPLES_DIR=../data
streamlit run app.py
```

Then open http://localhost:8501.

## More documents

- [docs/DESIGN.md](docs/DESIGN.md): design decisions, failure handling, capacity, API errors and limits.
- [docs/TESTING.md](docs/TESTING.md): automated tests, manual acceptance checklist and troubleshooting.
- [docs/AI_USAGE.md](docs/AI_USAGE.md): how I used AI on this project.
- [load/](load/README.md): the load scripts behind the capacity numbers.
