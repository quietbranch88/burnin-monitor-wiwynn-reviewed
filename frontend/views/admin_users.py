from datetime import UTC, datetime, timedelta

import streamlit as st
from lib import api, i18n, session, ui
from lib.i18n import t

heading = st.columns([5, 1.5], vertical_alignment="bottom")
heading[0].title(t("Users"), anchor=False)
st.caption(t("Manage accounts, access and sign-in credentials."))
ui.show_notice()
if not session.is_admin():
    st.stop()

ROLES_HELP = t("Viewer: read only. Engineer: create and import, change own records. Admin: everything, including this module.")
ROLES = ["admin", "user", "viewer"]

directory = st.container()

# An admin can create an account with any role; self-registration on the login page gives Viewer.
# A new form after a success clears the fields; after a refusal (409, 422) they stay for correction.
version = st.session_state.setdefault("create_user_version", 0)
with heading[1], st.popover(t("Create a user"), icon=":material/person_add:", width="stretch"), st.form(f"create-user-{version}"):
    st.caption(t("Create an account and choose its access level."))
    cols = st.columns(2)
    new_name = cols[0].text_input(t("Username"), help=t("3-50 letters, digits, '.', '_' or '-'"))
    new_email = cols[1].text_input(t("Email"))
    new_password = cols[0].text_input(t("Password"), type="password", help=t("8-64 characters, with at least one letter and one digit"))
    new_role = cols[1].selectbox(t("Role"), ROLES, index=ROLES.index("user"), format_func=i18n.role_name, help=ROLES_HELP)
    if st.form_submit_button(t("Create account"), type="primary"):
        try:
            api.post("/admin/users", json={"username": new_name.strip(), "email": new_email.strip(),
                                           "password": new_password, "role": new_role})
            ui.notify(t("Created {name}.", name=new_name.strip()))
            st.session_state.create_user_version += 1
            st.rerun()
        except api.ApiError as error:
            api.show_error(error)
with directory:
    toolbar = st.columns([4, 1, 2], vertical_alignment="bottom")
    search = toolbar[0].text_input(t("Find a user"), placeholder=t("Username or email"), key="keep:users-q").strip()
    page = ui.page_input(toolbar[1], "users", search)
    try:
        result = api.get("/admin/users", params={"page": page, "page_size": 50, **({"q": search} if search else {})})
    except api.ApiError as error:
        api.show_error(error)
        ui.retry()
        st.stop()

    ui.keep_page_in_range("users", page, result["pages"])
    ui.pager_buttons(toolbar[2], "users", page, max(result["pages"], 1))
    st.caption(t("{total} accounts · Page {page} of {pages}", total=f"{result['total']:,}",
                 page=page, pages=max(result["pages"], 1)))
    selected_area = st.container()
    if result["items"]:
        users_frame = ui.local_frame(result["items"], "created_at")
        users_frame["role"] = users_frame["role"].map(i18n.role_name)
        st.dataframe(
            users_frame, hide_index=True, width="stretch", row_height=40,
            column_order=["username", "email", "role", "is_active", "created_at"],
            column_config={
                "username": st.column_config.TextColumn(t("Username"), width=150),
                "email": st.column_config.TextColumn(t("Email"), width=260),
                "role": st.column_config.TextColumn(t("Role"), width=140),
                "is_active": st.column_config.CheckboxColumn(t("Active"), width=75),
                "created_at": st.column_config.DatetimeColumn(t("Created at ({zone})", zone=ui.zone_name()),
                                                             format="YYYY-MM-DD HH:mm", width=200),
                "id": None,
            },
        )
    else:
        st.info(t("No users on this page."))


if not result["items"]:
    st.stop()

by_id = {u["id"]: u for u in result["items"]}
if st.session_state.get("admin-users-selected") not in by_id:  # e.g. after a search or another page
    # A search that found exactly one account selects it; otherwise nothing is selected.
    st.session_state["admin-users-selected"] = next(iter(by_id)) if search and len(by_id) == 1 else None
# Options are ids, so the selection survives a save. The label leaves out the role: Streamlit
# keeps a keyed selectbox's old label after a rerun; the role is in the table and the form.
# Nothing is selected at first, so an admin does not start out editing their own account.
selection_controls = selected_area.columns([3, 1.5, 1.5], vertical_alignment="bottom")
selected = selection_controls[0].selectbox(t("User"), list(by_id), index=None, placeholder=t("Choose a user"),
                        help=t("Choose an account from the current page."),
                        format_func=lambda i: by_id[i]["username"], key="admin-users-selected")
if selected is None:
    selected_area.caption(t("Select an account to change access or reset its password."))
    st.stop()
user = by_id[selected]


@st.cache_data(ttl=30, show_spinner=False)
def recent_failed_logins(user_id: int) -> int:
    """Wrong passwords and disabled-account attempts in the last 24 h; cached, so a save or a
    page change does not scan the log again. An error is raised, so a failure is not cached."""
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    return api.get("/admin/logs", params={"user_id": user_id, "logger": "app.users", "q": "Login failed",
                                          "start": since, "page_size": 1}, timeout=3)["total"]


failed: int | None
try:
    failed = recent_failed_logins(user["id"])
except api.ApiError:
    failed = None  # the log could not be read in time; the page works without it
identity = f"{user['email']} · {i18n.role_name(user['role'])}"
if failed == 0:
    identity += f" · {t('No failed logins in the last 24 hours.')}"
selected_area.caption(identity)
if failed:
    selected_area.warning(t("{count} failed logins in the last 24 hours; see System logs for the times.", count=failed), icon=":material/lock:")


def close_user_action() -> None:
    st.session_state.pop("admin-user-action", None)


@st.dialog(t("Manage access"), on_dismiss=close_user_action)
def manage_access(account: dict) -> None:
    if account["id"] == (st.session_state.get("user") or {}).get("id"):
        st.info(t("You cannot change your own role or deactivate your own account."))
        return
    st.caption(account["username"])
    with st.form(f"user-{account['id']}"):
        role = st.selectbox(t("Role"), ROLES, index=ROLES.index(account["role"]), format_func=i18n.role_name,
                            help=ROLES_HELP, key=f"access-role-{account['id']}")
        active = st.checkbox(t("Active"), value=account["is_active"], key=f"access-active-{account['id']}")
        if st.form_submit_button(t("Save access"), type="primary"):
            try:
                api.patch(f"/admin/users/{account['id']}", json={"role": role, "is_active": active})
                ui.notify(t("Saved {name}. The change applies to their next request.", name=account["username"]))
                close_user_action()
                st.rerun()
            except api.ApiError as error:
                api.show_error(error)


@st.dialog(t("Reset password"), on_dismiss=close_user_action)
def reset_password(account: dict) -> None:
    st.caption(account["username"])
    st.caption(t("Setting a new password signs this account out of all sessions."))
    st.caption(t("8-64 characters, with at least one letter and one digit"))
    with st.form(f"password-{account['id']}"):
        password = st.text_input(t("New password"), type="password", key=f"reset-password-{account['id']}")
        password_error = st.empty()
        confirmation = st.text_input(t("Confirm password"), type="password", key=f"reset-confirm-{account['id']}")
        confirmation_error = st.empty()
        if st.form_submit_button(t("Set new password"), type="primary"):
            # Basic format follows schemas.auth.PasswordRules; weak-password policy remains server-owned.
            problem = None
            if not password:
                problem = "Enter a new password."
            elif not 8 <= len(password) <= 64:
                problem = "Use 8–64 characters."
            elif not (any(c.isalpha() for c in password) and any(c.isdigit() for c in password)):
                problem = "Include at least one letter and one digit."
            elif len(password.encode("utf-8")) > 72:
                problem = "This password is too long. Try fewer characters."
            if problem:
                password_error.error(t(problem))
            if not confirmation:
                confirmation_error.error(t("Confirm the new password."))
            elif confirmation != password:
                confirmation_error.error(t("Passwords do not match."))
            if problem or not confirmation or confirmation != password:
                return
            try:
                api.put(f"/admin/users/{account['id']}/password", json={"password": password})
                ui.notify(t("New password set for {name}; their open sessions have ended.", name=account["username"]))
                close_user_action()
                st.rerun()
            except api.ApiError as error:
                api.show_error(error)


is_self = user["id"] == (st.session_state.get("user") or {}).get("id")
if selection_controls[1].button(t("Manage access"), icon=":material/manage_accounts:", disabled=is_self, width="stretch"):
    st.session_state["admin-user-action"] = ("access", selected)
if selection_controls[2].button(t("Reset password"), icon=":material/key:", width="stretch"):
    st.session_state["admin-user-action"] = ("password", selected)
if is_self:
    selected_area.caption(t("You cannot change your own role or deactivate your own account."))
action = st.session_state.get("admin-user-action")
if action and action[1] != selected:
    close_user_action()
elif action:
    if action[0] == "access" and not is_self:
        manage_access(user)
    elif action[0] == "password":
        reset_password(user)
