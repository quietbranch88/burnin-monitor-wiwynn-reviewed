import hashlib
import json
from datetime import date, datetime

import streamlit as st
from lib import api, session, ui
from lib.config import LABELS
from lib.i18n import t

# The API accepts record times in these years.
FIRST_DAY, LAST_DAY = date(1970, 1, 1), date(2999, 12, 31)

with st.container(horizontal=True, vertical_alignment="center"):
    st.title(t("Test records"), anchor=False)
    new_record_area = st.container(width="content")
st.caption(t("Find recorded measurements, then select a record to edit or create a new one."))
ui.tz_caption()
ui.show_notice()

try:
    categories = api.get("/records/categories")
except api.ApiError as error:
    api.show_error(error)
    ui.retry()
    st.stop()

# Keep common searches beside the table; disclose the less frequent controls separately.
toolbar = st.columns([3, 2, 2, 1], vertical_alignment="bottom")
q = toolbar[0].text_input(t("Title contains"), key="keep:records-q")
ui.keep_option("keep:records-category", ["All", *categories])
category = toolbar[1].selectbox(t("Category"), ["All", *categories], key="keep:records-category",
                                format_func=lambda c: t(c) if c == "All" else ui.label(c))
server = ui.server_id(toolbar[2].text_input(t("Server"), placeholder="S0017 or 17", key="keep:records-server"))
with toolbar[3].popover(t("Filters"), width="stretch"):
    span = ui.time_range("records", default="All time")
    row = st.columns(2)
    min_value = row[0].number_input(t("Min value"), value=None, placeholder=t("any"), key="keep:records-min")
    max_value = row[1].number_input(t("Max value"), value=None, placeholder=t("any"), key="keep:records-max")
    row = st.columns(2, vertical_alignment="bottom")
    sort_labels = {"recorded_at": "Recorded at", "value": "Value", "title": "Title", "category": "Category", "created_at": "Created at"}
    sort = row[0].selectbox(t("Sort by"), list(sort_labels), format_func=lambda s: t(sort_labels[s]), key="keep:records-sort")
    # Words that fit the sort field: newest first for times, largest first for values, Z to A for text.
    order_words = {"recorded_at": ("Newest first", "Oldest first"), "created_at": ("Newest first", "Oldest first"),
                   "value": ("Largest first", "Smallest first")}.get(sort, ("Z to A", "A to Z"))
    order = row[1].radio(t("Order"), ["desc", "asc"], horizontal=True, key="keep:records-order",
                         format_func=lambda o: t(order_words[0] if o == "desc" else order_words[1]))
    row = st.columns(2, vertical_alignment="bottom")
    page_size = row[0].selectbox(t("Rows per page"), [10, 20, 50, 100], index=1, key="keep:records-rows")
    mine = row[1].toggle(t("Only my records"), disabled=not session.can_edit(), key="keep:records-mine",
                         help=t("Records you created. Only their creator or an admin can change them."))

criteria = [t(label) + ": " + str(value) for label, value in (
    ("Title contains", q), ("Category", ui.label(category) if category != "All" else ""), ("Server", server),
) if value]
for label, value in (("Min value", min_value), ("Max value", max_value)):
    if value is not None:
        criteria.append(t(label) + ": " + str(value))
preset, dates = ui.range_choice("records")
if preset != "All time":
    chosen_range = t(preset)
    if preset == "Custom" and dates:
        chosen_range += " · " + " – ".join(str(day) for day in (dates if isinstance(dates, tuple) else (dates,)))
    criteria.append(t("Time range") + ": " + chosen_range)
if mine:
    criteria.append(t("Only my records"))
if criteria:
    st.caption(t("Active filters") + " · " + " · ".join(criteria))
st.caption(t("Sort by") + ": " + t(sort_labels[sort]) + " · " + t(order_words[0] if order == "desc" else order_words[1])
           + " · " + t("Rows per page") + ": " + str(page_size))

# A single compact command row wraps on narrow screens and stays above the table.
with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
    action_area = st.container(horizontal=True, width=440, vertical_alignment="bottom")
    count_area = st.container(width="stretch")
    page_area = st.container(width=90)
    paging_buttons = st.container(width=220)
# Leave room for the dataframe's floating toolbar above its top edge.
st.space("small")
table_area = st.container()
filters = (category, server, q, min_value, max_value, ui.range_choice("records"), sort, order, mine, page_size)
page = ui.page_input(page_area, "records", filters)

params = {"page": page, "page_size": page_size, "sort": sort, "order": order, "mine": mine, **span}
if category != "All":
    params["category"] = category
if server:
    params["server"] = server
if q:
    params["q"] = q
if min_value is not None:
    params["min_value"] = min_value
if max_value is not None:
    params["max_value"] = max_value

try:
    result = api.get("/records", params=params)
except api.ApiError as error:
    api.show_error(error)
    ui.retry()
    st.stop()

# Saving the final matching row on the last page may shrink the page count. An
# automatic page correction must retain the edit target, unlike user navigation.
pinned = st.session_state.get("records_just_created")
if pinned and not session.can_modify(pinned[0]):
    st.session_state.pop("records_just_created", None)
    pinned = None
last_page = max(result["pages"], 1)
if page > last_page and pinned and pinned[1:] == (filters, page):
    st.session_state.records_just_created = (pinned[0], filters, last_page)
ui.keep_page_in_range("records", page, result["pages"])
with paging_buttons:
    ui.pager_buttons(st, "records", page, max(result["pages"], 1))
count_area.caption(t("{total} records", total=f"{result['total']:,}") + " · "
                  + t("page {page} of {pages}", page=result["page"], pages=max(result["pages"], 1)))
frame = ui.local_frame(result["items"], "recorded_at", "created_at", "updated_at")
if frame.empty:
    table_area.info(t("No records match these filters."))
else:
    # Display names and units instead of the stored category ids; two decimals throughout.
    frame["unit"] = frame["category"].map(ui.unit)
    frame["category"] = frame["category"].map(ui.label)
    table_area.dataframe(
        frame[["server", "title", "value", "unit", "category", "recorded_at", "owner_username", "created_at", "id"]],
        hide_index=True,
        width="stretch",
        height=min(len(frame), 20) * 32 + 38,
        row_height=32,
        # Secondary audit columns stay available through the native column menu.
        column_order=["server", "title", "value", "unit", "category", "recorded_at", "owner_username"],
        column_config={
            "id": t("ID"),
            "server": st.column_config.TextColumn(t("Server"), width=70),
            "title": st.column_config.TextColumn(t("Title"), width=200),
            "value": st.column_config.NumberColumn(t("Value"), width=80, format="%.2f"),
            "unit": st.column_config.TextColumn(t("Unit"), width=55),
            "category": st.column_config.TextColumn(t("Category"), width=180),
            "recorded_at": st.column_config.DatetimeColumn(t("Recorded at"), width=175, format="YYYY-MM-DD HH:mm:ss"),
            "owner_username": st.column_config.TextColumn(t("Owner"), width=90,
                                                         help=t("Only the owner or an admin can change a record.")),
            "created_at": st.column_config.DatetimeColumn(t("Created at"), width="medium", format="YYYY-MM-DD HH:mm:ss"),
        },
    )

if not session.can_edit():
    st.caption(t("Viewers have read-only access."))
    st.stop()

# The known metrics first, then the other categories already used; a new one can be typed.
category_options = [*LABELS, *sorted(set(categories) - set(LABELS))]


def close_dialog() -> None:
    st.session_state.pop("records_dialog", None)


def open_dialog(kind: str, identifier: int | None = None) -> None:
    st.session_state.records_dialog = (kind, identifier)


def create(body: dict) -> None:
    try:
        created = api.post_once("/records", "create_record", json.dumps(body, sort_keys=True).encode(), json=body)
    except api.ApiError as error:
        # No answer, or a server-side failure: the record may exist. Keep the exact body so
        # a retry sends the same content with the same key and cannot create it twice.
        if error.status == 0 or error.status >= 500:
            st.session_state.pending_create = body
        api.show_error(error)
        return
    # Clear the pending retry only when this was it; another create must not hide its outcome.
    if st.session_state.get("pending_create") == body:
        del st.session_state.pending_create
    ui.notify(t("Created record {id}.", id=created["id"]))
    # Offered for editing even if it sorts onto another page, until the filters or page change.
    st.session_state.records_just_created = (created, filters, page)
    st.session_state.records_edit_id = created["id"]
    st.session_state.create_form_version += 1
    close_dialog()
    st.rerun()  # the table above was drawn before the create; show the new record


def request_retry() -> None:
    st.session_state.retry_create = True


@st.dialog(t("New record"), width="medium", on_dismiss=close_dialog)
def create_dialog() -> None:
    # A successful create starts a new, empty form; a refused one keeps what was typed.
    form_version = st.session_state.setdefault("create_form_version", 0)
    # Keyed, with a default fixed when this form starts: a default of "now" changing every minute
    # would replace the time the user typed.
    form_key = f"create-{form_version}"
    started = st.session_state.setdefault(f"{form_key}-now", datetime.now(ui.zone()).replace(second=0, microsecond=0))
    with st.form(form_key):
        title = st.text_input(t("Title"), placeholder=t("Line 1 station 03 - CPU temperature"), key=f"{form_key}-title")
        cols = st.columns(3)
        value = cols[0].number_input(t("Value"), value=0.0, format="%.2f", key=f"{form_key}-value")
        # A plain list: a selectbox that also takes typed values does not open on click inside a form.
        picked = cols[1].selectbox(t("Category"), category_options, index=None, format_func=ui.label,
                                   placeholder=t("Choose"), key=f"{form_key}-category")
        typed = cols[2].text_input(t("Or a new category"), placeholder="humidity", key=f"{form_key}-new-category")
        new_category = typed.strip() or picked
        cols = st.columns(3)
        new_server = cols[0].text_input(t("Server"), placeholder="S0017", key=f"{form_key}-server")
        day = cols[1].date_input(t("Recorded date ({zone})", zone=ui.zone_name()), value=started.date(), key=f"{form_key}-date",
                                 min_value=FIRST_DAY, max_value=LAST_DAY)
        at = cols[2].time_input(t("Recorded time ({zone})", zone=ui.zone_name()), value=started.time(), key=f"{form_key}-time")
        submitted = st.form_submit_button(t("Create"), type="primary")

    if submitted:
        create({"title": title, "value": value, "category": new_category or "", "server": ui.server_id(new_server),
                "recorded_at": datetime.combine(day, at, tzinfo=ui.zone()).isoformat()})
    elif st.session_state.pop("retry_create", False) and "pending_create" in st.session_state:
        create(st.session_state.pending_create)
    if pending := st.session_state.get("pending_create"):
        st.warning(t("“{title}” may not have been saved. Retrying is safe: it cannot create a duplicate.", title=pending["title"]))
        st.button(t("Retry"), type="primary", on_click=request_retry)

    if st.button(t("Close"), key="records-create-close"):
        close_dialog()
        st.rerun()


@st.dialog(t("Edit record"), width="medium", on_dismiss=close_dialog)
def edit_dialog(choice: dict) -> None:
    if not session.can_modify(choice):
        close_dialog()
        st.rerun()
    if st.session_state.pop("records_saved_notice", None) == choice["id"]:
        st.success(t("Saved record {id}.", id=choice["id"]))
    st.caption(t("Editing record #{id} · {title} · Owner: {owner}",
                 id=choice["id"], title=choice["title"], owner=choice["owner_username"]))
    if choice["id"] not in {r["id"] for r in result["items"]}:
        st.info(t("This record is outside the current results. It stays selected until you choose another record or change filters or page."))
    recorded = datetime.fromisoformat(choice["recorded_at"]).astimezone(ui.zone())
    # The time widget has minute precision; only send the timestamp if it was changed.
    recorded_minute = recorded.replace(second=0, microsecond=0)
    with st.form(f"edit-{choice['id']}"):
        title = st.text_input(t("Title"), value=choice["title"])
        cols = st.columns(3)
        value = cols[0].number_input(t("Value"), value=float(choice["value"]), format="%.2f")
        options = category_options if choice["category"] in category_options else [*category_options, choice["category"]]
        new_category = cols[1].selectbox(t("Category"), options, index=options.index(choice["category"]),
                                         format_func=ui.label)
        edited_server = cols[2].text_input(t("Server"), value=choice["server"])
        cols = st.columns(2)
        day = cols[0].date_input(t("Recorded date ({zone})", zone=ui.zone_name()), value=recorded.date(),
                                 min_value=FIRST_DAY, max_value=LAST_DAY)
        at = cols[1].time_input(t("Recorded time ({zone})", zone=ui.zone_name()), value=recorded.time().replace(microsecond=0))
        if st.form_submit_button(t("Save changes"), type="primary"):
            body = {"title": title, "value": value, "category": new_category, "server": ui.server_id(edited_server)}
            edited_at = datetime.combine(day, at.replace(second=0, microsecond=0), tzinfo=ui.zone())
            if edited_at != recorded_minute:
                body["recorded_at"] = edited_at.isoformat()
            try:
                updated = api.patch(f"/records/{choice['id']}", json=body)
                st.session_state.records_saved_notice = choice["id"]
                st.session_state.records_just_created = (updated, filters, page)
                st.rerun()
            except api.ApiError as error:
                api.show_error(error)
    # A form, so ticking the box does not rerun the page and move the keyboard focus away.
    with st.form(f"delete-{choice['id']}"):
        confirm = st.checkbox(t("I want to delete record #{id}", id=choice["id"]))
        if st.form_submit_button(t("Delete"), type="secondary"):
            if not confirm:
                st.warning(t("Tick the box first to delete the record."))
            else:
                try:
                    api.delete(f"/records/{choice['id']}")
                    ui.notify(t("Deleted record {id}.", id=choice["id"]))
                    st.session_state.pop("records_just_created", None)
                    close_dialog()
                    st.rerun()
                except api.ApiError as error:
                    api.show_error(error)

    if st.button(t("Close"), key="records-edit-close"):
        close_dialog()
        st.rerun()


choice = None
editable = [r for r in result["items"] if session.can_modify(r)]
# Creates and edits may move a record out of these results. Preserve the edit target
# across saves, but prefer the latest API list row whenever it is present.
pinned = st.session_state.get("records_just_created")
if pinned and pinned[1:] != (filters, page):
    st.session_state.pop("records_just_created", None)
elif pinned and pinned[0]["id"] not in {r["id"] for r in editable}:
    editable.insert(0, pinned[0])
if not editable:
    action_area.info(t("No record on this page that you can change. Engineers can change their own records; admins can change any."))
else:
    by_id = {r["id"]: r for r in editable}
    labels = {i: f"#{i} · {r['title']} ({r['owner_username']})" for i, r in by_id.items()}
    # Keyed, so a pick sticks. Streamlit keeps a keyed box's old labels, so the key also
    # names the labels: after a title change a new box starts on the same record.
    key = f"records-edit-{hashlib.sha256(repr(sorted(labels.items())).encode()).hexdigest()[:12]}"
    if st.session_state.get(key) not in by_id:
        last = st.session_state.get("records_edit_id")
        st.session_state[key] = last if last in by_id else next(iter(by_id))
    choice = by_id[action_area.selectbox(t("Record to edit"), list(by_id), format_func=lambda identifier: labels[identifier], key=key, width=310)]
    if pinned and pinned[0]["id"] != choice["id"]:
        st.session_state.pop("records_just_created", None)
    st.session_state.records_edit_id = choice["id"]

action_area.button(t("Edit record"), disabled=choice is None, icon=":material/edit:",
                   on_click=open_dialog, args=("edit", choice["id"] if choice else None))
new_record_area.button(t("New record"), type="primary", icon=":material/add:",
                       on_click=open_dialog, args=("create",))

active_dialog = st.session_state.get("records_dialog")
if active_dialog and active_dialog[0] == "create":
    create_dialog()
elif active_dialog and choice and active_dialog == ("edit", choice["id"]):
    edit_dialog(choice)
elif active_dialog:
    close_dialog()
