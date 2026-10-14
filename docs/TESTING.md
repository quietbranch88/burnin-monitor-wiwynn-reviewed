# Testing

How to run the automated tests, and a manual checklist that walks every assignment module through the UI. Start the stack first with the [README quick start](../README.md#quick-start); the commands below use the same project name, `burnin-demo`.

## Automated tests

**Backend** (pytest), against a separate MariaDB 11.7 that the `test` profile starts on its own network with temporary storage. It never touches the demo database. The suite runs the Alembic migration down and up first.

```sh
docker compose -p burnin-demo --env-file .env --profile test run --rm --build tests pytest -q --cov
docker compose -p burnin-demo --env-file .env --profile test rm -sf test-db
```

The second line removes the test database; run it also after a failure.

**Frontend** (unittest and Streamlit AppTest, with the API mocked):

```sh
docker compose -p burnin-demo --env-file .env run --rm --no-deps frontend python -m unittest discover -s tests
```

The cookie-script tests need Node and are skipped in the container.

**Lint and types:**

```sh
docker run --rm -v "${PWD}:/r:ro" -w /r ghcr.io/astral-sh/ruff:0.13.0 check --no-cache backend frontend
docker compose -p burnin-demo --env-file .env --profile test run --rm --no-deps tests mypy app tests
```

The first line works in PowerShell and POSIX shells. The frontend type check needs a local Python 3.12 environment: install `frontend/requirements.txt` with `-c frontend/constraints.txt` plus `mypy==2.4.0`, then run `python -m mypy app.py lib views` in `frontend/`.

**What they cover.** The backend tests go through the real HTTP routes, services and database: roles and ownership, every protected route needing a token, logout ending all sessions, paging/filters/sorting, CSV/JSON import and its validation, idempotency keys, analytics and the Excel file (including time zones with negative and +14:00 offsets), the real-time tick, batch writes, retries and retention, the WebSocket stream with deactivated accounts and database outages, and database-error classification. The frontend tests cover session handling, API error display, page state and the live page's data shaping. Neither suite drives a real browser; the checklist below does.

**Load.** The scripts behind the capacity numbers are in [load/](../load/README.md).

## Manual acceptance checklist

Use the [test accounts](../README.md#test-accounts) and a private window per account when you need two at once (tabs in one profile share the login).

| Module | Do | Expect |
| --- | --- | --- |
| Users | On the Log in page, **Register** tab, **Create account**, then sign in with it | Viewer access: no create, import or admin pages |
| Users | Sign in as `viewer`; in Swagger, authorize as `viewer` and call `GET /api/v1/admin/users` | Read-only records; `403` from the API |
| Users | Log out and reload | The login page; the old session does not come back |
| Records | As `engineer`, **Test records → New record**: a unique title, value 60, CPU temp, server `S0017` | The record appears with owner `engineer` |
| Records | Search, filter, sort and page the list | Results and page counts follow the controls |
| Records | Pick it in **Record to edit**, **Edit record**, set 65, **Save changes**; then tick **I want to delete record #…** and click **Delete** | The change persists after a reload; then the record is gone |
| Records | As `engineer`, `PATCH /api/v1/records/{id}` an admin-owned record in Swagger | `403`, record unchanged |
| Import | As `engineer`, **Import** `data/sample_records.json` | 10 records imported |
| Import | Import the same file again in the same session | Nothing stored twice |
| Import | Import `data/sample_records_invalid.csv` | Every bad row listed; no records added from that file |
| Real-time | Open **Live telemetry** and watch for 10 seconds | Values, the fleet grid and alerts update every second |
| Real-time | Open **Reading trends** | Line charts per metric and a bar chart of each metric's worst sensor, as % of its limit |
| Real-time | Click a server cell | Server details with its 50 sensors |
| Real-time | As `admin`, in **Settings** set **GPU temp (°C) · alert above** to 1, click **Save settings**, return to Live telemetry | New GPU readings are flagged as alerts; restore 85 afterwards |
| Alerts | Open **Alert events** (an event is stored when its alert ends, so after the GPU step above, restore the threshold first), select an unacknowledged event, **Acknowledge selected** (as `engineer`) | The row shows who confirmed it and when; as `viewer` the action is absent |
| Analytics | **Analytics**, Test records, All categories, All time | Count and a per-category table with count, total, average, minimum and maximum; trend chart |
| Analytics | Pick one category; under **Filters**, a narrower time range | Count, total, average, minimum and maximum cards for that category; results follow the range |
| Analytics | **Generate Excel report**, then **Download Excel report** | Summary, By category, Trend and Data sheets matching the filters |
| Admin | **Users**: select the registered account, **Manage access**, Role Engineer, **Save access**; then untick **Active** and **Save access** | The account gains create/import; after deactivation its next request is refused |
| Admin | **System logs** | The record, import, settings and user changes above, with who did them |
| Admin | **Database**, then open **Advanced diagnostics** | Connection OK; MariaDB 11.7, pool and writer counters; stored readings grow between refreshes |
| Admin | **Telemetry history**: server `S0017`, **Older**, **Previous** and **Newest** | Stored readings, newest first, paged without overlap |

## Stop, resume and reset

```sh
docker compose -p burnin-demo --env-file .env stop
docker compose -p burnin-demo --env-file .env up -d --wait
```

Data is kept in the `db-data` volume across stop, start and `down`. To start over with a fresh database (this deletes all demo data):

```sh
docker compose -p burnin-demo --env-file .env --profile test down -v
docker compose -p burnin-demo --env-file .env up -d --wait
```

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| Port already allocated | Set `API_PORT` / `FRONTEND_PORT` in `.env` (for example `127.0.0.1:18000`) and run `up -d --wait` again |
| `migrate` exits with an error | `docker compose -p burnin-demo --env-file .env logs migrate`; the API waits for a successful migration by design |
| `db` stays unhealthy | `docker compose -p burnin-demo --env-file .env logs db`; check Docker's disk and memory |
| `all predefined address pools have been fully subnetted` | Docker has no free network range; remove networks you no longer need with `docker network rm` |
| A demo password no longer works | Seeding never overwrites existing users; use an admin password reset, or reset the database |
| Swagger answers `401` after a UI logout | Logout ends every session of that account; authorize again |
| Empty live analytics right after startup | Statistics are per finished minute; wait one minute |
