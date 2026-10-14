import streamlit as st
from lib import api, session, ui
from lib.i18n import t

st.title(t("System logs"), anchor=False)
st.caption(t("Review account activity, system events, and API requests."))
ui.tz_caption()
if not session.is_admin():
    st.stop()

# Read before drawing: failed-login mode controls the message, level and kind.
failed_logins_on = st.session_state.get("keep:logs-failed-logins", False)
KINDS = {"events": "Changes and events", "requests": "API requests", "all": "Everything"}
# Changes and events first: who changed a role, a record or a setting, and logins. Every API
# call is a request line, which would bury them.
toolbar = st.columns([3, 2, 1], vertical_alignment="bottom")
q = toolbar[0].text_input(t("Message contains"), key="keep:logs-q", disabled=failed_logins_on, max_chars=200)
with toolbar[1]:
    span = ui.time_range("logs", default="Last 24 hours")
with toolbar[2].popover(t("Filters"), width="stretch"):
    row = st.columns(2)
    account = row[1].text_input(t("Done by"), placeholder="admin", key="keep:logs-user",
                               help=t("Lines of what this account did. To see what happened to an account, search the message for its name."))
    level = row[0].selectbox(t("Level"), ["All", "INFO", "WARNING", "ERROR", "CRITICAL"], index=0,
                             key="keep:logs-level", format_func=lambda v: t(v) if v == "All" else v,
                             disabled=failed_logins_on)
    failed_logins = st.toggle(t("Failed logins only"), key="keep:logs-failed-logins")

criteria = [t(label) + ": " + str(value) for label, value in (
    ("Done by", account.strip()), ("Level", level if level != "All" and not failed_logins else ""),
) if value]
if failed_logins:
    criteria.append(t("Failed logins only"))
if criteria:
    st.caption(t("Active filters") + " · " + " · ".join(criteria))

# Draw paging above the selector; fill its page widget after reading the selected kind.
navigation = st.columns([3, 1, 1, 2], vertical_alignment="bottom")
# Older sessions may retain an empty choice from when this control was optional.
if "keep:logs-kind" in st.session_state and st.session_state["keep:logs-kind"] is None:
    st.session_state["keep:logs-kind"] = "events"
kind = st.segmented_control(t("Saved view") if failed_logins_on else t("Show"), list(KINDS), default="events",
                            key="keep:logs-kind", format_func=lambda k: t(KINDS[k]), required=True, disabled=failed_logins_on,
                            help=t("Failed-login mode shows events. Turn it off to return to this view.") if failed_logins_on else None)
results = st.container()
summary = navigation[0].container()
page_size = navigation[1].selectbox(t("Rows per page"), [25, 50, 100], index=1, key="keep:logs-rows")
page = ui.page_input(navigation[2], "logs", (kind, level, q, account, failed_logins,
                                           ui.range_choice("logs"), page_size))

params = {"page": page, "page_size": page_size, **span}
if kind != "all":
    params["kind"] = kind
if level != "All":
    params["level"] = level
if q:
    params["q"] = q

if failed_logins:
    params |= {"kind": "events", "logger": "app.users", "q": "Login failed", "level": "WARNING"}
if account := account.strip():
    try:
        matches = api.get("/admin/users", params={"q": account, "page_size": 100})["items"]
    except api.ApiError as error:
        api.show_error(error)
        st.stop()
    # Any case; part of a name works when it matches one account only.
    exact = [u for u in matches if u["username"].lower() == account.lower()]
    user_id = (exact or matches)[0]["id"] if exact or len(matches) == 1 else None
    if user_id is None:
        st.info(t("No account named {name}.", name=account))
        st.stop()
    params["user_id"] = user_id

try:
    result = api.get("/admin/logs", params=params)
except api.ApiError as error:
    api.show_error(error)
    ui.retry()
    st.stop()

ui.keep_page_in_range("logs", page, result["pages"])
ui.pager_buttons(navigation[3], "logs", page, max(result["pages"], 1))
summary.markdown("**" + t("{total} entries", total=f"{result['total']:,}") + "**")
summary.caption(t("page {page} of {pages}", page=result["page"], pages=max(result["pages"], 1)))
summary.caption(t("Newest first. Logs are written in batches every few seconds."))
with results:
    frame = ui.local_frame(result["items"], "created_at")
    if frame.empty:
        st.info(t("No log entries match."))
        st.caption(t("Try a wider time range or adjust the filters."))
    else:
        # The message already says method, path, status and duration for request lines.
        frame["username"] = frame["username"].fillna(t("No linked account")).replace("", t("No linked account"))
        st.dataframe(
            frame[["created_at", "level", "message", "username"]],
            hide_index=True,
            width="stretch",
            column_order=["created_at", "level", "message", "username"],
            column_config={
                "created_at": st.column_config.DatetimeColumn(t("Time ({zone})", zone=ui.zone_name()),
                                                              format="YYYY-MM-DD HH:mm:ss", width="medium"),
                "level": st.column_config.TextColumn(t("Level"), width="small"),
                "message": st.column_config.TextColumn(t("Message"), width="large"),
                "username": st.column_config.TextColumn(t("Done by"), width="medium",
                                                       help=t("Public requests and background events may not be linked to an account.")),
            },
        )
