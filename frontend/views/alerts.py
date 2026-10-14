import pandas as pd
import streamlit as st
from lib import api, session, ui
from lib.i18n import t

st.title(t("Alert events"))
ui.tz_caption()
st.caption(t("One row per alert: from a sensor's first reading past its limit to its first reading back inside it, "
             "with the peak. Kept 30 days, so a run can be checked after its raw readings are gone."))
if outcome := st.session_state.pop("alerts_ack_outcome", None):
    succeeded, total = outcome
    if succeeded == total:
        st.success(t("Acknowledged {count} alerts.", count=succeeded))
    else:
        st.warning(t("Confirmed {succeeded} of {total} selected alerts. {remaining} remain unconfirmed. Refresh before retrying.",
                     succeeded=succeeded, total=total, remaining=total - succeeded))
if error := st.session_state.pop("alerts_ack_error", None):
    api.show_error(api.ApiError(*error))

try:
    sensors: dict[str, list[str]] = api.get("/realtime/config")["sensors"]
except api.ApiError as error:
    api.show_error(error)
    ui.retry()
    st.stop()
categories = list(sensors)

with st.popover(t("Filters"), icon=":material/filter_list:"):
    ui.keep_option("keep:alerts-category", ["All", *categories])
    category = st.selectbox(t("Category"), ["All", *categories], format_func=lambda c: t(c) if c == "All" else ui.label(c),
                            key="keep:alerts-category")
    server = ui.server_id(st.text_input(t("Server"), placeholder="S0001 or 1", key="keep:alerts-server", max_chars=20))
    span = ui.time_range("alerts", default="Last 24 hours")
    unacknowledged = st.toggle(t("Unacknowledged only"), key="keep:alerts-unacknowledged")

params = {"limit": 100, **span}
if category != "All":
    params["category"] = category
if server:
    params["server"] = server
if unacknowledged:
    params["acknowledged"] = False

# Keyset paging, as on the telemetry history page: the cursors of the pages seen so far.
filters_key = (category, server, ui.range_choice("alerts"), unacknowledged)
if st.session_state.get("alerts_filters") != filters_key:
    st.session_state.alerts_filters = filters_key
    st.session_state.alerts_cursors = [None]
cursors = st.session_state.alerts_cursors
if cursors[-1] is not None:
    params["before_at"], params["before_id"] = cursors[-1]

try:
    result = api.get("/realtime/alert-events", params=params)
except api.ApiError as error:
    api.show_error(error)
    ui.retry()
    st.stop()


def newest() -> None:
    st.session_state.alerts_cursors = [None]


def older(cursor: tuple[str, int]) -> None:
    st.session_state.alerts_cursors.append(cursor)


def previous() -> None:
    st.session_state.alerts_cursors.pop()


# Keyset pages cannot jump to page N; say why, since a page-number control is what people expect.
paging_help = t("Each page continues from the last alert on screen, so alerts stored meanwhile never shift "
                "or repeat rows. Step one page at a time with Older and Previous; there is no jump to a page number.")
nav = st.columns([1, 1, 1, 3], vertical_alignment="center")
nav[0].button(t("Newest"), disabled=len(cursors) == 1, on_click=newest, width="stretch", icon=":material/first_page:")
nav[1].button(t("Previous"), disabled=len(cursors) == 1, on_click=previous, width="stretch",
              icon=":material/chevron_left:", help=paging_help)
nav[2].button(t("Older"), disabled=result["next_before_id"] is None, on_click=older,
              args=((result["next_before_at"], result["next_before_id"]),), width="stretch",
              icon=":material/chevron_right:", help=paging_help)
nav[3].caption(t("Page {page} · 100 alerts per page, newest first. An alert is stored when it ends.", page=len(cursors)))

items = result["items"]
# A selection contains row positions, not IDs. Recreate the widget whenever those
# positions can refer to another event, including a new alert arriving on page 1.
selection_scope = (filters_key, cursors[-1], tuple((item["id"], item["acknowledged_at"]) for item in items))
if st.session_state.get("alerts_selection_scope") != selection_scope:
    st.session_state.alerts_selection_scope = selection_scope
    st.session_state.alerts_selection_version = st.session_state.get("alerts_selection_version", 0) + 1
table_key = f"alerts-table-{st.session_state.alerts_selection_version}"
frame = ui.local_frame(items, "started_at", "ended_at", "acknowledged_at")
if frame.empty:
    st.info(t("No alerts match."))
    st.stop()
# Empty while the alert is still open, or when the API stopped while it was open.
frame["duration_s"] = (pd.to_datetime(frame["ended_at"]) - pd.to_datetime(frame["started_at"])).dt.total_seconds()
frame["category"] = frame["category"].map(ui.label)
can_acknowledge = session.can_edit()
table = frame[["started_at", "ended_at", "duration_s", "server", "category", "sensor", "peak_value", "threshold", "readings",
               "acknowledged_by", "acknowledged_at"]]
column_config: dict = {
        "started_at": st.column_config.DatetimeColumn(t("Started ({zone})", zone=ui.zone_name()), format="YYYY-MM-DD HH:mm:ss"),
        "ended_at": st.column_config.DatetimeColumn(t("Ended ({zone})", zone=ui.zone_name()), format="YYYY-MM-DD HH:mm:ss"),
        "duration_s": st.column_config.NumberColumn(t("Duration (s)"), format="%.0f"),
        "server": t("Server"), "category": st.column_config.TextColumn(t("Metric")), "sensor": t("Sensor"),
        "peak_value": st.column_config.NumberColumn(t("Peak"), format="%.2f"),
        "threshold": t("Threshold"), "readings": t("Readings over the limit"),
        "acknowledged_by": t("Acknowledged by"),
        "acknowledged_at": st.column_config.DatetimeColumn(t("Acknowledged ({zone})", zone=ui.zone_name()),
                                                           format="YYYY-MM-DD HH:mm:ss"),
}
if can_acknowledge:
    selection = st.dataframe(table, hide_index=True, width="stretch", key=table_key, on_select="rerun",
                             selection_mode="multi-row", column_config=column_config)
    selected_ids = [items[index]["id"] for index in selection.selection.rows if items[index]["acknowledged_at"] is None]
    if st.button(t("Acknowledge selected"), disabled=not selected_ids, icon=":material/done_all:") and selected_ids:
        # Keep progress before each request: even a session/role-change rerun must
        # not lose responses already confirmed by the API.
        st.session_state.alerts_selection_version += 1
        st.session_state.alerts_ack_outcome = (0, len(selected_ids))
        for index, alert_id in enumerate(selected_ids):
            try:
                api.patch(f"/realtime/alert-events/{alert_id}", json={"acknowledged": True})
            except api.ApiError as error:
                st.session_state.alerts_ack_error = (error.status, error.detail, error.errors)
                break
            st.session_state.alerts_ack_outcome = (index + 1, len(selected_ids))
        st.rerun()
else:
    st.dataframe(table, hide_index=True, width="stretch", key=table_key, column_config=column_config)
