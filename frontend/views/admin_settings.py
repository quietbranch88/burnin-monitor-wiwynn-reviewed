"""Admin: alert thresholds, the warning level, the interface language and the plant time zone for every station."""

from datetime import datetime

import streamlit as st
from lib import api, i18n, session, ui
from lib.config import PLANT_ZONES
from lib.i18n import LANGUAGES, t

st.title(t("Alert and display settings"), anchor=False)
ui.tz_caption()
if not session.is_admin():
    st.stop()

try:
    current = api.get("/admin/settings")
except api.ApiError as error:
    api.show_error(error)
    ui.retry()
    st.stop()

if current["updated_by"]:
    changed = datetime.fromisoformat(current["updated_at"].replace("Z", "+00:00")).astimezone(ui.zone())
    st.caption(t("Last changed by {name} at {time}. Every change is in the system log.",
                 name=current["updated_by"], time=f"{changed:%Y-%m-%d %H:%M}"))

# Stable, page-local keys preserve submitted values when this rerun fetches new
# defaults. Without keep: they are discarded when the user leaves the page.
with st.container(width=960), st.form("settings", border=False):
    scope_column, save_column = st.columns([3, 1], vertical_alignment="center")
    scope_column.caption(t("Changes apply to every station. Save display preferences and alert limits together."))
    with save_column:
        saved = st.form_submit_button(t("Save settings"), icon=":material/save:", type="primary", width="stretch")
    feedback = st.container()
    with st.container(border=True, gap="xsmall"):
        st.subheader(t("Display"), anchor=False)
        language_column, plant_column = st.columns(2, gap="medium")
        with language_column:
            language = st.radio(t("Interface language for every station"), list(LANGUAGES), format_func=LANGUAGES.get,
                                index=list(LANGUAGES).index(current["language"]), horizontal=True, key="settings-language",
                                help=t("The stations are shared, so the language is set here and not per person."))
        with plant_column:
            # Keep a custom draft zone available if another admin changes the saved zone.
            zones = list(PLANT_ZONES)
            for zone in (current["time_zone"], st.session_state.get("settings-zone")):
                if zone and zone not in zones:
                    zones.append(zone)
            time_zone = st.selectbox(t("Plant"), zones, index=zones.index(current["time_zone"]), key="settings-zone",
                                     format_func=lambda z: f"{t(PLANT_ZONES.get(z, str(z)))} · {z}",
                                     help=t("Every station shows and enters times in this plant's time zone. Stored times stay in UTC."))
    with st.container(border=True, gap="xsmall"):
        st.subheader(t("Alerts"), anchor=False)
        st.caption(t("Thresholds apply to every sensor of the metric from the next second. Readings already stored keep "
                     "the threshold they were compared with."))
        warning = st.slider(t("Warning from"), min_value=50, max_value=99, value=current["warning_percent"], format="%d%%",
                            key="settings-warning",
                            help=t("A reading this close to its limit is a warning; past the limit it is an alert."))
        thresholds = {}
        for index, item in enumerate(current["thresholds"]):
            if index % 2 == 0:  # a row per pair, so a phone shows them in reading order
                columns = st.columns(2)
            category, unit = item["category"], ui.unit(item["category"])
            side = t("alert below") if item["alert_below"] else t("alert above")
            thresholds[category] = columns[index % 2].number_input(
                f"{ui.label(category)} ({unit}) · {side}" if unit else f"{ui.label(category)} · {side}",
                min_value=0.1, max_value=1e15, value=float(item["threshold"]), step=1.0 if unit == "°C" else 100.0,
                format="%.1f" if unit == "°C" else "%.0f",
                key=f"settings-threshold-{category}",
                help=t("Default from .env: {value}", value=ui.format_value(item["default"], category)),
            )
    st.caption(t("Thresholds apply from the next second. Live pages refresh display preferences within 30 seconds; "
                 "other pages update on the next interaction or reload."))

if st.session_state.pop("settings-save-success", False) and not saved:
    feedback.success(t("Settings saved."))

if saved:
    change = {"warning_percent": warning, "language": language, "time_zone": time_zone,
              "thresholds": {c: v for c, v in thresholds.items()
                             if v != next(i["threshold"] for i in current["thresholds"] if i["category"] == c)}}
    try:
        api.patch("/admin/settings", json=change)
        api.display_changed()  # this session now; other sessions on their next page/fragment run
        i18n.use(language)  # so the notice below is already in the new language
        ui.use_zone(time_zone)
        st.session_state["settings-save-success"] = True
        st.rerun()
    except api.ApiError as error:
        with feedback:
            api.show_error(error)
