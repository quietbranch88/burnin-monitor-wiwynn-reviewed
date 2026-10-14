from datetime import datetime, timedelta

import streamlit as st
from lib import api, session, ui
from lib.i18n import t

st.title(t("Telemetry history"), anchor=False)
st.caption(t("Inspect stored readings by server, sensor, and time."))
ui.tz_caption()
if not session.is_admin():
    st.stop()

try:
    sensors: dict[str, list[str]] = api.get("/realtime/config")["sensors"]
except api.ApiError as error:
    api.show_error(error)
    ui.retry()
    st.stop()
categories = list(sensors)

filters = st.container(border=True)
row = filters.columns([2, 2, 2, 2.5, 1.5], vertical_alignment="bottom")
ui.keep_option("keep:history-category", ["All", *categories])
category = row[0].selectbox(t("Category"), ["All", *categories], format_func=lambda c: t(c) if c == "All" else ui.label(c),
                            key="keep:history-category")
# The sensors of the chosen category; a sensor name alone can repeat across categories.
# One box per category: a single keyed box would keep showing the previous category's sensor.
sensor_options = ["All", *sensors.get(category, [])]
sensor_key = f"keep:history-sensor-{category}"
ui.keep_option(sensor_key, sensor_options)
sensor = row[1].selectbox(t("Sensor"), sensor_options, format_func=lambda s: t(s) if s == "All" else s,
                          key=sensor_key, disabled=category == "All",
                          help=t("Pick a category first.") if category == "All" else None)
server = ui.server_id(row[2].text_input(t("Server"), placeholder="S0001 or 1", key="keep:history-server", max_chars=20))
# Read before drawing: "Up to a time" replaces the time range, so the range is shown as unused.
up_to_on = st.session_state.get("keep:history-up-to", False)
with row[3]:
    span = ui.time_range("history", default="Last 1 hour", disabled=up_to_on,
                         help=t("Not used while “Up to a time” is on.") if up_to_on else None)
anomalies_only = row[4].toggle(t("Alerts only"), key="keep:history-alerts", help=t("Only readings past their limit: above it, or below it for fans."))

# Jump to a moment instead of paging back to it: the newest readings up to that time.
row = filters.columns([2, 2, 2, 4], vertical_alignment="bottom")


def start_up_to() -> None:
    # Turned on: start from now, not from the moment chosen the last time.
    for key in ("keep:history-up-to-date", "keep:history-up-to-time"):
        st.session_state.pop(key, None)


up_to = row[0].toggle(t("Up to a time"), key="keep:history-up-to", help=t("Show the readings just before a chosen moment."),
                      on_change=start_up_to)
end = None
if up_to:
    now = datetime.now(ui.zone()).replace(second=0, microsecond=0) + timedelta(minutes=1)  # include this minute
    day = row[1].date_input(t("Date ({zone})", zone=ui.zone_name()), value=now.date(), key="keep:history-up-to-date")
    at = row[2].time_input(t("Time ({zone})", zone=ui.zone_name()), value=now.time(), key="keep:history-up-to-time", step=60)
    end = datetime.combine(day, at, tzinfo=ui.zone())
    span = {}  # the readings just before that moment, however far back

params = {"limit": 100, "anomalies_only": anomalies_only, **span}
if end is not None:
    params["end"] = end.isoformat()
if category != "All":
    params["category"] = category
    if sensor != "All":
        params["sensor"] = sensor
if server:
    params["server"] = server

# Keyset paging: remember the cursors of the pages seen so "Newest" can go back. The filters
# are identified by what the user picked, not by the computed start time, which moves.
filters_key = (category, sensor, server, ui.range_choice("history"), anomalies_only, end)
if st.session_state.get("history_filters") != filters_key:
    st.session_state.history_filters = filters_key
    st.session_state.history_cursors = [None]
cursors = st.session_state.history_cursors
if cursors[-1] is not None:
    params["before_at"], params["before_id"] = cursors[-1]

try:
    result = api.get("/admin/realtime/history", params=params)
except api.ApiError as error:
    api.show_error(error)
    ui.retry()
    st.stop()


def newest() -> None:
    st.session_state.history_cursors = [None]


def older(cursor: tuple[str, int]) -> None:
    st.session_state.history_cursors.append(cursor)


def previous() -> None:
    st.session_state.history_cursors.pop()


st.subheader(t("Stored readings"), anchor=False)
# Keyset pages cannot jump to page N; say why, since a page-number control is what people expect.
paging_help = t("Each page continues from the last reading on screen, so readings stored meanwhile never shift "
                "or repeat rows. Step one page at a time with Older and Previous; there is no jump to a page number.")
nav = st.columns([1, 1, 1, 3], vertical_alignment="center")
nav[0].button(t("Newest"), disabled=len(cursors) == 1, on_click=newest, width="stretch", icon=":material/first_page:")
nav[1].button(t("Previous"), disabled=len(cursors) == 1, on_click=previous, width="stretch",
              icon=":material/chevron_left:", help=paging_help)
# The cursor is bound now, from the page on screen. Read in the click's rerun instead, it would
# come from the first page fetched again, which has moved on while new readings arrived.
nav[2].button(t("Older"), disabled=result["next_before_id"] is None, on_click=older,
              args=((result["next_before_at"], result["next_before_id"]),), width="stretch",
              icon=":material/chevron_right:", help=paging_help)
nav[3].caption(t("Page {page} · 100 readings per page · newest first", page=len(cursors)))
st.caption(t("Readings are written in batches. This table does not refresh automatically; reload to check again, usually within about 10 s."))

frame = ui.local_frame(result["items"], "generated_at")
if frame.empty:
    st.info(t("No persisted readings match."))
    st.caption(t("Try a wider time range or adjust the filters."))
    st.stop()
if category != "All" and sensor != "All" and server:
    ui.time_chart(frame[["generated_at", "value", "threshold"]], "generated_at", ["value", "threshold"],
                  time_format="%m-%d %H:%M:%S", height=240)
frame["category"] = frame["category"].map(ui.label)
# A word rather than a faint tick, which reads as unchecked at a glance.
frame["is_anomaly"] = frame["is_anomaly"].map(lambda over: t("Over limit") if over else "")
st.dataframe(
    frame[["generated_at", "server", "category", "sensor", "value", "threshold", "is_anomaly"]],
    hide_index=True,
    width="stretch",
    column_config={
        "server": t("Server"), "value": st.column_config.NumberColumn(t("Value"), format="%.2f"),
        "threshold": t("Threshold"), "is_anomaly": t("Over limit"),
        "category": st.column_config.TextColumn(t("Metric")), "sensor": t("Sensor"),
        "generated_at": st.column_config.DatetimeColumn(t("Time ({zone})", zone=ui.zone_name()), format="YYYY-MM-DD HH:mm:ss"),
    },
)
