# syntax=docker/dockerfile:1.7
# Build context: repository root.

# ---- builder: resolve dependencies into a virtualenv ----
# Base images are pinned by digest (resolved 2026-10-12); bump tag and digest together.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# constraints.txt pins every transitive dependency, so a later build resolves the same set.
COPY backend/requirements.txt backend/constraints.txt /tmp/
RUN pip install -r /tmp/requirements.txt -c /tmp/constraints.txt

# ---- test: builder + test tools; used by `docker compose --profile test` ----
FROM builder AS test
COPY backend/requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install -r /tmp/requirements-dev.txt -c /tmp/constraints.txt
WORKDIR /app
COPY backend/ /app/
COPY data/ /data/
# test_config.py checks every setting against these two files.
COPY .env.example docker-compose.yml /repo/
CMD ["pytest", "-q"]

# ---- runtime: slim image with only the virtualenv and source ----
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
RUN useradd --create-home --uid 10001 app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
# Only what the API and the migrate step run: no tests or dev requirements.
COPY backend/app/ /app/app/
COPY backend/alembic/ /app/alembic/
COPY backend/alembic.ini /app/
COPY backend/scripts/ /app/scripts/
COPY data/ /data/
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"
# One worker: the WebSocket broadcaster and batch buffers live in this process.
# Migrations run separately, in the Compose `migrate` service (scripts/migrate.sh).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
