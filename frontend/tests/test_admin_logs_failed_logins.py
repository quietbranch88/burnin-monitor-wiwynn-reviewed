"""Failed-login mode requests event logs without erasing the user's selected kind."""

import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from streamlit.proto.WidgetStates_pb2 import WidgetStates
from streamlit.testing.v1 import AppTest

PAGE = Path(__file__).resolve().parents[1] / "views" / "admin_logs.py"


class FailedLoginMode(unittest.TestCase):
    def test_removed_filters_ignore_legacy_state_and_do_not_reset_paging(self):
        requests = []

        def get(_path, params=None):
            requests.append(dict(params))
            return {"items": [], "total": 200, "page": params["page"], "pages": 4}

        with patch("lib.session.is_admin", return_value=True), patch("lib.api.get", side_effect=get):
            app = AppTest.from_file(str(PAGE), default_timeout=15)
            for key, value in (("path", "/old-path"), ("status", 503), ("logger", "old.logger"),
                               ("request-id", "old-request")):
                app.session_state[f"keep:logs-{key}"] = value
            app.run()
            self.assertFalse(app.exception)
            self.assertTrue({"path", "status_code", "logger", "request_id"}.isdisjoint(requests[-1]))
            labels = {item.label for item in [*app.text_input, *app.number_input]}
            self.assertTrue({"Path contains", "Status code", "Logger", "Request ID"}.isdisjoint(labels))
            self.assertEqual([item.proto.popover.label for item in app.get("popover")], ["Filters"])
            self.assertFalse(any("old-" in item.value or "old.logger" in item.value for item in app.caption))
            app.number_input(key="keep:logs-page").set_value(3).run()
            app.session_state["keep:logs-path"] = "/another-old-path"
            app.session_state["keep:logs-status"] = 404
            app.session_state["keep:logs-logger"] = "another.logger"
            app.session_state["keep:logs-request-id"] = "another-request"
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(requests[-1]["page"], 3)
            self.assertTrue({"path", "status_code", "logger", "request_id"}.isdisjoint(requests[-1]))

    def test_visible_account_level_message_and_time_filters_reach_query(self):
        requests = []

        def get(path, params=None):
            if path == "/admin/users":
                self.assertEqual(params, {"q": "test-admin", "page_size": 100})
                return {"items": [{"id": 17, "username": "test-admin"}]}
            self.assertEqual(path, "/admin/logs")
            requests.append(dict(params))
            return {"items": [], "total": 0, "page": 1, "pages": 0}

        with patch("lib.session.is_admin", return_value=True), patch("lib.api.get", side_effect=get):
            app = AppTest.from_file(str(PAGE), default_timeout=15).run()
            app.text_input(key="keep:logs-user").set_value("test-admin")
            app.text_input(key="keep:logs-q").set_value("updated settings")
            app.selectbox(key="keep:logs-level").set_value("ERROR")
            app.selectbox(key="keep:logs-preset").set_value("Last 1 hour").run()
            self.assertFalse(app.exception)
            self.assertEqual(requests[-1]["user_id"], 17)
            self.assertEqual(requests[-1]["level"], "ERROR")
            self.assertEqual(requests[-1]["q"], "updated settings")
            elapsed = (datetime.now(UTC) - datetime.fromisoformat(requests[-1]["start"])).total_seconds()
            self.assertLess(abs(elapsed - 3600), 10)
            app.selectbox(key="keep:logs-preset").set_value("All time").run()
            self.assertNotIn("start", requests[-1])
            self.assertNotIn("end", requests[-1])

    def test_preexisting_empty_kind_is_visibly_restored_to_events(self):
        with patch("lib.session.is_admin", return_value=True), patch("lib.api.get") as get:
            get.return_value = {"items": [], "total": 0, "page": 1, "pages": 0}
            app = AppTest.from_file(str(PAGE), default_timeout=15)
            app.session_state["keep:logs-kind"] = None
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.get("button_group")[0].value, "events")
            self.assertEqual(get.call_args.kwargs["params"]["kind"], "events")

    def test_kind_requires_a_selection_and_repeated_valid_selection_keeps_query(self):
        requests = []

        def get(_path, params=None):
            requests.append(dict(params))
            return {"items": [], "total": 0, "page": 1, "pages": 0}

        with patch("lib.session.is_admin", return_value=True), patch("lib.api.get", side_effect=get):
            app = AppTest.from_file(str(PAGE), default_timeout=15).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.get("button_group")[0].value, "events")
            self.assertTrue(app.get("button_group")[0].proto.required)
            for kind, label in (("requests", "API requests"), ("requests", "API requests"),
                                ("all", "Everything"), ("all", "Everything"), ("events", "Changes and events")):
                # Native protocol for valid selections; browser prevention of deselection
                # needs a real browser because AppTest's serde still accepts an empty list.
                states = WidgetStates()
                state = states.widgets.add(id=app.get("button_group")[0].proto.id)
                state.string_array_value.data.append(label)
                app._run(states)
                self.assertFalse(app.exception)
                self.assertEqual(app.get("button_group")[0].value, kind)
                self.assertEqual(requests[-1].get("kind"), None if kind == "all" else kind)
                self.assertTrue(app.get("button_group")[0].proto.required)

    def test_missing_account_is_explicit_without_guessing_an_identity(self):
        items = [{"created_at": "2026-10-13T09:00:00Z", "level": "INFO", "message": "Request completed",
                  "username": username, "logger": "app.request", "client_ip": None, "request_id": None}
                 for username in (None, "", "test-admin")]
        result = {"items": items, "total": 3, "page": 1, "pages": 1}
        with patch("lib.session.is_admin", return_value=True), patch("lib.api.get", return_value=result):
            app = AppTest.from_file(str(PAGE), default_timeout=15).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.dataframe[0].value["username"].tolist(),
                         ["No linked account", "No linked account", "test-admin"])
        self.assertEqual(len(app.dataframe), 1)
        self.assertEqual(list(app.dataframe[0].value.columns), ["created_at", "level", "message", "username"])

    def test_toggle_uses_events_then_restores_each_selected_kind_and_filters(self):
        for selected_kind in ("requests", "all", "events"):
            with self.subTest(selected_kind=selected_kind):
                requests = []

                def get(path, params=None, captured=requests):
                    self.assertEqual(path, "/admin/logs")
                    captured.append(dict(params))
                    return {"items": [], "total": 200, "page": params["page"], "pages": 4}

                with patch("lib.session.is_admin", return_value=True), patch("lib.api.get", side_effect=get):
                    app = AppTest.from_file(str(PAGE), default_timeout=15)
                    app.session_state["keep:logs-kind"] = selected_kind
                    app.session_state["keep:logs-level"] = "ERROR"
                    app.session_state["keep:logs-logger"] = "app.request"
                    app.session_state["keep:logs-q"] = "original search"
                    app.run()
                    self.assertFalse(app.exception)
                    original = {k: requests[-1].get(k) for k in ("kind", "logger", "q", "level")}
                    self.assertEqual(original["kind"], None if selected_kind == "all" else selected_kind)
                    self.assertIsNone(original["logger"])
                    app.number_input(key="keep:logs-page").set_value(3).run()
                    self.assertEqual(requests[-1]["page"], 3)

                    app.toggle(key="keep:logs-failed-logins").set_value(True).run()
                    self.assertFalse(app.exception)
                    self.assertEqual(requests[-1].get("kind"), "events")
                    self.assertEqual(requests[-1]["logger"], "app.users")
                    self.assertEqual(requests[-1]["q"], "Login failed")
                    self.assertEqual(requests[-1]["level"], "WARNING")
                    self.assertEqual(requests[-1]["page"], 1)
                    self.assertEqual(app.session_state["keep:logs-kind"], selected_kind)
                    saved_view = app.get("button_group")[0].proto
                    self.assertEqual(saved_view.label, "Saved view")
                    self.assertTrue(saved_view.disabled)
                    self.assertEqual(saved_view.help, "Failed-login mode shows events. Turn it off to return to this view.")

                    app.toggle(key="keep:logs-failed-logins").set_value(False).run()
                    self.assertFalse(app.exception)
                    restored = {k: requests[-1].get(k) for k in ("kind", "logger", "q", "level")}
                    self.assertEqual(restored, original)
                    self.assertEqual(app.session_state["keep:logs-kind"], selected_kind)
                    self.assertEqual(requests[-1]["page"], 1)
                    restored_view = app.get("button_group")[0].proto
                    self.assertEqual(restored_view.label, "Show")
                    self.assertFalse(restored_view.disabled)


if __name__ == "__main__":
    unittest.main()
