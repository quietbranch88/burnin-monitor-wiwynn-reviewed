"""Login state: st.session_state for the current tab, a cookie so a reload keeps the login.

Streamlit has no server-side API for setting cookies, so a small script writes
it in the browser. The cookie holds the JWT, expires with it and is SameSite=Strict.
"""

import base64
import json
import re
import time

import streamlit as st

from lib import api
from lib.i18n import LANG_KEY, t

KEYS = ("token", "user", "expires_at", "stream")
COOKIE = "burnin_token"
# The shape of a JWT. The cookie is user input: only a value of this shape may be written
# into the page script below, anything else is cleared without being echoed.
JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def init() -> None:
    for key in KEYS:
        st.session_state.setdefault(key, None)


def _token_expiry(token: str) -> float | None:
    """Read `exp` from the JWT payload; the API still verifies the signature."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, ValueError, KeyError, TypeError):  # the cookie is user input; any shape can arrive
        return None


def _remember(token: str, user: dict, expires_at: float) -> None:
    st.session_state.token = token
    st.session_state.user = user
    # Log out a minute early so a request never races the expiry.
    st.session_state.expires_at = expires_at - 60


def restore_from_cookie() -> None:
    """After a reload, rebuild the session from the cookie if its token is still valid."""
    # st.context.cookies is captured when the tab connects and never refreshes, so
    # after a logout in this tab it still holds the old token: do not restore then.
    if st.session_state.get("token") or st.session_state.get("logged_out"):
        return
    token = st.context.cookies.get(COOKIE)
    if not token:
        return
    expiry = _token_expiry(token)
    # The shape is checked here, before the API is even asked: the API checks it too, but this
    # value will be written into the page script below, and only a token may ever get there.
    if not JWT.fullmatch(token) or expiry is None or expiry - 60 <= time.time():
        st.session_state.clear_cookie_token = token
        return
    try:
        # A short timeout: while the API is down this runs on every rerun of the login page.
        user = api.get("/auth/me", auth=False, headers={"Authorization": f"Bearer {token}"}, timeout=3)
    except api.ApiError as error:
        if error.status in (401, 403):
            st.session_state.clear_cookie_token = token  # the token itself was refused
        else:
            # The API or its database is down: keep the cookie, so the next run restores the login.
            st.session_state.flash = t("The service is unavailable right now. Your login is kept; try again shortly.")
        return
    _remember(token, user, expiry)


def login(username: str, password: str) -> None:
    body = api.post("/auth/login", auth=False, data={"username": username, "password": password})
    _remember(body["access_token"], body["user"], time.time() + body["expires_in"])
    st.session_state.logged_out = False
    st.session_state.pop("clear_cookie_token", None)


def logout(message: str | None = None, *, by_user: bool = False) -> None:
    if by_user and st.session_state.get("token"):
        # Ends this user's sessions everywhere: other tabs and devices get 401 on their next request.
        try:
            # auth=False: a 401 here means the sessions already ended, not "log out again".
            api.post("/auth/logout", auth=False, timeout=3,
                     headers={"Authorization": f"Bearer {st.session_state.token}"})
        except api.ApiError as error:
            if error.status != 401:
                message = t("Logged out in this tab. The service did not answer, so other open sessions may stay signed in.")
    old_token = st.session_state.get("token")
    stream = st.session_state.get("stream")
    if stream is not None:
        stream.stop()
    # Clear everything the pages stored (prepared reports, pending retries, filters), so the
    # next person to log in in this tab starts clean.
    # The language, the plant's time zone and the "page not found" note (tied to the address the
    # tab opened with) belong to the tab, not to the user who just left.
    kept = {k: st.session_state[k] for k in (LANG_KEY, "display_zone", "not_found_shown") if k in st.session_state}
    for key in list(st.session_state):
        del st.session_state[key]
    init()
    st.session_state.update(kept)
    st.session_state.flash = message
    st.session_state.logged_out = True
    # Only a logout the user chose drops a deep link; after an expired session the next
    # login returns to the page the user was on.
    st.session_state.logged_out_by_user = by_user
    st.session_state.clear_cookie_token = old_token


def sync_cookie() -> None:
    """Make the browser's login cookie match the session, on every full run.

    Signed in, the cookie is written; after a logout or a refused token, it is cleared.
    Writing it only once proved unreliable: right after a login on the default page the
    script element could be replaced before the browser ran it, and the next reload
    logged the user out. Repeating it costs one hidden element per page run.

    All tabs share the cookie, so the script checks what it holds first. A token is
    written only over an older one (by its fractional `iat`). Equal timestamps do
    not establish which distinct login is newer, so the current cookie wins a tie.
    A clear removes only the token this tab ended, never another tab's newer login.
    """
    if (token := st.session_state.get("token")) and JWT.fullmatch(token):
        max_age = max(0, int(st.session_state.expires_at + 60 - time.time()))  # until the token expires
        cookie = f"{COOKIE}={token}; Path=/; Max-Age={max_age}; SameSite=Strict"
        action = f"if (!current || current === mine || issued(mine) > issued(current)) document.cookie = {json.dumps(cookie)} + secure;"
    elif token := st.session_state.get("clear_cookie_token"):
        cookie = f"{COOKIE}=; Path=/; Max-Age=0; SameSite=Strict"
        if JWT.fullmatch(token):
            action = f"if (current === mine) document.cookie = {json.dumps(cookie)} + secure;"
        else:
            # Not a token at all (a planted or corrupt cookie): never put it in the page; clear unconditionally.
            token = ""
            action = f"document.cookie = {json.dumps(cookie)} + secure;"
    else:
        return
    # Inside a function: every page run adds a new script, and top-level `const`s would
    # collide with the previous run's in the page's shared global scope.
    script = (
        "(() => {"
        f"const mine = {json.dumps(token)};"
        f"const current = (document.cookie.split('; ').find(c => c.startsWith('{COOKIE}=')) || '').slice({len(COOKIE) + 1});"
        # Behind HTTPS the cookie is never sent over plain HTTP; the local demo runs on HTTP.
        "const secure = location.protocol === 'https:' ? '; Secure' : '';"
        "const issued = t => { try { return JSON.parse(atob(t.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))).iat || 0; }"
        " catch (e) { return 0; } };"
        + action
        + "})();"
    )
    st.html(f"<script>{script}</script>", unsafe_allow_javascript=True)


def is_logged_in() -> bool:
    expires_at = st.session_state.get("expires_at")
    if st.session_state.get("token") and expires_at and time.time() >= expires_at:
        logout(t("Your session has expired. Please log in again."))
    return bool(st.session_state.get("token"))


def role() -> str | None:
    user = st.session_state.get("user")
    return user["role"] if user else None


def can_edit() -> bool:
    return role() in ("admin", "user")


def is_admin() -> bool:
    return role() == "admin"


def can_modify(record: dict) -> bool:
    user = st.session_state.get("user") or {}
    return role() == "admin" or (role() == "user" and record["owner_id"] == user.get("id"))
