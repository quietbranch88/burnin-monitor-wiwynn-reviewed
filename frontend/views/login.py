import streamlit as st
from lib import api, session
from lib.i18n import t

# A bounded card stays readable on desktop and fits the available width on phones.
with st.container(horizontal=True, horizontal_alignment="center"), st.container(width=440, border=True, gap="small"):
    st.subheader(t("Burn-in Monitor"), anchor=False)
    st.caption(t("Server-factory burn-in test monitoring"))

    if message := st.session_state.pop("flash", None):
        st.info(message)

    login_tab, register_tab = st.tabs([t("Log in"), t("Register")])

    with login_tab:
        with st.form("login", border=False):
            username = st.text_input(t("Username"))
            password = st.text_input(t("Password"), type="password")
            submitted = st.form_submit_button(t("Log in"), type="primary", width="stretch")
        if submitted:
            try:
                session.login(username, password)
                st.rerun()
            except api.ApiError as error:
                api.show_error(error)

    with register_tab:
        with st.form("register", clear_on_submit=False, border=False):
            new_username = st.text_input(t("Username"), help=t("3-50 letters, digits, '.', '_' or '-'"))
            email = st.text_input(t("Email"))
            new_password = st.text_input(t("Password"), type="password", help=t("8-64 characters, with at least one letter and one digit"))
            confirm = st.text_input(t("Confirm password"), type="password")
            created = st.form_submit_button(t("Create account"), type="primary", width="stretch")
        if created:
            if new_password != confirm:
                st.error(t("Passwords do not match."))
            else:
                try:
                    api.post("/auth/register", auth=False, json={"username": new_username, "email": email, "password": new_password})
                    st.success(t("Account created with the Viewer role. You can log in now; an admin can grant more access."))
                except api.ApiError as error:
                    api.show_error(error)

    with st.expander(t("Account access")):
        st.caption(t("New accounts start as Viewer. Ask an administrator for engineering or admin access."))
        st.caption(t("Demo accounts: admin · engineer · viewer. Use the demo passwords from the README "
                     "unless your administrator changed them."))
