"""Initial schema: users, records, telemetry, per-minute statistics, system logs, idempotency keys, settings.

Revision ID: 0001
Revises:
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATETIME6 = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql", "mariadb")
ROLE = sa.Enum("admin", "user", "viewer", name="user_role")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("role", ROLE, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        # Part of every token; a logout increments it, so all of that user's tokens stop working.
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", DATETIME6, nullable=False),
        sa.Column("updated_at", DATETIME6, nullable=False),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_table(
        "records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("server", sa.String(20), nullable=False),
        sa.Column("recorded_at", DATETIME6, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("created_at", DATETIME6, nullable=False),
        sa.Column("updated_at", DATETIME6, nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_records_owner_id_users", ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_records_recorded_at", "records", ["recorded_at"])
    op.create_index("ix_records_owner_id", "records", ["owner_id"])
    op.create_index("ix_records_category_recorded_at", "records", ["category", "recorded_at"])
    op.create_index("ix_records_server", "records", ["server"])

    op.create_table(
        "realtime_readings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("server", sa.String(20), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("sensor", sa.String(20), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.Column("threshold", sa.Double(), nullable=False),
        sa.Column("is_anomaly", sa.Boolean(), nullable=False),
        sa.Column("generated_at", DATETIME6, nullable=False),
        sa.Column("persisted_at", DATETIME6, nullable=False),
    )
    op.create_index("ix_realtime_readings_generated_at", "realtime_readings", ["generated_at"])
    op.create_index(
        "ix_realtime_readings_category_generated_at", "realtime_readings", ["category", "generated_at"]
    )
    # InnoDB adds the primary key to every index, so this is (is_anomaly, generated_at, id):
    # the alert history, newest time first, reads it backwards instead of scanning the table.
    op.create_index("ix_realtime_readings_is_anomaly_generated_at", "realtime_readings", ["is_anomaly", "generated_at"])
    # One reading per server, time and sensor, so a retried batch cannot store a row twice.
    # Its leading columns also serve the server and time history queries.
    op.create_index(
        "ux_realtime_readings_server_generated_at_category_sensor",
        "realtime_readings",
        ["server", "generated_at", "category", "sensor"],
        unique=True,
    )

    op.create_table(
        "realtime_minute_stats",
        sa.Column("minute", DATETIME6, primary_key=True),
        sa.Column("category", sa.String(50), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("total", sa.Double(), nullable=False),
        sa.Column("minimum", sa.Double(), nullable=False),
        sa.Column("maximum", sa.Double(), nullable=False),
        sa.Column("anomalies", sa.Integer(), nullable=False),
        sa.Column("applied_contributions", sa.JSON(), nullable=True),
    )

    op.create_table(
        "system_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", DATETIME6, nullable=False),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("logger", sa.String(100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(36)),
        sa.Column("method", sa.String(10)),
        sa.Column("path", sa.String(500)),
        sa.Column("status_code", sa.Integer()),
        sa.Column("duration_ms", sa.Float()),
        sa.Column("user_id", sa.Integer()),
        sa.Column("client_ip", sa.String(45)),
    )
    op.create_index("ix_system_logs_created_at", "system_logs", ["created_at"])
    op.create_index("ix_system_logs_level", "system_logs", ["level"])
    op.create_index("ix_system_logs_status_code", "system_logs", ["status_code"])
    op.create_index("ix_system_logs_user_id", "system_logs", ["user_id"])

    # Stored outcomes of creates and imports sent with an Idempotency-Key header.
    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(100, collation="utf8mb4_bin"), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", DATETIME6, nullable=False),
    )
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])

    # Settings an admin changes at runtime; the migrate step fills them from .env once.
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("warning_percent", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(5), nullable=False),
        sa.Column("time_zone", sa.String(64), nullable=False),
        sa.Column("updated_at", DATETIME6, nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_table(
        "metric_thresholds",
        sa.Column("category", sa.String(50), primary_key=True),
        sa.Column("threshold", sa.Double(), nullable=False),
        sa.Column("updated_at", DATETIME6, nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )

    # Which process runs the telemetry generator: a second worker or instance sees a live
    # holder here and keeps its generator off (app/services/lease.py).
    op.create_table(
        "generator_lease",
        sa.Column("name", sa.String(40), primary_key=True),
        sa.Column("holder", sa.String(80), nullable=False),
        sa.Column("renewed_at", DATETIME6, nullable=False),
    )

    # One alert of one sensor, from its first reading past the limit to its first reading back
    # inside; kept for weeks, after the raw readings are gone (app/services/alerts.py).
    op.create_table(
        "realtime_alerts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("server", sa.String(20), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("sensor", sa.String(20), nullable=False),
        sa.Column("started_at", DATETIME6, nullable=False),
        sa.Column("ended_at", DATETIME6, nullable=True),
        sa.Column("threshold", sa.Double(), nullable=False),
        sa.Column("peak_value", sa.Double(), nullable=False),
        sa.Column("readings", sa.Integer(), nullable=False),
        sa.Column("acknowledged_at", DATETIME6, nullable=True),
        sa.Column("acknowledged_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index(
        "ux_realtime_alerts_server_category_sensor_started_at", "realtime_alerts",
        ["server", "category", "sensor", "started_at"], unique=True,
    )
    op.create_index("ix_realtime_alerts_started_at", "realtime_alerts", ["started_at"])


def downgrade() -> None:
    op.drop_table("realtime_alerts")
    op.drop_table("generator_lease")
    op.drop_table("metric_thresholds")
    op.drop_table("system_settings")
    op.drop_table("idempotency_keys")
    op.drop_table("system_logs")
    op.drop_table("realtime_minute_stats")
    op.drop_table("realtime_readings")
    op.drop_table("records")
    op.drop_table("users")
