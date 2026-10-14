"""History navigation follows visited cursor boundaries, never invented page offsets."""

import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

PAGE = Path(__file__).resolve().parents[1] / "views" / "admin_history.py"


class HistoryNavigation(unittest.TestCase):
    def test_previous_returns_to_visited_page_and_filters_reset_to_newest(self):
        requests = []

        def get(path, params=None):
            if path == "/realtime/config":
                return {"sensors": {"cpu_temp": ["CPU0"]}}
            self.assertEqual(path, "/admin/realtime/history")
            requests.append(dict(params))
            cursor = params.get("before_id")
            timestamps = {917: "2026-10-14T00:02:00Z", 403: "2026-10-14T00:01:00Z"}
            if cursor is not None:
                self.assertEqual(params["before_at"], timestamps[cursor])
            else:
                self.assertNotIn("before_at", params)
            # Opaque cursor fixtures: there is no relation between a cursor and a page number.
            next_cursor = {None: 917, 917: 403, 403: None}[cursor]
            return {"items": [], "next_before_id": next_cursor,
                    "next_before_at": timestamps.get(next_cursor)}

        def button(app, label):
            matches = [item for item in app.button if item.label == label]
            self.assertEqual(len(matches), 1, f"Expected one {label} navigation button")
            return matches[0]

        with patch("lib.session.is_admin", return_value=True), patch("lib.api.get", side_effect=get):
            app = AppTest.from_file(str(PAGE), default_timeout=10).run()
            self.assertFalse(app.exception)
            self.assertTrue(button(app, "Previous").disabled)
            self.assertNotIn("before_id", requests[-1])

            button(app, "Older").click().run()
            self.assertEqual(requests[-1]["before_id"], 917)
            button(app, "Older").click().run()
            self.assertEqual(requests[-1]["before_id"], 403)
            self.assertTrue(button(app, "Older").disabled)

            button(app, "Previous").click().run()
            self.assertEqual(requests[-1]["before_id"], 917)
            self.assertFalse(app.number_input)  # no page-number control that suggests jumping to page N
            self.assertTrue(any(item.value.startswith("Page 2 ·") for item in app.caption))
            self.assertIn("no jump to a page number", button(app, "Older").help)

            button(app, "Older").click().run()
            self.assertEqual(requests[-1]["before_id"], 403)
            button(app, "Newest").click().run()
            self.assertNotIn("before_id", requests[-1])
            self.assertTrue(button(app, "Previous").disabled)

            button(app, "Older").click().run()
            app.text_input(key="keep:history-server").set_value("S0002").run()
            self.assertEqual(requests[-1]["server"], "S0002")
            self.assertNotIn("before_id", requests[-1])
            self.assertTrue(button(app, "Previous").disabled)
            self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
