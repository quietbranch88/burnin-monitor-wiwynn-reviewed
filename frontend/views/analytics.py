import pandas as pd
import streamlit as st
from lib import api, ui
from lib.config import UNITS
from lib.i18n import t

ALL = "All categories"
# Axis format per bucket size, always 24-hour.
TIME_FORMATS = {"minute": "%m-%d %H:%M", "hour": "%m-%d %H:00", "day": "%Y-%m-%d"}

st.title(t("Analytics"), anchor=False)
st.caption(t("Compare results, follow trends, and download a report for your selected data."))

source = st.radio(t("Source"), ["records", "realtime"], horizontal=True, key="keep:analytics-source",
                  format_func=lambda s: t("Test records") if s == "records" else t("Live telemetry"))
st.caption(t("Test records are results entered or imported by engineers.") if source == "records"
           else t("Live telemetry is sensor data reported by servers during operation."))
try:
    thresholds = {} if source == "records" else api.get("/realtime/config")["thresholds"]
    categories = api.get("/records/categories") if source == "records" else list(thresholds)
except api.ApiError as error:
    api.show_error(error)
    ui.retry()
    st.stop()
# All categories first, so the per-category table is on the page from the start; picking one
# category shows its average and extremes. Kept per source: the sources have different categories.
ui.keep_option(f"keep:analytics-{source}-category", [ALL, *categories])
category_column, server_column, filter_column = st.columns([3, 3, 1], vertical_alignment="bottom")
category = category_column.selectbox(t("Category"), [ALL, *categories],
                                    format_func=lambda c: t(c) if c == ALL else ui.label(c),
                                    key=f"keep:analytics-{source}-category")
server = ui.server_id(server_column.text_input(
    t("Server"), placeholder=t("All servers — enter an ID to filter"),
    key=f"keep:analytics-{source}-server", max_chars=20,
    help=t("One server. For live telemetry its statistics come from its raw readings, and the Excel data "
           "sheet holds the latest 50,000 of them: about 17 minutes of one server.")))
with filter_column.popover(t("Filters"), width="stretch"):
    span = ui.time_range(f"analytics-{source}", default="All time" if source == "records" else "Last 1 hour")
    interval = st.selectbox(t("Trend interval"), ["minute", "hour", "day"],
                            index=0 if source == "realtime" else 2,
                            key=f"keep:analytics-{source}-interval", format_func=t,
                            help=t("How long each point of the trend covers."))
preset, dates = ui.range_choice(f"analytics-{source}")
chosen_range = t(preset)
if preset == "Custom" and dates:
    chosen_range += " · " + " – ".join(str(day) for day in (dates if isinstance(dates, tuple) else (dates,)))
st.caption(t("Time range") + ": " + chosen_range + " · " + t("Trend interval") + ": " + t(interval))
ui.tz_caption()

if source == "realtime" and server:
    st.caption(t("One server: the statistics come from its raw readings in the whole minutes of the range, "
                 "the same readings the Excel data sheet lists (at most 50,000). The range spans at most 3 hours; "
                 "the last hour when none is given."))
elif source == "realtime":
    st.caption(t("Live telemetry is summarised per minute: a time range counts the whole minutes inside it, "
                 "up to the last finished minute, and the Excel data sheet lists the readings of those minutes."))
params = {"source": source, **span}
if category != ALL:
    params["category"] = category
if server:
    params["server"] = server

try:
    with st.spinner(t("Loading…")):
        summary = api.get("/analytics/summary", params=params)
        by_category = api.get("/analytics/categories", params=params)
        trend = api.get("/analytics/trend", params=params | {"interval": interval, "tz": ui.zone_name()})
except api.ApiError as error:
    api.show_error(error)
    ui.retry()
    st.stop()

st.subheader(t("Overview"), anchor=False)
unit = "" if category == ALL else UNITS.get(category, "")
# Statistics across metrics with different units mean nothing, so the cards show them for one
# category; with all categories selected, the per-category table below has them.
count_label = t("Total records") if source == "records" else t("Total readings")
if category == ALL:
    metrics = st.columns(2, gap="medium")
    metrics[0].metric(count_label, f"{summary['count']:,}", border=True)
    metrics[1].metric(t("Categories"), f"{len(by_category):,}", border=True)
else:
    metrics = st.columns(5, gap="medium")
    metrics[0].metric(count_label, f"{summary['count']:,}", border=True)
    shown = [("Total", "total"), ("Average", "average"), ("Minimum", "minimum"), ("Maximum", "maximum")]
    for column, (title, key) in zip(metrics[1:], shown, strict=True):
        column.metric(f"{t(title)} ({unit})" if unit else t(title), ui.format_number(summary[key]), border=True)

if not by_category:
    st.info(t("No data for these filters."))
    st.stop()

with st.container(border=True):
    st.subheader(t("Trend"), anchor=False)
    st.caption(t("Average, minimum, and maximum for each time interval."))
    # The trend endpoint returns at most this many points, newest first.
    TREND_POINT_LIMIT = 5000
    if trend and len(trend) >= TREND_POINT_LIMIT:
        st.warning(t("The trend shows only the newest {count} points; pick an hour or day interval to see the whole range.",
                     count=f"{TREND_POINT_LIMIT:,}"))
    elif trend:
        first = ui.local_frame(trend, "bucket")["bucket"].min()
        st.caption(t("The data in this range starts at {time}.", time=f"{first:%Y-%m-%d %H:%M}"))
    if trend:
        points = ui.local_frame(trend, "bucket")
        names = sorted(points["category"].unique())
        tabs = st.tabs([f"{ui.label(c)} ({UNITS[c]})" if c in UNITS else ui.label(c) for c in names])
        for tab, name in zip(tabs, names, strict=True):
            with tab:
                series = points[points["category"] == name][["bucket", "average", "minimum", "maximum"]]
                lines = ["average", "minimum", "maximum"]
                if name in thresholds:  # live telemetry: show the alert threshold too
                    series = series.assign(threshold=thresholds[name])
                    lines.append("threshold")
                ui.time_chart(series, "bucket", lines, time_format=TIME_FORMATS[interval], height=260)
    else:
        st.info(t("No trend points for these filters."))

# With one category the table would repeat the cards above; it compares several.
if category == ALL:
    with st.container(border=True):
        st.subheader(t("By category"), anchor=False)
        st.caption(t("Compare each category in its own unit. Select a category above for summary statistics."))
        frame = pd.DataFrame(by_category)
        frame.insert(0, "name", frame["category"].map(ui.label))
        frame.insert(1, "unit", frame["category"].map(ui.unit))
        st.dataframe(
            frame[["name", "unit", "count", "average", "minimum", "maximum", "total"]],
            hide_index=True,
            width="stretch",
            column_config={"name": t("Category"), "unit": t("Unit"),
                           "count": st.column_config.NumberColumn(t("Count"), format="localized"),
                           "average": st.column_config.NumberColumn(t("Average"), format="%.2f"),
                           "minimum": st.column_config.NumberColumn(t("Minimum"), format="%.2f"),
                           "maximum": st.column_config.NumberColumn(t("Maximum"), format="%.2f"),
                           "total": st.column_config.NumberColumn(t("Total"), format="%.2f")},
        )
        if len(frame) > 1:
            with st.expander(t("Compare counts by category")):
                ui.bar_chart(frame, "count", "name", title=t("Records per category") if source == "records" else t("Readings per category"),
                             height=60 + 40 * len(frame))

export_params = params | {"interval": interval, "tz": ui.zone_name()}
# Identified by what the user picked: the computed start time of "Last 1 hour" moves on every rerun.
export_key = (source, ui.range_choice(f"analytics-{source}"), category, server, interval, ui.zone_name())


def request_export():
    st.session_state.export_requested = True


@st.fragment
def export_report():
    # Only this card reruns: preparing a workbook must not reload the analytics charts.
    with st.container(border=True):
        st.markdown(f"**{t('Export')}**")
        st.caption(t("The report lists at most the latest 50,000 rows of raw data; its labels are in English."))
        prepared = st.session_state.get("excel")
        if prepared and prepared[0] != export_key:
            st.session_state.pop("excel", None)
            prepared = None
        actions = st.empty()
        failure = None
        if st.session_state.pop("export_requested", False):
            # Replace the previous controls before the blocking request, including regeneration.
            with actions.container(), st.spinner(t("Preparing the report…")):
                try:
                    # Relative presets advance while this fragment stays mounted.
                    request_params = {key: value for key, value in export_params.items() if key not in {"start", "end"}}
                    request_params.update(ui.resolve_time_range(*ui.range_choice(f"analytics-{source}")))
                    content = api.get("/analytics/export", params=request_params, raw=True, timeout=60)
                    prepared = (export_key, content)
                    st.session_state.excel = prepared
                    st.toast(t("Report generated. Click Download to save the latest file."), icon=":material/check_circle:")
                except api.ApiError as error:
                    failure = error
            actions.empty()
        with actions.container():
            if failure:
                api.show_error(failure)
            if prepared:
                st.caption(t("Report ready"))
            with st.container(horizontal=True, vertical_alignment="center"):
                if prepared:
                    st.download_button(
                        t("Download Excel report"), prepared[1],
                        file_name=f"analytics-{source}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        type="primary", icon=":material/download:", on_click="ignore",
                    )
                st.button(
                    t("Regenerate report") if prepared else t("Generate Excel report"),
                    icon=":material/refresh:" if prepared else ":material/table_view:",
                    type="secondary" if prepared else "primary", on_click=request_export,
                )


export_report()
