"""Source checks for the current query style."""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SOURCES = [p for d in ("app", "alembic") for p in (BACKEND / d).rglob("*.py")]

RAW_SQL = [
    re.compile(r"\btext\("),
    re.compile(r"\bop\.execute\("),
    re.compile(r"exec_driver_sql"),
    re.compile(r"\.execute\(\s*[\"']"),
    # Upper-case keywords at the start of a string literal, e.g. "SELECT ...".
    re.compile(r"[\"'](SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|SHOW|TRUNCATE)\s"),
]


def test_no_raw_sql_in_application_or_migrations():
    assert SOURCES, "no source files found"
    hits = [
        f"{path.relative_to(BACKEND)}:{n}: {line.strip()}"
        for path in SOURCES
        for n, line in enumerate(path.read_text().splitlines(), start=1)
        if any(p.search(line) for p in RAW_SQL)
    ]
    assert hits == []


def test_no_sql_files_shipped():
    assert list(BACKEND.rglob("*.sql")) == []
