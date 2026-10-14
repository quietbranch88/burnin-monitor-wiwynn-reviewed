"""Streamlit entry point: login gate, role-based navigation, sidebar with logout.

Every page, the login page included, is registered with a fixed URL, so any URL
resolves whether or not the visitor is signed in. "/" only routes: to the login page,
or to the live page. The sidebar lists only the pages the role may use, and a
disallowed URL falls back to the live page with a notice. Pages are identified by their
URL path, which does not change with the interface language.
"""

import re
from html import escape
from urllib.parse import urlparse

import streamlit as st
from lib import api, i18n, session, ui
from lib.i18n import t

# Streamlit forgets a widget's value when its page is not shown. Writing kept values (filters)
# back here, in the script that runs on every page and before any widget is
# drawn, keeps them while the user visits other pages.
for key in [k for k in st.session_state if isinstance(k, str) and k.startswith(ui.KEEP)]:
    st.session_state[key] = st.session_state[key]
display = api.display_settings()
i18n.use(display.get("language"))
ui.use_zone(display.get("time_zone"))
# After use(), so the tab title is in the admin's language.
st.set_page_config(
    page_title=t("Burn-in Monitor"), page_icon=":material/monitor_heart:", layout="wide",
    menu_items={"About": f"**{t('Burn-in Monitor')}**\n\n{t('Server-factory burn-in test monitoring')}"},
)
session.init()
session.restore_from_cookie()
ui.style(authenticated=session.is_logged_in())

EDITOR_PAGES = {"import_data"}
ADMIN_PAGES = {"admin_users", "admin_logs", "admin_database", "admin_history", "admin_settings"}


def page_groups() -> dict[str, list]:
    # Every page has a fixed URL, the same signed in or out; "/" is only a router (below).
    return {
        t("Monitoring"): [
            st.Page("views/realtime.py", title=t("Live telemetry"), icon=":material/monitoring:", url_path="realtime"),
            st.Page("views/alerts.py", title=t("Alert events"), icon=":material/notifications_active:", url_path="alerts"),
            st.Page("views/admin_history.py", title=t("Telemetry history"), icon=":material/history:", url_path="admin_history"),
        ],
        t("Test results"): [
            st.Page("views/records.py", title=t("Test records"), icon=":material/table_rows:", url_path="records"),
            st.Page("views/import_data.py", title=t("Import test results"), icon=":material/upload_file:", url_path="import_data"),
            st.Page("views/analytics.py", title=t("Analytics"), icon=":material/analytics:", url_path="analytics"),
        ],
        t("Administration"): [
            st.Page("views/admin_users.py", title=t("Users"), icon=":material/group:", url_path="admin_users"),
            st.Page("views/admin_logs.py", title=t("System logs"), icon=":material/receipt_long:", url_path="admin_logs"),
            st.Page("views/admin_database.py", title=t("Database"), icon=":material/database:", url_path="admin_database"),
            st.Page("views/admin_settings.py", title=t("Alert and display settings"), icon=":material/tune:", url_path="admin_settings"),
        ],
    }


def home() -> None:
    """The page at "/": never shown, the router below sends the visitor on."""


def allowed(path: str) -> bool:
    if path in ADMIN_PAGES:
        return session.is_admin()
    if path in EDITOR_PAGES:
        return session.can_edit()
    return True


PATH = re.compile(r"/[A-Za-z0-9._~%/-]*")


def unknown_path() -> str | None:
    """The address the visitor asked for when it matched no page, else None.

    Streamlit sends an unknown single-level path such as /recordz to the default page ("/"),
    and st.context.url still holds what was asked for. A deeper path such as /realtime/extra
    cannot load the app at all (Streamlit's page shell loads its scripts by relative paths);
    it shows a blank page, a known limit rather than a patch to Streamlit's own files.
    """
    missing = urlparse(st.context.url or "").path
    # Only something that looks like a path, in the characters a URL path may hold: the address
    # is user input, and the notice below renders as markdown.
    if missing.strip("/") and len(missing) <= 200 and PATH.fullmatch(missing):
        return missing
    return None


groups = page_groups()
pages = [p for group in groups.values() for p in group]
by_path = {p.url_path: p for p in pages}
login_page = st.Page("views/login.py", title=t("Log in"), icon=":material/login:", url_path="login")
home_page = st.Page(home, title="Home", default=True)
navigation = st.navigation([home_page, login_page, *pages], position="hidden")

if navigation.url_path == home_page.url_path and (missing := unknown_path()):
    # st.context.url keeps the address the tab opened with, so say it once per session.
    if st.session_state.get("not_found_shown") != missing:
        st.session_state.not_found_shown = missing
        st.session_state.denied = t("Page not found: {path}", path=f"`{missing}`")

if not session.is_logged_in():
    if navigation.url_path != login_page.url_path:
        # A deep link: return to that page after logging in. Not after a logout, or the next
        # person to log in here would land on the previous user's page.
        if navigation.url_path in by_path and not st.session_state.get("logged_out_by_user"):
            st.session_state.next_page = navigation.url_path
        if message := st.session_state.pop("denied", None):
            st.session_state.flash = message
        st.switch_page(login_page)  # the login page's run clears the cookie if needed
else:
    api.refresh_role_if_due()
    if navigation.url_path in (home_page.url_path, login_page.url_path):
        st.switch_page(pages[0])
    if not allowed(navigation.url_path):
        # Keep a notice already set, such as "your role is now viewer", which explains this one.
        st.session_state.setdefault("denied", t("Your role cannot open “{page}”.", page=navigation.title))
        st.switch_page(pages[0])
    if (target := st.session_state.pop("next_page", None)) and target != navigation.url_path:
        if allowed(target):
            st.switch_page(by_path[target])
        st.session_state.denied = t("Your role cannot open “{page}”.", page=by_path[target].title)

    user = st.session_state.user
    short_labels = {"admin_settings": t("Settings"), "import_data": t("Import")}
    with st.sidebar, st.container(gap="xxsmall"):
        with st.container(key="sidebar-brand"):
            st.markdown(f":material/monitor_heart: **{t('Burn-in Monitor')}**")
        for name, group in groups.items():
            visible = [p for p in group if allowed(p.url_path)]
            if visible:
                with st.container(gap="xxsmall"):
                    st.caption(name)
                    for page in visible:
                        st.page_link(page, label=short_labels.get(page.url_path))
        with st.container(key="sidebar-account", border=True, gap="xxsmall"):
            account, sign_out = st.columns(2, vertical_alignment="center", gap="xxsmall")
            with account:
                st.html(
                    '<div class="account-identity">'
                    f'<strong>{escape(user["username"])}</strong>'
                    f'<span>{escape(i18n.role_name(user["role"]))}</span></div>'
                )
            if sign_out.button(t("Log out"), icon=":material/logout:", type="tertiary", width="stretch"):
                session.logout(t("You have been logged out everywhere."), by_user=True)
                st.rerun()
    if message := st.session_state.pop("denied", None):
        st.warning(message)

session.sync_cookie()
navigation.run()
