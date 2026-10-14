"""Export state contracts; browser checks cover fragment rendering and downloads."""

import ast
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib import ui
from lib.api import ApiError

ROOT = Path(__file__).resolve().parents[1]


class State(dict):
    def __setattr__(self, key, value):
        self[key] = value


class ExportReport(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.st = MagicMock(session_state=self.state)
        self.clock = self.enterContext(patch("lib.ui.datetime", wraps=datetime))
        self.clock.now.return_value = datetime(2026, 10, 14, 12, tzinfo=UTC)
        self.enterContext(patch("lib.ui.st", self.st))
        self.state["keep:analytics-records-preset"] = "Last 1 hour"
        self.state["display_zone"] = "UTC"
        self.api = MagicMock(ApiError=ApiError)
        self.key = ("records", "Last 1 hour", "cpu_temp", "S0001", "minute", "UTC")
        self.params = {"source": "records", "category": "cpu_temp", "server": "S0001",
                       "interval": "minute", "tz": "UTC", "start": "2026-10-14T11:00:00+00:00"}
        tree = ast.parse((ROOT / "views/analytics.py").read_text(encoding="utf-8"))
        definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        for node in definitions:
            node.decorator_list = []
        self.scope = {"st": self.st, "api": self.api, "ui": ui, "t": lambda value: value,
                      "source": "records", "export_key": self.key, "export_params": self.params}
        exec(compile(ast.Module(body=definitions, type_ignores=[]), "analytics.py", "exec"), self.scope)
        self.render = self.scope["export_report"]

    def generate(self):
        self.scope["request_export"]()
        self.render()

    def test_generate_preserves_filtered_api_contract_and_repeated_download_bytes(self):
        self.api.get.return_value = b"workbook-response"
        self.generate()
        self.api.get.assert_called_once_with("/analytics/export", params=self.params, raw=True, timeout=60)
        self.assertNotIn("export_requested", self.state)
        self.assertEqual(self.state["excel"], (self.key, b"workbook-response"))
        for _ in range(2):
            self.render()
            download = self.st.download_button.call_args
            self.assertEqual(download.args, ("Download Excel report", b"workbook-response"))
            self.assertEqual(download.kwargs["on_click"], "ignore")
            self.assertEqual(download.kwargs["type"], "primary")
            self.assertEqual(self.st.button.call_args.args[0], "Regenerate report")
            self.assertEqual(self.st.button.call_args.kwargs["type"], "secondary")
        self.api.get.assert_called_once()

    def test_filter_change_invalidates_old_report_and_does_not_offer_download(self):
        self.state["excel"] = (self.key, b"old")
        self.scope["export_key"] = ("records", "All time", "cpu_temp", "S0001", "minute", "UTC")
        self.render()
        self.assertNotIn("excel", self.state)
        self.st.download_button.assert_not_called()
        self.api.get.assert_not_called()
        self.assertEqual(self.st.button.call_args.args[0], "Generate Excel report")

    def test_failed_generation_shows_error_and_allows_retry(self):
        error = ApiError(503, "Try again")
        self.api.get.side_effect = [error, b"retry-workbook"]
        self.generate()
        self.api.show_error.assert_called_once_with(error)
        self.st.download_button.assert_not_called()
        self.assertNotIn("export_requested", self.state)
        self.assertEqual(self.st.button.call_args.args[0], "Generate Excel report")
        self.generate()
        self.assertEqual(self.state["excel"], (self.key, b"retry-workbook"))

    def test_failed_regeneration_retains_prepared_report(self):
        self.state["excel"] = (self.key, b"previous-workbook")
        error = ApiError(503, "Try again")
        self.api.get.side_effect = error
        self.generate()
        self.api.show_error.assert_called_once_with(error)
        self.assertEqual(self.st.download_button.call_args.args[1], b"previous-workbook")
        self.assertEqual(self.state["excel"], (self.key, b"previous-workbook"))

    def test_success_feedback_only_after_successful_generation_and_regeneration(self):
        self.render()
        self.st.toast.assert_not_called()
        self.api.get.side_effect = [b"first-workbook", ApiError(503, "Try again"), b"new-workbook"]
        self.generate()
        self.st.toast.assert_called_once_with("Report generated. Click Download to save the latest file.", icon=":material/check_circle:")
        self.st.toast.reset_mock()
        self.render()
        self.st.toast.assert_not_called()
        self.generate()
        self.st.toast.assert_not_called()
        self.assertEqual(self.state["excel"], (self.key, b"first-workbook"))
        self.generate()
        self.st.toast.assert_called_once_with("Report generated. Click Download to save the latest file.", icon=":material/check_circle:")
        self.assertEqual(self.state["excel"], (self.key, b"new-workbook"))

    def test_regeneration_resolves_relative_range_at_request_time(self):
        self.generate()
        self.assertEqual(self.api.get.call_args.kwargs["params"]["start"], "2026-10-14T11:00:00+00:00")
        self.clock.now.return_value = datetime(2026, 10, 14, 13, tzinfo=UTC)
        self.generate()
        self.assertEqual(self.api.get.call_args.kwargs["params"]["start"], "2026-10-14T12:00:00+00:00")

    def test_today_and_yesterday_follow_display_zone_midnight(self):
        self.state["display_zone"] = "Asia/Taipei"
        self.state["keep:analytics-records-preset"] = "Today"
        self.clock.now.return_value = datetime(2026, 10, 14, 15, 59, tzinfo=UTC)
        self.generate()
        self.assertEqual(self.api.get.call_args.kwargs["params"]["start"], "2026-10-14T00:00:00+08:00")
        self.clock.now.return_value = datetime(2026, 10, 14, 16, 1, tzinfo=UTC)
        self.generate()
        self.assertEqual(self.api.get.call_args.kwargs["params"]["start"], "2026-10-15T00:00:00+08:00")
        self.assertEqual(self.api.get.call_args.kwargs["params"]["end"], "2026-10-15T23:59:59.999999+08:00")
        self.state["keep:analytics-records-preset"] = "Yesterday"
        self.generate()
        self.assertEqual(self.api.get.call_args.kwargs["params"]["start"], "2026-10-14T00:00:00+08:00")

    def test_custom_dates_stay_fixed_and_all_time_clears_stale_boundaries(self):
        self.state["keep:analytics-records-preset"] = "Custom"
        self.state["keep:analytics-records-dates"] = (date(2026, 10, 1), date(2026, 10, 3))
        for now in [datetime(2026, 10, 14, tzinfo=UTC), datetime(2026, 10, 15, tzinfo=UTC)]:
            self.clock.now.return_value = now
            self.generate()
            self.assertEqual(self.api.get.call_args.kwargs["params"]["start"], "2026-10-01T00:00:00+00:00")
            self.assertEqual(self.api.get.call_args.kwargs["params"]["end"], "2026-10-03T23:59:59.999999+00:00")
        self.state["keep:analytics-records-preset"] = "All time"
        self.generate()
        self.assertNotIn("start", self.api.get.call_args.kwargs["params"])
        self.assertNotIn("end", self.api.get.call_args.kwargs["params"])


if __name__ == "__main__":
    unittest.main()
