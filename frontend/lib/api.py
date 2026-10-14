"""Thin HTTP client for the FastAPI backend. Session expiry sends the user back to login."""

import hashlib
import re
import time
import uuid
from typing import Any

import requests
import streamlit as st

from lib import i18n
from lib.config import API_BASE_URL, LABELS
from lib.i18n import t


class ApiError(Exception):
    def __init__(self, status: int, detail: str, errors: list | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.errors = errors or []


def call(method: str, path: str, *, auth: bool = True, raw: bool = False, with_headers: bool = False,
         timeout: float = 15, **kwargs) -> Any:
    headers = kwargs.pop("headers", {})
    if auth and st.session_state.get("token"):
        headers["Authorization"] = f"Bearer {st.session_state.token}"
    try:
        response = requests.request(method, f"{API_BASE_URL}{path}", headers=headers, timeout=timeout, **kwargs)
    except requests.Timeout as exc:
        raise ApiError(0, t("The API did not answer within {seconds} s; it may be busy. Try again.", seconds=f"{timeout:.0f}")) from exc
    except requests.RequestException as exc:
        raise ApiError(0, t("API unreachable: {error}", error=exc.__class__.__name__)) from exc
    if response.status_code == 401 and auth:
        from lib import session

        session.logout(t("Your session has ended: logged out, perhaps in another tab, expired, or the account was disabled. Please log in again."))
        st.rerun()
    if response.status_code == 403 and auth and st.session_state.get("user"):
        refresh_role()
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text[:200]}
        detail = body.get("detail")
        raise ApiError(response.status_code, detail if isinstance(detail, str) else str(detail), body.get("errors"))
    result = response.content if raw else response.json() if response.content else None
    return (result, response.headers) if with_headers else result


@st.cache_data(ttl=30, show_spinner=False)
def _display_settings() -> dict:
    return call("GET", "/settings/display", auth=False, timeout=5)


def display_settings() -> dict:
    """The language and plant time zone an admin chose for everyone; shared by all sessions,
    read again on the next page/fragment run after the 30 s cache expires.
    Empty when the API did not answer."""
    try:
        return _display_settings()
    except ApiError:
        return {}  # not cached: the next run asks again


def display_changed() -> None:
    _display_settings.clear()


ROLE_CHECK_SECONDS = 15


def refresh_role_if_due() -> None:
    """Re-read the role now and then, so the menu follows a change an admin made, promotion
    included (a demotion is also caught by the first 403)."""
    if time.monotonic() - st.session_state.get("role_checked_at", 0.0) >= ROLE_CHECK_SECONDS:
        refresh_role()


def refresh_role() -> None:
    """A 403 can mean an admin changed this user's role: reload it so the menu matches."""
    st.session_state.role_checked_at = time.monotonic()
    try:
        # Short: this also runs inside the live page's 1 s refresh, which must not stall.
        me = requests.get(f"{API_BASE_URL}/auth/me", headers={"Authorization": f"Bearer {st.session_state.token}"}, timeout=2)
    except requests.RequestException:
        return
    if me.ok and me.json()["role"] != st.session_state.user["role"]:
        st.session_state.user = me.json()
        st.session_state.denied = t("Your role is now {role}; the menu has been updated.", role=i18n.role_name(me.json()["role"]))
        st.rerun()


def get(path: str, **kwargs) -> Any:
    return call("GET", path, **kwargs)


def post(path: str, **kwargs) -> Any:
    return call("POST", path, **kwargs)


MAX_REMEMBERED_KEYS = 50


def post_once(path: str, scope: str, content: bytes, *, keep_after_success: bool = False, **kwargs) -> Any:
    """POST with an Idempotency-Key that stays the same while the same content is retried.

    If a response is lost, sending the same content again returns the stored result instead
    of creating twice. Different content gets its own key. A success forgets the key, unless
    `keep_after_success`: then sending the same content again, even after other content, stays
    a no-op while the server keeps the key (24 hours).
    """
    digest = hashlib.sha256(content).hexdigest()
    keys: dict[str, str] = st.session_state.setdefault(f"idempotency_{scope}", {})
    key = keys.pop(digest, None) or str(uuid.uuid4())
    keys[digest] = key  # most recent last
    while len(keys) > MAX_REMEMBERED_KEYS:
        keys.pop(next(iter(keys)))
    headers = kwargs.pop("headers", {}) | {"Idempotency-Key": key}
    result = call("POST", path, headers=headers, **kwargs)
    if not keep_after_success:
        keys.pop(digest, None)
    return result


def patch(path: str, **kwargs) -> Any:
    return call("PATCH", path, **kwargs)


def put(path: str, **kwargs) -> Any:
    return call("PUT", path, **kwargs)


def delete(path: str, **kwargs) -> Any:
    return call("DELETE", path, **kwargs)


def show_error(error: ApiError) -> None:
    """Show corrections where the form called us; reserve tables for imported rows."""
    if error.status >= 500:
        # The API's outage response is actionable; other server/proxy details are not.
        message = (i18n.api_message(error.status, error.detail) if error.detail == "Database unavailable, please retry shortly"
                   else t("The service could not complete your request. Please try again."))
        st.error(message)
        return
    if error.status == 0 and error.detail.startswith(("API unreachable:", t("API unreachable: {error}", error=""))):
        st.error(t("Cannot connect to the service. Check your connection and try again."))
        return
    if not error.errors:
        st.error(i18n.api_message(error.status, error.detail))
        return

    labels = {
        "username": "Username", "email": "Email", "password": "Password", "role": "Role",
        "is_active": "Active", "title": "Title", "value": "Value", "category": "Category",
        "recorded_at": "Recorded at", "server": "Server", "warning_percent": "Warning from",
        "language": "Interface language for every station", "time_zone": "Plant", "thresholds": "Threshold",
        **LABELS,
    }

    def field_label(field: str | None) -> str:
        if not field or field == "body":
            return ""
        return " / ".join(t(labels.get(part, part.replace("_", " ").capitalize()))
                          for part in field.split(".") if part not in {"body", "query"})

    if any(item.get("row") is not None for item in error.errors):
        st.error(i18n.api_message(error.status, error.detail))
        st.caption(t("In a CSV, row 1 is the header and row 2 the first record; in JSON, row 1 is the first record."))
        rows = [{t("Row"): item.get("row"), t("Field"): field_label(item.get("field")),
                 t("Problem"): item.get("message", "")} for item in error.errors]
        st.dataframe(rows, hide_index=True)
        return

    problems = []
    for item in error.errors:
        label = field_label(item.get("field"))
        raw_message = item.get("message", "")
        message = raw_message
        if i18n.lang() == "zh":
            problems.append(f"**{label}:** {message}" if label else message)
            continue
        if raw_message == "Field required":
            message = t("Enter a value.")
        elif raw_message.startswith("Input should be a valid number"):
            message = t("Enter a valid number.")
        elif raw_message.startswith("Input should be a valid integer"):
            message = t("Enter a whole number.")
        else:
            constraints = (
                (r"String should have at least (\d+) characters?", "Use at least {limit} characters."),
                (r"String should have at most (\d+) characters?", "Use no more than {limit} characters."),
                (r"Input should be greater than (\S+)", "Enter a number greater than {limit}."),
                (r"Input should be greater than or equal to (\S+)", "Enter a number of at least {limit}."),
                (r"Input should be less than (\S+)", "Enter a number less than {limit}."),
                (r"Input should be less than or equal to (\S+)", "Enter a number no greater than {limit}."),
            )
            for pattern, guidance in constraints:
                if match := re.fullmatch(pattern, raw_message):
                    message = t(guidance, limit=match[1])
                    break
        problems.append(f"**{label}:** {message}" if label else message)
    message = problems[0] if len(problems) == 1 else "\n".join(f"- {problem}" for problem in problems)
    st.error(i18n.api_message(error.status, message))
