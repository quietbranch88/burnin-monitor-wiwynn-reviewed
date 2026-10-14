from datetime import datetime

import pandas as pd
import streamlit as st
from lib import api, session, ui
from lib.i18n import t

st.title(t("Database status"), anchor=False)
st.caption(t("Database connection and storage overview.") + " · " + t("Refreshed every 15 s"))
ui.tz_caption()
if not session.is_admin():
    st.stop()


def compact(rows: int) -> str:
    """Millions fit a metric card; '≈ 3,429,…' did not."""
    return f"{rows / 1_000_000:.2f} M" if rows >= 1_000_000 else f"{rows:,}"


TABLE_NAMES = {"users": "Users", "records": "Test records", "realtime_minute_stats": "Per-minute statistics",
               "realtime_alerts": "Alert events", "realtime_readings": "Live readings", "system_logs": "System logs"}


@st.fragment(run_every="15s")
def status_panel() -> None:
    try:
        status = api.get("/admin/db-status")
    except api.ApiError as error:
        api.show_error(error)
        return
    top = st.columns(4)
    top[0].metric(t("Connection"), t("OK"), border=True)  # a lost connection is a 503, shown above instead
    top[1].metric(t("Saved test results"), f"{status['table_rows']['records']:,}", border=True)
    top[1].caption(t("Initial samples, manual entries and successful imports."))
    top[2].metric(t("Checked at ({zone})", zone=ui.zone_name()), datetime.now(ui.zone()).strftime("%H:%M:%S"), border=True)

    top[3].metric(t("Fleet generator"), t("running") if status["generator_running"] else t("off"),
                  help=t("Runs in the one API process that holds the generator lease; off while another process holds it "
                         "or the lease cannot be confirmed."), border=True)
    st.caption(t("This is not the database total. Live readings, alerts and system logs are counted separately "
                 "under Advanced diagnostics."))

    if status["skipped_ticks"]:
        st.warning(t("The generator was held up and skipped {count} seconds of readings since the API started.",
                     count=status["skipped_ticks"]), icon=":material/timer_off:")
    with st.expander(t("Advanced diagnostics")):
        pool = status["pool"]
        health = st.columns(2)
        health[0].metric(t("Ping"), f"{status['ping_ms']} ms", help=t("Time for one round trip to the database."), border=True)
        health[1].metric(t("Live viewers"), status["websocket_clients"], help=t("Open live-telemetry connections."), border=True)
        st.subheader(t("Batch writers"), anchor=False,
                     help=t("Readings and log lines are collected in memory and written to the database in batches. "
                            "Waiting should stay small and Dropped at 0; a growing Waiting means the database is behind."))
        writers = pd.DataFrame([{"writer": t(name), **status[key]} for name, key in (
            ("realtime readings", "realtime_writer"), ("per-minute statistics", "minute_writer"), ("alert events", "alert_writer"),
            ("system logs", "log_writer"))])
        if "in_flight" not in writers:
            writers["in_flight"] = 0
        if "queued" not in writers:
            writers["queued"] = writers["pending"] - writers["in_flight"]
        writers["last_flush_at"] = ui.to_display(writers["last_flush_at"])
        writers["last_error"] = writers["last_error"].fillna("—")
        st.dataframe(
            writers[["writer", "pending", "queued", "in_flight", "written", "dropped", "backoff_seconds", "last_flush_at", "last_error"]],
            hide_index=True,
            width="stretch",
            column_config={
                "writer": t("Data"), "pending": st.column_config.NumberColumn(t("Waiting"), format="localized"),
                "queued": st.column_config.NumberColumn(t("Queued"), format="localized"),
                "in_flight": st.column_config.NumberColumn(t("In flight"), format="localized"),
                "written": st.column_config.NumberColumn(t("Written"), format="localized"),
                "dropped": st.column_config.NumberColumn(t("Dropped"), format="localized"),
                "backoff_seconds": st.column_config.NumberColumn(
                    t("Retry in (s)"), format="%.0f",
                    help=t("After a failed write: the wait before the next attempt, doubling up to 30 s.")),
                "last_flush_at": st.column_config.DatetimeColumn(t("Last write ({zone})", zone=ui.zone_name()), format="YYYY-MM-DD HH:mm:ss"),
                "last_error": t("Last error"),
            },
        )
        st.caption(t("MariaDB version") + ": " + status["server_version"])
        st.subheader(t("Connection pool"), anchor=False)
        cols = st.columns(4)
        cols[0].metric(t("Open connections"), f"{pool['checked_in'] + pool['checked_out']} / {pool['configured_size']}",
                       help=t("Database connections the API keeps open, out of the configured number."), border=True)
        cols[1].metric(t("Checked out"), pool["checked_out"], help=t("Connections busy with a request right now."), border=True)
        cols[2].metric(t("Idle"), pool["checked_in"], help=t("Open connections waiting for work."), border=True)
        cols[3].metric(t("Overflow in use"), f"{pool['overflow']} / {pool['configured_max_overflow']}",
                       help=t("Extra connections opened under load. Near the maximum, requests start to wait."), border=True)

        st.subheader(t("Table rows"), anchor=False)
        st.caption(t("≈ marks estimated counts for live readings and system logs; the other counts are exact."))
        # Counts differ by orders of magnitude, so numbers read better than bars.
        approximate = set(status.get("approximate_rows", []))
        for column, (table, rows) in zip(st.columns(len(status["table_rows"])), status["table_rows"].items(), strict=True):
            # Large tables are estimated from their id range rather than counted.
            column.metric(t(TABLE_NAMES.get(table, table)), f"≈ {compact(rows)}" if table in approximate else f"{rows:,}",
                          help=t("Estimated from the id range; counting every row would be slow.") if table in approximate else None, border=True)


status_panel()
