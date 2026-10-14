"""Alert event paging follows visited cursor boundaries, like the telemetry history."""

import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

PAGE = Path(__file__).resolve().parents[1] / "views" / "alerts.py"


class AlertNavigation(unittest.TestCase):
    def test_previous_returns_to_the_visited_page(self):
        requests = []
        timestamps = {91: "2026-10-14T00:02:00Z", 47: "2026-10-14T00:01:00Z"}

        def get(path, params=None):
            if path == "/realtime/config":
                return {"sensors": {"cpu_temp": ["CPU0"]}}
            self.assertEqual(path, "/realtime/alert-events")
            requests.append(dict(params))
            cursor = params.get("before_id")
            if cursor is not None:
                self.assertEqual(params["before_at"], timestamps[cursor])
            next_cursor = {None: 91, 91: 47, 47: None}[cursor]
            return {"items": [], "next_before_id": next_cursor, "next_before_at": timestamps.get(next_cursor)}

        def button(app, label):
            matches = [item for item in app.button if item.label == label]
            self.assertEqual(len(matches), 1, f"Expected one {label} navigation button")
            return matches[0]

        with patch("lib.api.get", side_effect=get):
            app = AppTest.from_file(str(PAGE), default_timeout=15)
            app.session_state.user = {"id": 7, "role": "user"}
            app.session_state.display_zone = "Asia/Taipei"
            app.run()
            self.assertFalse(app.exception)
            self.assertTrue(button(app, "Previous").disabled)
            self.assertIn("no jump to a page number", button(app, "Older").help)

            button(app, "Older").click().run()
            self.assertEqual(requests[-1]["before_id"], 91)
            button(app, "Older").click().run()
            self.assertEqual(requests[-1]["before_id"], 47)
            self.assertTrue(button(app, "Older").disabled)

            button(app, "Previous").click().run()
            self.assertEqual(requests[-1]["before_id"], 91)
            button(app, "Previous").click().run()
            self.assertNotIn("before_id", requests[-1])
            self.assertTrue(button(app, "Previous").disabled)
            self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
