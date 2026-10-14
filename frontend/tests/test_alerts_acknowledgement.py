"""Alert UI contracts with HTTP substituted; native dataframe state is injected
because AppTest does not provide a dataframe selection action. Browser checks
must exercise actual row selection and the real API separately.
"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from lib.api import ApiError
from streamlit.testing.v1 import AppTest

PAGE = Path(__file__).resolve().parents[1] / "views/alerts.py"


def alert(identifier, *, acknowledged=False):
    return {"id": identifier, "started_at": "2026-10-14T00:00:00Z", "ended_at": "2026-10-14T00:00:05Z",
            "server": "S0001", "category": "cpu_temp", "sensor": "CPU0", "peak_value": 99.0,
            "threshold": 90.0, "readings": 5, "acknowledged_by": "engineer" if acknowledged else None,
            "acknowledged_at": "2026-10-14T01:00:00Z" if acknowledged else None}


class AlertAcknowledgement(unittest.TestCase):
    def setUp(self):
        self.rows = [alert(17), alert(29), alert(42, acknowledged=True)]
        self.requests = []
        self.patched = []
        self.fail_id = None
        self.enterContext(patch("lib.api.get", side_effect=self.get))
        self.enterContext(patch("lib.api.patch", side_effect=self.acknowledge))
        self.selection = None
        self.app = AppTest.from_file(str(PAGE), default_timeout=15)
        self.app.session_state.user = {"id": 7, "role": "user"}
        self.app.session_state.display_zone = "Asia/Taipei"
        self.run_page()
        self.assertFalse(self.app.exception)

    def get(self, path, params=None):
        if path == "/realtime/config":
            return {"sensors": {"cpu_temp": ["CPU0"]}}
        self.assertEqual(path, "/realtime/alert-events")
        self.requests.append(dict(params))
        rows = [dict(row) for row in self.rows if not (params.get("acknowledged") is False and row["acknowledged_at"])]
        return {"items": rows, "next_before_id": 91, "next_before_at": "2026-10-13T00:00:00Z"}

    def acknowledge(self, path, *, json):
        self.assertEqual(json, {"acknowledged": True})
        identifier = int(path.rsplit("/", 1)[1])
        self.patched.append(identifier)
        if identifier == self.fail_id:
            raise ApiError(503, "Database unavailable, please retry shortly")
        for row in self.rows:
            if row["id"] == identifier and not row["acknowledged_at"]:
                row.update(acknowledged_at="2026-10-14T02:00:00Z", acknowledged_by="engineer")
        return next(row for row in self.rows if row["id"] == identifier)

    def button(self, label="Acknowledge selected"):
        return next(button for button in self.app.button if button.label == label)

    def run_page(self):
        # AppTest omits native dataframe selection when serializing widgets.
        # Submit the real widget protocol rather than mocking st.dataframe.
        widgets = self.app._tree.get_widget_states()
        if self.selection is not None:
            identifier, rows = self.selection
            state = next((state for state in widgets.widgets if state.id == identifier), None)
            if state is None:
                state = widgets.widgets.add(id=identifier)
            state.string_value = json.dumps({"selection": {"rows": rows, "columns": [], "cells": []}})
        self.app._run(widgets)
        self.assertFalse(self.app.exception)

    def select(self, rows):
        self.selection = (self.app.dataframe[0].proto.id, rows)
        self.run_page()

    def click(self, label="Acknowledge selected"):
        self.button(label).click()
        self.run_page()

    def test_both_editor_roles_submit_only_selected_unacknowledged_ids(self):
        for role in ("user", "admin"):
            with self.subTest(role=role):
                self.rows = [alert(17), alert(29), alert(42, acknowledged=True)]
                self.app.session_state.user = {"id": 7, "role": role}
                self.run_page()
                self.select([1, 2])
                self.assertFalse(self.button().disabled)
                self.click()
                self.assertFalse(self.app.exception)
                self.assertEqual(self.patched[-1:], [29])
                self.assertIn("Acknowledged 1 alerts.", [notice.value for notice in self.app.success])
                self.assertTrue(self.button().disabled)
        self.assertEqual(self.patched, [29, 29])

    def test_viewer_table_has_no_selection_or_acknowledgement_action(self):
        self.app.session_state.user = {"id": 8, "role": "viewer"}
        self.run_page()
        self.assertFalse(self.app.exception)
        self.assertNotIn("Acknowledge selected", [button.label for button in self.app.button])
        self.assertFalse(self.app.dataframe[0].proto.selection_mode)
        self.assertEqual(self.patched, [])

    def test_acknowledgement_columns_show_username_and_local_time(self):
        frame = self.app.dataframe[0].value
        self.assertEqual(frame.iloc[2]["acknowledged_by"], "engineer")
        self.assertEqual(frame.iloc[2]["acknowledged_at"], pd.Timestamp("2026-10-14T09:00:00"))
        self.assertTrue(pd.isna(frame.iloc[0]["acknowledged_at"]))

    def test_unacknowledged_filter_uses_false_and_resets_page_and_selection(self):
        self.click("Older")
        self.select([0])
        self.app.toggle(key="keep:alerts-unacknowledged").set_value(True)
        self.run_page()
        self.assertFalse(self.app.exception)
        self.assertIs(self.requests[-1]["acknowledged"], False)
        self.assertNotIn("before_id", self.requests[-1])
        self.assertTrue(self.button().disabled)
        self.assertEqual(len(self.app.dataframe[0].value), 2)
        self.app.toggle(key="keep:alerts-unacknowledged").set_value(False)
        self.run_page()
        self.assertNotIn("acknowledged", self.requests[-1])

    def test_page_change_clears_selection_even_when_rows_happen_to_match(self):
        self.select([0])
        self.click("Older")
        self.assertEqual(self.requests[-1]["before_id"], 91)
        self.assertTrue(self.button().disabled)

    def test_filter_change_clears_selection_even_when_rows_happen_to_match(self):
        self.select([0])
        self.app.text_input(key="keep:alerts-server").set_value("1")
        self.run_page()
        self.assertEqual(self.requests[-1]["server"], "S0001")
        self.assertTrue(self.button().disabled)

    def test_arriving_alert_cannot_redirect_selected_position_on_action_rerun(self):
        self.select([0])
        self.rows.insert(0, alert(88))
        self.click()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.patched, [])
        self.assertTrue(self.button().disabled)

    def test_reordered_rows_cannot_redirect_selected_position(self):
        self.select([0])
        self.rows.reverse()
        self.click()
        self.assertEqual(self.patched, [])

    def test_partial_failure_preserves_confirmed_count_and_reloads_server_state(self):
        self.rows = [alert(17), alert(29), alert(42)]
        self.run_page()
        self.fail_id = 29
        self.select([0, 1, 2])
        self.click()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.patched, [17, 29])
        self.assertFalse(self.app.success)
        self.assertIn("Confirmed 1 of 3 selected alerts. 2 remain unconfirmed. Refresh before retrying.",
                      [notice.value for notice in self.app.warning])
        self.assertTrue(self.app.error)
        self.assertEqual(self.app.dataframe[0].value.iloc[0]["acknowledged_by"], "engineer")
        self.assertTrue(self.button().disabled)
        self.fail_id = None
        self.select([1, 2])
        self.click()
        self.assertEqual(self.patched, [17, 29, 29, 42])
        self.assertIn("Acknowledged 2 alerts.", [notice.value for notice in self.app.success])


if __name__ == "__main__":
    unittest.main()
