# syntax=docker/dockerfile:1.7
# Build context: repository root.

# Base images are pinned by digest (resolved 2026-10-12); bump tag and digest together.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# constraints.txt pins every transitive dependency, so a later build resolves the same set.
COPY frontend/requirements.txt frontend/constraints.txt /tmp/
RUN pip install -r /tmp/requirements.txt -c /tmp/constraints.txt

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
RUN useradd --create-home --uid 10001 app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY frontend/ /app/
COPY data/sample_records.csv data/sample_records_b.csv data/sample_records_invalid.csv data/sample_records.json /app/samples/
USER app
EXPOSE 8501
HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=2)"
# Other settings live in .streamlit/config.toml so local runs behave the same.
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
