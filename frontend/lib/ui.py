from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from lib.config import LABELS, UNITS
from lib.i18n import t

PRESETS = {
    "Last 15 minutes": timedelta(minutes=15),
    "Last 1 hour": timedelta(hours=1),
    "Last 24 hours": timedelta(days=1),
    "Today": "today",
    "Yesterday": "yesterday",
    "Last 7 days": timedelta(days=7),
    "Last 30 days": timedelta(days=30),
    "All time": None,
}


def _days(first, last) -> dict[str, str]:
    """Whole days in the display zone, sent to the API with their offset."""
    start = datetime.combine(first, time.min, tzinfo=zone())
    end = datetime.combine(last, time.max, tzinfo=zone())
    return {"start": start.isoformat(), "end": end.isoformat()}


ZONE_KEY = "display_zone"


def use_zone(name: str | None) -> None:
    """Show this session's times in the admin's plant zone; keep the last one if it could not be read."""
    if name:
        st.session_state[ZONE_KEY] = name


def zone_name() -> str:
    return st.session_state.get(ZONE_KEY, "UTC")  # until the admin's choice has been read


def zone() -> ZoneInfo:
    return ZoneInfo(zone_name())


KEEP = "keep:"  # widget keys with this prefix keep their value across pages (see app.py)


def keep_option(key: str, options: list) -> None:
    """Forget a kept choice that is no longer offered, e.g. a category that has gone."""
    if key in st.session_state and st.session_state[key] not in options:
        del st.session_state[key]


def time_range(key: str, default: str = "Last 30 days", disabled: bool = False, help: str | None = None) -> dict[str, str]:
    """Preset or custom range, returned as ISO 8601 UTC query parameters."""
    options = [*PRESETS, "Custom"]
    choice = st.selectbox(t("Time range"), options, index=options.index(default), key=f"{KEEP}{key}-preset", format_func=t,
                          disabled=disabled, help=help)
    picked = None
    if choice == "Custom":
        today = datetime.now(UTC).astimezone(zone()).date()
        picked = st.date_input(t("Dates ({zone})", zone=zone_name()), value=(today - timedelta(days=7), today), key=f"{KEEP}{key}-dates")
    return resolve_time_range(choice, picked)


def resolve_time_range(choice: str, picked=None) -> dict[str, str]:
    """Resolve selected dates at request time without rendering or rerunning widgets."""
    now = datetime.now(UTC)
    if choice == "Custom":
        days = [d for d in (picked if isinstance(picked, tuple) else (picked,)) if d]
        # One date picked so far (the second click is still to come): that day alone.
        return _days(days[0], days[-1]) if days else {}
    span = PRESETS[choice]
    today = now.astimezone(zone()).date()
    if span == "today":
        return _days(today, today)
    if span == "yesterday":
        return _days(today - timedelta(days=1), today - timedelta(days=1))
    return {"start": (now - span).isoformat()} if isinstance(span, timedelta) else {}


def range_choice(key: str) -> tuple:
    """What the user picked in `time_range`, stable across reruns.

    The computed start time moves on every rerun, so it cannot tell whether the filters
    changed; the widget values can.
    """
    return st.session_state.get(f"{KEEP}{key}-preset"), st.session_state.get(f"{KEEP}{key}-dates")


def page_input(container, key: str, filters: object) -> int:
    """A page number that goes back to 1 when the filters change."""
    page_key, marker = f"{KEEP}{key}-page", f"{key}-filters"
    if st.session_state.get(marker) != filters:
        st.session_state[marker] = filters
        st.session_state[page_key] = 1
    if (last := st.session_state.pop(f"{key}-last-page", None)) is not None:
        st.session_state[page_key] = last
    return int(container.number_input(t("Page"), min_value=1, step=1, key=page_key))


def pager_buttons(container, key: str, page: int, pages: int) -> None:
    """Previous / next buttons beside the page number."""
    page_key = f"{KEEP}{key}-page"

    def go(to: int) -> None:
        st.session_state[page_key] = to

    left, right = container.columns(2)
    left.button(t("Previous"), key=f"{key}-previous", disabled=page <= 1, on_click=go, args=(page - 1,),
                icon=":material/chevron_left:", width="stretch")
    right.button(t("Next"), key=f"{key}-next", disabled=page >= pages, on_click=go, args=(page + 1,),
                 icon=":material/chevron_right:", width="stretch")


def keep_page_in_range(key: str, page: int, pages: int) -> None:
    """Past the last page, show the last page instead of an empty one; page 1 when nothing is left."""
    last = max(pages, 1)
    if page > last:
        st.session_state[f"{key}-last-page"] = last
        st.rerun()


def notify(message: str) -> None:
    """Show a success message after the next rerun."""
    st.session_state.notice = message


def retry() -> None:
    """After a failed page load: the next click loads again instead of leaving the error in place."""
    if st.button(t("Try again"), icon=":material/refresh:"):
        st.rerun()


def show_notice() -> None:
    if message := st.session_state.pop("notice", None):
        st.success(message)


def to_display(series: pd.Series) -> pd.Series:
    """UTC timestamps from the API -> naive wall-clock times in the display zone."""
    return pd.to_datetime(series, utc=True).dt.tz_convert(zone()).dt.tz_localize(None)


def local_frame(rows: list[dict], *columns: str) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for column in columns:
        if column in frame:
            frame[column] = to_display(frame[column])
    return frame


# Streamlit 1.64 has no theme controls for these shell gaps. Its sidebar header
# remains in flow above the brand, preserving the collapse button's own space.
# If Streamlit renames these test ids, only the default spacing returns.
APPEARANCE_ICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E"
    "%3Cg fill='none' stroke='black' stroke-width='1.7' stroke-linecap='round'%3E%3Ccircle cx='12' cy='12' r='4'/%3E"
    "%3Cpath d='M12 2v2m0 16v2M2 12h2m16 0h2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42'/%3E"
    "%3C/g%3E%3Cpath d='M12 8a4 4 0 0 1 0 8Z' fill='black'/%3E%3C/svg%3E"
)
STYLE = """<style>
/* Appearance is the primary action in the native minimal menu. Keep its button,
   focus handling and theme preferences; replace only the decorative icon. */
[data-testid="stMainMenuButton"]:has(svg) { position: relative; }
[data-testid="stMainMenuButton"] > svg { opacity: 0; }
[data-testid="stMainMenuButton"]:has(svg)::after {
    content: "";
    position: absolute;
    inset: 0;
    margin: auto;
    width: 1.375rem;
    height: 1.375rem;
    pointer-events: none;
    background-color: currentColor;
    mask: url("__APPEARANCE_ICON__") center / contain no-repeat;
}
[data-testid="stMainBlockContainer"] { padding-top: 2.5rem; }
[data-testid="stSidebar"][aria-expanded="true"] { min-width: 16rem; max-width: 16rem; }
[data-testid="stSidebar"] [data-testid="stSidebarHeader"] { height: 2rem; margin-bottom: 0; }
[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] { padding-top: 0; padding-bottom: 0.5rem; }
[data-testid="stSidebar"] .st-key-sidebar-brand p { font-size: 1.125rem; line-height: 1.4; margin: 0; }
[data-testid="stSidebar"] .st-key-sidebar-brand [data-testid="stMarkdownContainer"] { margin-bottom: 0; }
[data-testid="stSidebar"] [data-testid="stPageLink"] p { font-size: 0.875rem; }
[data-testid="stSidebar"] .st-key-sidebar-account { margin-top: 0.5rem; }
[data-testid="stSidebar"] .st-key-sidebar-account .account-identity { display: flex; flex-direction: column; gap: 2px; }
[data-testid="stSidebar"] .st-key-sidebar-account .account-identity strong { font-size: 0.875rem; line-height: 1.4; overflow-wrap: anywhere; }
[data-testid="stSidebar"] .st-key-sidebar-account .account-identity span { font-size: 0.75rem; line-height: 1.4; opacity: 0.72; }
[data-testid="stSidebar"] .st-key-sidebar-account button p { font-size: 0.8125rem; }
[data-testid="stSidebar"] hr { margin: 0.5rem 0; }
</style>""".replace("__APPEARANCE_ICON__", APPEARANCE_ICON)


def style(*, authenticated: bool = True) -> None:
    st.html(STYLE)
    if not authenticated:
        st.html('''<style>
        [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display: none; }
        </style>''')


def tz_caption() -> None:
    st.caption(t("Times are shown in {zone}.", zone=zone_name()))


# Decimal places per unit, so values such as RPM do not overflow a metric card.
DECIMALS = {"°C": 1, "W": 0, "RPM": 0}
# Chart palette: blues for values, orange for maxima, red for limits.
# Darkened just enough for 3:1 contrast on white (WCAG for graphics): the brand #00A2DF and
# #FFAF03 are 2.9:1 and 1.8:1.
BLUE, DEEP_BLUE, ORANGE, RED, GREY = "#0093CC", "#006C92", "#B97F00", "#DA0101", "#6B7C88"
SERIES_COLORS = {"value": DEEP_BLUE, "threshold": RED, "average": DEEP_BLUE, "minimum": BLUE, "maximum": ORANGE}


def label(category: str) -> str:
    """Display name of a metric, e.g. cpu_temp -> CPU temp; other categories as they are."""
    return t(LABELS[category]) if category in LABELS else category


def unit(category: str) -> str:
    return UNITS.get(category, "")


LOAD_CAP = 999.0  # a stopped fan; also keeps the charts' JSON free of Infinity


def load(value: float, threshold: float, below: bool) -> float:
    """How close a reading is to its limit, in percent: 100 is the limit (same rule as the API)."""
    if below:
        return min(threshold / value * 100, LOAD_CAP) if value > 0 else LOAD_CAP
    return min(value / threshold * 100, LOAD_CAP)


def server_id(typed: str) -> str:
    """A server as typed: "17" means S0017, the id the live page shows; anything else is kept."""
    typed = typed.strip()
    return f"S{int(typed):04d}" if typed.isdigit() and len(typed) <= 4 else typed


def format_value(value: float, category: str) -> str:
    unit = UNITS.get(category, "")
    return f"{value:,.{DECIMALS.get(unit, 2)}f} {unit}".strip()


def format_number(value: float | None) -> str:
    """Rounded like the tables in the browser (JavaScript toFixed): from the exact binary value,
    halves away from zero. So -3.125 shows as -3.13 and 2.675 (2.67499...) as 2.67 in both."""
    if value is None:
        return "–"
    places = Decimal(1) if abs(value) >= 1000 else Decimal("0.01")
    return f"{Decimal(value).quantize(places, rounding=ROUND_HALF_UP):,}"


def time_chart(frame: pd.DataFrame, x: str, columns: list[str], *, time_format: str, height: int = 200) -> None:
    """Line chart over display-zone wall-clock times with a 24-hour axis.

    The frame holds naive wall-clock times. Vega reads naive times as browser-local
    and formats them back in local time, so the wall clock is shown unchanged in
    any browser zone. labelExpr forces the format over Vega's multi-level default.
    """
    long = frame.reset_index()[[x, *columns]].melt(id_vars=[x], var_name="series", value_name="reading")
    long["series"] = long["series"].map(t)  # legend in the chosen language
    colors = [SERIES_COLORS.get(c, GREY) for c in columns]
    chart = (
        alt.Chart(long)
        .mark_line()
        .encode(
            x=alt.X(f"{x}:T", title=None,
                    axis=alt.Axis(labelExpr=f"timeFormat(datum.value, '{time_format}')", labelOverlap=True)),
            y=alt.Y("reading:Q", title=None, scale=alt.Scale(zero=False)),
            color=alt.Color("series:N", scale=alt.Scale(domain=[t(c) for c in columns], range=colors),
                            legend=alt.Legend(orient="bottom", title=None)),
            tooltip=[alt.Tooltip(f"{x}:T", title=t("time"), format="%Y-%m-%d %H:%M:%S"),
                     alt.Tooltip("series:N", title=t("Line")), alt.Tooltip("reading:Q", title=t("value"), format=",.2f")],
        )
        .properties(height=height)
    )
    st.altair_chart(chart, width="stretch")


def bar_chart(frame: pd.DataFrame, x: str, y: str, *, title: str, height: int = 220) -> None:
    """Horizontal bars with a titled value axis."""
    chart = (
        alt.Chart(frame)
        .mark_bar()
        .encode(x=alt.X(f"{x}:Q", title=title), y=alt.Y(f"{y}:N", title=None, sort=None),
                tooltip=[alt.Tooltip(f"{y}:N", title=t("category")), alt.Tooltip(f"{x}:Q", title=title, format=",")])
        .properties(height=height)
    )
    st.altair_chart(chart, width="stretch")
