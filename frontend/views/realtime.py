"""Fleet dashboard: one tick summary per second, shared across every viewer.

Top to bottom: operational status, fleet and alert attention panels, metric summaries,
then trends and server details. Colour
marks only what needs attention: orange from the warning level, red past the limit.
"""

from datetime import UTC, datetime, timedelta
from html import escape

import pyarrow as pa
import streamlit as st
from lib import api, i18n, session, ui
from lib.config import API_BASE_URL, API_WS_URL, UNITS
from lib.i18n import t
from lib.stream import RealtimeStream

st.title(t("Live telemetry"), anchor=False)
st.caption(t("Monitor fleet health, inspect alerts, then drill into a server or metric."))
ui.tz_caption()


def get_stream() -> RealtimeStream:
    stream = st.session_state.get("stream")
    if stream is None or not stream.alive or stream.token != st.session_state.token:
        if stream is not None:
            stream.stop()
        stream = RealtimeStream(API_WS_URL, st.session_state.token, summary_url=f"{API_BASE_URL}/realtime/summary")
        # Fill the charts with the ticks generated before this page opened.
        stream.extend(api.get("/realtime/summary", params={"seconds": 300}))
        stream.start()
        st.session_state.stream = stream
    return stream


@st.fragment(run_every="5s")
def retry_soon() -> None:
    if st.session_state.pop("live_retry_armed", False):
        st.rerun(scope="app")
    st.session_state.live_retry_armed = True


try:
    config = api.get("/realtime/config")
    stream = get_stream()
except api.ApiError as error:
    # The stream reconnects by itself once it runs; before that, retry the whole page.
    api.show_error(error)
    st.caption(t("Retrying every 5 seconds."))
    retry_soon()
    st.stop()
st.session_state.pop("live_retry_armed", None)

categories: list[str] = list(config["thresholds"])
below: set[str] = set(config["alert_below"])
ABOUT = ("Each tile and grid cell shows the sensor nearest its limit: the hottest, or for fans the slowest, "
         "because one failing part matters more than the average. % of limit is how close it is: "
         "100% is the limit. Orange from {warning}%, red past the limit. An admin sets both.")


# The API sends per-server readings for fleets up to this size: SERVER_DETAIL_SERVERS in the
# backend, repeated here because the message carries no such field.
GRID_MAX_SERVERS = 200
# Ticks arrive every second; a few seconds of silence means the generator or the stream has stopped.
STALE_AFTER_SECONDS = 5
STATUS_TEXT = {"connecting": "Connecting…", "idle": "Paused while nobody is watching"}


def status_text(status: str) -> str:
    if status.startswith("reconnecting"):
        return t("Connection lost; reconnecting…")
    if status.startswith("rejected"):
        return t("The server refused the live connection; retrying…")
    return t(STATUS_TEXT.get(status, status))


def local_time(iso: str) -> str:
    """API time (UTC, 'Z') -> display-zone wall clock as a naive ISO string for Vega."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ui.zone()).replace(tzinfo=None).isoformat()


def load(r: dict) -> float:
    return ui.load(r["value"], r["threshold"], r["category"] in below)


def worst_label(category: str) -> str:
    return t("fleet min") if category in below else t("fleet max")


# Chart data sits inside a layer on purpose. Streamlit moves top-level `data` and `datasets`
# into Arrow, and its frontend redraws only when that Arrow table's shape or byte size
# changes, so a same-shaped update (a full 60 s window) can stay frozen on screen.
# Data inside a layer stays in the spec, and a changed spec always redraws.
def line_spec(title: str, points: list[dict], worst: str) -> dict:
    return {
        "title": {"text": title, "anchor": "start", "fontSize": 14},
        "height": 220,
        "layer": [{
            "data": {"values": points},
            "mark": {"type": "line"},
            "encoding": {
                "x": {"field": "t", "type": "temporal", "title": None,
                      "axis": {"labelExpr": "timeFormat(datum.value, '%H:%M:%S')", "labelOverlap": True}},
                "y": {"field": "v", "type": "quantitative", "title": None, "scale": {"zero": False}},
                "color": {"field": "series", "type": "nominal", "legend": {"orient": "top", "title": None},
                          "scale": {"domain": [worst, t("fleet average"), t("threshold")],
                                    "range": [ui.ORANGE, "#4A87C2", ui.RED]}},
                "tooltip": [{"field": "t", "type": "temporal", "format": "%H:%M:%S", "title": t("time")},
                            {"field": "series", "title": t("Line")}, {"field": "v", "format": ",.2f", "title": t("value")}],
            },
        }],
    }


def bar_spec(summary: dict, warning: int) -> dict:
    """Compare the newest worst sensor per metric on a common limit-relative axis."""
    points = [{"metric": ui.label(category),
               "percent": ui.load(s["worst"], s["threshold"], s["alert_below"]),
               "value": ui.format_value(s["worst"], category)} for category, s in summary.items()]
    return {
        "title": {"text": t("Latest reading by metric"), "anchor": "start", "fontSize": 14},
        "height": 220,
        "layer": [
            {"data": {"values": points}, "mark": {"type": "bar"},
             "encoding": {
                 "y": {"field": "metric", "type": "nominal", "title": None, "sort": None,
                       "axis": {"labelLimit": 150}},
                 "x": {"field": "percent", "type": "quantitative", "title": t("% of limit"),
                       "scale": {"domainMin": 0}},
                 "color": {"condition": [{"test": "datum.percent > 100", "value": ui.RED},
                                         {"test": f"datum.percent >= {warning}", "value": "#FFAF03"}], "value": ui.BLUE},
                 "tooltip": [{"field": "metric", "title": t("metric")}, {"field": "value", "title": t("value")},
                             {"field": "percent", "title": t("% of limit"), "format": ".1f"}]}},
            {"data": {"values": [{"limit": 100}]}, "mark": {"type": "rule", "strokeDash": [4, 4], "color": ui.RED},
             "encoding": {"x": {"field": "limit", "type": "quantitative"}}},
        ],
    }


# cache_resource, not cache_data: every viewer gets the same objects without a pickle copy,
# and the tables go to the browser as pyarrow without a pandas conversion. Nothing mutates them.
@st.cache_resource(ttl=10, max_entries=32, show_spinner=False)
def fleet_view(last_tick: str, first_tick: str, count: int, window: int, lang: str, zone: str,  # noqa: ARG001
               digest: str, _ticks: tuple) -> dict:  # noqa: ARG001 - key parts only; see the docstring
    """Tiles, charts and the alert list for one tick, built once and shared by every viewer.

    `digest` (lib.stream.fingerprint) stands for the complete tick content in the cache key:
    equal endpoints and sample counts can still hide different middle history after a
    reconnect. The ticks themselves are passed with a leading underscore, so Streamlit does
    not hash the whole buffer on every refresh; the stream digests each tick once on arrival.
    Identical histories share the resource, while lang and zone distinguish display labels
    and times.
    """
    cutoff = datetime.fromisoformat(last_tick.replace("Z", "+00:00")) - timedelta(seconds=window)
    ticks = [tick for tick in _ticks if datetime.fromisoformat(tick["generated_at"].replace("Z", "+00:00")) >= cutoff]
    latest = ticks[-1]
    charts = {}
    for category in categories:
        points = []
        for tick in ticks:
            s = tick["summary"].get(category)
            if s is None:
                continue
            when = local_time(tick["generated_at"])
            points += [{"t": when, "series": worst_label(category), "v": s["worst"]},
                       {"t": when, "series": t("fleet average"), "v": s["average"]},
                       {"t": when, "series": t("threshold"), "v": s["threshold"]}]
        charts[category] = line_spec(f"{ui.label(category)} ({UNITS.get(category, '')})", points, worst_label(category))
    # One row per sensor in alert, worst first: which server and sensor, how far past the limit, how often.
    grouped: dict[tuple[str, str, str], dict] = {}
    for tick in ticks:
        for a in tick["alerts"]:
            g = grouped.setdefault((a["server"], a["category"], a["sensor"]), {"count": 0, "worst": a, "last": a["generated_at"]})
            g["count"] += 1
            g["last"] = max(g["last"], a["generated_at"])
            if load(a) > load(g["worst"]):
                g["worst"] = a
    worst_first = sorted(grouped.items(), key=lambda item: load(item[1]["worst"]), reverse=True)
    alerts = [{t("server"): server, t("sensor"): f"{ui.label(category)} · {sensor}",
               t("% of limit"): round(load(g["worst"]), 1), t("last seen"): local_time(g["last"])[11:19]}
              for (server, category, sensor), g in worst_first[:30]]
    return {"summary": latest["summary"], "warning": latest.get("warning_percent", config["warning_percent"]),
            "charts": charts, "alerts": pa.Table.from_pylist(alerts) if alerts else None,
            # From the per-category counts: the alert list itself holds at most 50 readings a second.
            "alerts_in_window": sum(s["anomalies"] for tick in ticks for s in tick["summary"].values())}


def severity(percent: float, warning: int) -> str:
    return "alert" if percent > 100 else "warning" if percent >= warning else "normal"


def tile_palette(theme_type: str) -> dict[str, str]:
    """Presentation colors belong to the viewer, outside the shared tick caches."""
    if theme_type == "dark":
        return {"surface": "#1B2430", "text": "#E8EEF5", "muted": "#B0BECD",
                "border": "#364354", "track": "#303D4D", "normal": "#8AA4BA"}
    return {"surface": "#F8FAFC", "text": "#182635", "muted": "#526170",
            "border": "#DCE3EB", "track": "#E5EBF1", "normal": "#657D91"}


def tiles_html(view: dict, theme_type: str = "light") -> str:
    """One tile per metric: the reading nearest the limit and a bar to the limit."""
    palette = tile_palette(theme_type)
    tiles = []
    for category in categories:
        s = view["summary"].get(category)
        if s is None:
            continue
        percent = ui.load(s["worst"], s["threshold"], s["alert_below"])
        state = severity(percent, view["warning"])
        fill = {"normal": palette["normal"], "warning": "#FFAF03", "alert": ui.RED}[state]
        edge = palette["border"] if state == "normal" else fill
        hover = t("Limit {limit}; average {average}", limit=ui.format_value(s["threshold"], category),
                  average=ui.format_value(s["average"], category))
        # Four lines only: what, the value, how close to the limit (the tick), and where.
        tiles.append(f"""
<div style="border:1px solid {palette['border']};border-left:3px solid {edge};border-radius:6px;
            background:{palette['surface']};padding:12px;min-width:0"
     title="{escape(hover)}">
  <div style="font-size:12px;color:{palette['muted']};white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{escape(ui.label(category))}</div>
  <div style="font-size:22px;font-weight:600;white-space:nowrap;color:{palette['text']};
              font-variant-numeric:tabular-nums;line-height:1.5">{escape(ui.format_value(s["worst"], category))}</div>
  <div style="position:relative;height:4px;background:{palette['track']};border-radius:3px;margin:8px 0">
    <div style="position:absolute;left:0;top:0;bottom:0;width:{min(percent, 120) / 1.2:.1f}%;background:{fill};border-radius:3px"></div>
    <div style="position:absolute;left:83.3%;top:-2px;bottom:-2px;width:2px;background:{palette['text']}"></div>
  </div>
  <div style="font-size:12px;color:{palette['muted']};white-space:nowrap;overflow:hidden;
              text-overflow:ellipsis;font-variant-numeric:tabular-nums">{percent:.0f}% · {escape(s["worst_server"])} {escape(s["worst_sensor"])}</div>
</div>""")
    # One row on a wide screen; on a phone the tiles wrap, two or three to a row.
    return f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px">{"".join(tiles)}</div>'


ALL = "all"


@st.cache_resource(ttl=10, max_entries=16, show_spinner=False)
def grid_cells(tick: str, color_by: str, warning: int, lang: str, _readings: list[dict]) -> list[dict]:  # noqa: ARG001
    """One cell per server: its sensor nearest the limit, in one metric or in all of them."""
    worst: dict[str, dict] = {}
    for r in _readings:
        if color_by != ALL and r["category"] != color_by:
            continue
        if r["server"] not in worst or load(r) > load(worst[r["server"]]):
            worst[r["server"]] = r
    servers = sorted(worst)
    columns = 10 if len(servers) <= 40 else 20
    cells = []
    for index, server in enumerate(servers):
        r = worst[server]
        percent = load(r)  # Round only the tooltip; classification must retain small excursions.
        cells.append({"x": index % columns, "y": index // columns, "server": server, "short": server.lstrip("S0") or "0",
                      "metric": ui.label(r["category"]), "sensor": r["sensor"], "value": ui.format_value(r["value"], r["category"]),
                      "percent": percent, "state": severity(percent, warning)})
    return cells


def grid_spec(cells: list[dict], warning: int) -> dict:
    rows = max(c["y"] for c in cells) + 1
    tooltip = [{"field": "server", "title": t("server")}, {"field": "metric", "title": t("metric")},
               {"field": "sensor", "title": t("sensor")}, {"field": "value", "title": t("value")},
               {"field": "percent", "title": t("% of limit"), "format": ".1f"}]
    position = {"x": {"field": "x", "type": "ordinal", "axis": None},
                "y": {"field": "y", "type": "ordinal", "axis": None}}
    return {
        "height": 56 * rows,
        "layer": [
            # The selection belongs to one layer; on the top level every layer would declare it again.
            {"params": [{"name": "pick", "select": {"type": "point", "fields": ["server"]}}],
             "data": {"values": cells}, "mark": {"type": "rect", "cornerRadius": 3, "stroke": "#FFFFFF", "strokeWidth": 2},
             "encoding": position | {
                 # Calm blues below the warning level, so the warning and alert colours stand out.
                 "color": {"condition": [{"test": "datum.percent > 100", "value": ui.RED},
                                         {"test": f"datum.percent >= {warning}", "value": "#FFAF03"}],
                           "field": "percent", "type": "quantitative", "legend": None,
                           "scale": {"domain": [0, warning], "range": ["#EAF5FA", "#7CC4E4"], "clamp": True}},
                 "tooltip": tooltip}},
            {"data": {"values": cells}, "mark": {"type": "text", "fontSize": 11},
             "encoding": position | {"text": {"field": "short"},
                                     "color": {"condition": {"test": "datum.percent > 100", "value": "#FFFFFF"}, "value": "#0F2430"},
                                     "tooltip": tooltip}},
        ],
        "config": {"view": {"stroke": None}},
    }


def pick_server(grid_key: str) -> None:
    """A click on a grid cell opens that server's details."""
    if grid_key != st.session_state.get("live-grid-key"):
        return
    selection = st.session_state.get(grid_key, {}).get("selection", {}).get("pick", [])
    if selection:
        st.session_state.detail_server = selection[0]["server"]
        st.session_state["keep:live-server"] = selection[0]["server"]
        st.session_state["keep:live-view"] = "details"
        # Retire the old tabs: a delayed event from them must not undo this jump.
        st.session_state["live-tabs-generation"] = st.session_state.get("live-tabs-generation", 0) + 1


@st.cache_resource(ttl=10, max_entries=8, show_spinner=False)
def server_table(tick: str, lang: str, _readings: list[dict]) -> pa.Table:  # noqa: ARG001 - tick and lang are the key
    """One row per server with its sensor nearest the limit per metric; `tick` is the cache key."""
    rows: dict[str, dict] = {}
    for r in _readings:
        row = rows.setdefault(r["server"], {t("server"): r["server"]} | {ui.label(c): None for c in categories})
        row[ui.label(r["category"])] = r["value"]
        if r["is_anomaly"]:
            row[t("over limit")] = ", ".join(filter(None, [row.get(t("over limit")), f"{ui.label(r['category'])} ({r['sensor']})"]))
    return pa.Table.from_pylist([row | {t("over limit"): row.get(t("over limit"), "—")} for row in rows.values()])


def server_details(servers: list[str], default: str) -> None:
    """Every sensor of one server, from the newest tick held by the API."""
    if st.session_state.get("keep:live-server") not in servers:
        st.session_state["keep:live-server"] = default
    # The widget is removed on other tabs; retain the user's choice independently.
    st.session_state.detail_server = st.session_state["keep:live-server"]

    def remember_server() -> None:
        st.session_state["keep:live-server"] = st.session_state.detail_server

    server = st.selectbox(t("Server"), servers, key="detail_server", on_change=remember_server)
    try:
        readings = api.get("/realtime/latest", params={"server": server, "limit": 1000})
    except api.ApiError as error:
        api.show_error(error)
        return
    rows = [{t("metric"): ui.label(r["category"]), t("sensor"): r["sensor"], t("value"): ui.format_value(r["value"], r["category"]),
             t("limit"): ui.format_value(r["threshold"], r["category"]), t("% of limit"): round(load(r), 1)}
            for r in sorted(readings, key=load, reverse=True)]
    st.dataframe(pa.Table.from_pylist(rows), hide_index=True, width="stretch", height=min(38 + 35 * len(rows), 560),
                 column_config={t("% of limit"): st.column_config.ProgressColumn(
                     t("% of limit"), min_value=0, max_value=120, format="%.1f%%",
                     help=t("How close the reading is to its limit; above 100% is an alert."))})


def status_line(ticks: list[dict], readings: list[dict] | None, warning: int, last: str | None) -> int:
    """Is the page live, how big is the fleet, what needs attention; returns the chosen window in seconds."""
    # Readings past the limit this second, from the per-category counts; the warning count is of
    # the sensors shown in the grid (the worst per server and metric), which the message lists.
    alerts = sum(s["anomalies"] for s in ticks[-1]["summary"].values()) if ticks else 0
    warnings = sum(severity(load(r), warning) == "warning" for r in readings or [])
    # Two columns, so on a phone the window control moves below instead of squeezing the line.
    status_column, window_column = st.columns([2.9, 2.1], vertical_alignment="center")
    # A connected socket does not prove data is flowing: the generator pauses while the database is
    # down. Without a recent tick, say so instead of showing the last second's counts as "Now".
    age = None if last is None else (datetime.now(UTC) - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds()
    fresh = age is not None and age <= STALE_AFTER_SECONDS
    with status_column, st.container(border=True, horizontal=True, vertical_alignment="center", gap="medium"):
        if stream.status != "connected":
            st.badge(status_text(stream.status), icon=":material/wifi_off:", color="orange")
        elif fresh:
            st.badge(t("Live"), icon=":material/sensors:", color="green")
        else:
            st.badge(t("No new data for {seconds} s", seconds=f"{age:,.0f}" if age is not None else "?"),
                     icon=":material/pause_circle:", color="orange")
        if readings is not None and fresh:
            # This second only; the alert list below covers the whole window.
            st.badge(t("Now: {count} past the limit", count=alerts), icon=":material/error:", color="red" if alerts else "gray")
            st.badge(t("Now: {count} at {warning}% or more", count=warnings, warning=warning), icon=":material/warning:",
                     color="orange" if warnings else "gray")
        st.caption(t("{servers} servers · {sensors} sensors each · {rate} readings/s",
                     servers=f"{config['servers']:,}", sensors=sum(len(s) for s in config["sensors"].values()),
                     rate=f"{config['readings_per_second']:,.0f}")
                   + (" · " + local_time(last)[11:19] if last else ""))
    # Covers the alert list and the trend charts; the tiles, the grid and the counts are always the newest second.
    # Older sessions may retain an empty choice from when this control was optional.
    if "keep:live-window" in st.session_state and st.session_state["keep:live-window"] is None:
        st.session_state["keep:live-window"] = 60
    window = window_column.segmented_control(t("Window"), [30, 60, 120, 300], default=60, key="keep:live-window",
                                             format_func=lambda s: t("{seconds} s", seconds=s), required=True,
                                             help=t("How far back the alert list and the trend charts go. The tiles, the grid "
                                                    "and the counts always show the newest second."))
    window_column.caption(t("Charts and alerts show the last {seconds} seconds. Readings update every second.",
                            seconds=window))
    return window


def fleet_grid(readings: list[dict] | None, warning: int) -> None:
    with st.container(horizontal=True, vertical_alignment="bottom"):
        st.subheader(t("Fleet grid"), anchor=False, help=t("One cell per server, numbered; hover for the sensor, click for its details."))
        if readings:
            options = [ALL, *categories]
            ui.keep_option("keep:live-color-by", options)
            color_by = st.selectbox(t("Colour by"), options, key="keep:live-color-by", label_visibility="collapsed",
                                    format_func=lambda c: t("Worst of all") if c == ALL else ui.label(c), width=180)
    if readings:
        # The direct answer to "which server is closest to failing right now".
        nearest = max(readings, key=load)
        st.markdown(t("Nearest the limit now: **{server}** · {metric} {sensor} · **{percent}%**",
                      server=nearest["server"], metric=ui.label(nearest["category"]), sensor=nearest["sensor"],
                      percent=f"{load(nearest):.0f}"))
        cells = grid_cells(readings[0]["generated_at"], color_by, warning, i18n.lang(), readings)
        grid_key = f"fleet-grid:{st.session_state.get('live-grid-generation', 0)}"
        # Callbacks in one batch still belong to this rendered chart, even when a
        # tab callback requests the next generation before its grid callback runs.
        st.session_state["live-grid-key"] = grid_key
        st.vega_lite_chart(spec=grid_spec(cells, warning), width="stretch", key=grid_key,
                           on_select=lambda: pick_server(grid_key), selection_mode="pick")
    elif config["servers"] > GRID_MAX_SERVERS:
        st.caption(t("The grid needs the per-server readings, which the API sends for fleets of up to 200 servers."))
    else:  # the summary history arrives first; the per-server readings come with the next live message
        st.caption(t("Waiting for the first readings…"))


def alert_table(view: dict, window: int) -> None:
    st.subheader(t("Over-limit readings in the last {seconds} s: {count}",
                   seconds=window, count=view["alerts_in_window"]), anchor=False)
    st.caption(t("Each second over the limit counts as one reading; the table groups sampled readings by sensor "
                 "and shows up to 30 sensors."))
    if view["alerts"] is None:
        st.success(t("No reading is past its limit in the last {seconds} s.", seconds=window), icon=":material/check_circle:")
    else:
        st.dataframe(view["alerts"], hide_index=True, width="stretch", height=260,
                     column_config={t("% of limit"): st.column_config.NumberColumn(
                                        t("% of limit"), format="%.1f%%", help=t("The worst reading in the window; above 100% is past the limit.")),
                                    t("last seen"): f"{t('last seen')} ({ui.zone_name()})"})


def detail_views(view: dict, readings: list[dict] | None) -> None:
    """Native tabs render only the chosen view, with stable IDs across language changes."""
    # The choice is an id, not a label, so it survives a language change.
    views = {"trends": t("Reading trends"), "details": t("Server details")}
    if readings:
        views["servers"] = t("All servers")
    ui.keep_option("keep:live-view", list(views))
    shown = st.session_state.get("keep:live-view", "trends")
    if shown not in views:
        shown = "trends"
    generation = st.session_state.get("live-tabs-generation", 0)
    tab_key = f"live-details-tabs:{generation}"
    st.session_state[tab_key] = views[shown]

    def remember_tab() -> None:
        if generation != st.session_state.get("live-tabs-generation", 0):
            return
        label = st.session_state[tab_key]
        selected = next(identifier for identifier, name in views.items() if name == label)
        if st.session_state.get("keep:live-view") == "details" and selected != "details":
            # Vega retains its selection per chart ID and ignores clicking the same
            # cell again. A deliberate exit from details starts a fresh selection.
            st.session_state["live-grid-generation"] = st.session_state.get("live-grid-generation", 0) + 1
        st.session_state["keep:live-view"] = selected

    tabs = st.tabs(list(views.values()), key=tab_key, on_change=remember_tab)
    selected_tab = tabs[list(views).index(shown)]
    if not selected_tab.open:
        return
    # Only the chosen view is drawn: seven charts or a full table, redrawn every second.
    with selected_tab:
        if shown == "trends":
            st.vega_lite_chart(spec=bar_spec(view["summary"], view["warning"]), width="stretch")
            chart_columns = st.columns(2)
            for index, category in enumerate(categories):
                chart_columns[index % 2].vega_lite_chart(spec=view["charts"][category], width="stretch")
        elif shown == "details":
            servers = sorted({r["server"] for r in readings}) if readings else [f"S{i:04d}" for i in range(1, config["servers"] + 1)]
            worst = max(readings, key=load)["server"] if readings else servers[0]
            server_details(servers, worst)
        elif readings:
            servers = server_table(readings[0]["generated_at"], i18n.lang(), readings)
            st.caption(t("{count} servers", count=servers.num_rows))
            st.caption(t("Per server and metric, the sensor nearest its limit."))
            # Numbers stay numbers so the columns sort correctly; shown with the tiles' units and decimals.
            st.dataframe(servers, hide_index=True, width="stretch", height=min(38 + 35 * servers.num_rows, 600),
                         column_config={ui.label(c): st.column_config.NumberColumn(
                             f"{ui.label(c)} ({ui.unit(c)})", format=f"%.{ui.DECIMALS.get(ui.unit(c), 2)}f") for c in categories})


@st.fragment(run_every="1s")
def live() -> None:
    if stream.status == "unauthorized":
        session.logout(t("Your session has ended: logged out, perhaps in another tab, expired, or the account was disabled. Please log in again."))
        st.rerun()
    if not stream.alive:
        # The reader thread exits after two idle minutes (a tab in the background, a sleeping
        # laptop). This fragment alone cannot restart it: a full run goes through get_stream().
        st.rerun(scope="app")
    # This fragment reruns alone, so check here too whether an admin changed the role, or the
    # language or time zone of every station; either redraws the whole page.
    api.refresh_role_if_due()
    display = api.display_settings()
    if display and (display["language"], display["time_zone"]) != (i18n.lang(), ui.zone_name()):
        i18n.use(display["language"])
        ui.use_zone(display["time_zone"])
        st.rerun(scope="app")
    ticks, digest = stream.snapshot_keyed()
    last = ticks[-1]["generated_at"] if ticks else None
    readings = stream.latest_readings()  # per server and metric, the sensor nearest its limit
    warning = ticks[-1].get("warning_percent", config["warning_percent"]) if ticks else config["warning_percent"]
    window = status_line(ticks, readings, warning, last)
    stale = last is None or datetime.fromisoformat(last.replace("Z", "+00:00")) < datetime.now(UTC) - timedelta(seconds=window)
    view = None if stale or last is None else fleet_view(last, ticks[0]["generated_at"], len(ticks), window, i18n.lang(),
                                                           ui.zone_name(), digest, tuple(ticks))
    if not ticks:
        st.info(t("Waiting for the first readings…"))
        return
    if view is None:
        st.info(t("No readings in the selected window."))
        return
    grid, alerts = st.columns([1.1, 1], gap="medium")
    with grid, st.container(border=True):
        fleet_grid(readings, warning)
    with alerts, st.container(border=True):
        alert_table(view, window)
    st.subheader(t("Nearest the limit, per metric"), anchor=False, help=t(ABOUT, warning=warning))
    st.html(tiles_html(view, st.context.theme.type or "light"))
    with st.container(horizontal=True, vertical_alignment="center"):
        st.subheader(t("Explore readings"), anchor=False)
        with st.popover(t("About trends"), icon=":material/info:", type="tertiary"):
            st.write(t("Lines show how readings change over time. Bars show the latest reading closest to its limit for each metric. "
                       "The 30, 60, 120 or 300 second window controls the line charts and alert history."))
    with st.container(border=True):
        detail_views(view, readings)


live()
