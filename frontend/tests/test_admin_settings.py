"""Settings feedback belongs to the submitted form and never replays writes."""
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from lib.api import ApiError
from lib.config import UNITS
from streamlit.testing.v1 import AppTest

PAGE = Path(__file__).resolve().parents[1] / "views/admin_settings.py"


class SettingsFeedback(unittest.TestCase):
    def setUp(self):
        self.current = dict(updated_by=None, updated_at=None, language="en", time_zone="UTC", warning_percent=90,
                            thresholds=[dict(category="cpu_temp", threshold=90, default=90, alert_below=False)])
        self.enterContext(patch("lib.api.get", side_effect=lambda *_args, **_kwargs: deepcopy(self.current)))
        self.api_patch = self.enterContext(patch("lib.api.patch"))
        self.changed = self.enterContext(patch("lib.api.display_changed"))
        self.app = AppTest.from_file(str(PAGE), default_timeout=15)
        self.app.session_state.user = {"id": 1, "role": "admin"}
        self.app.run()
        self.assertFalse(self.app.exception)

    def save(self):
        next(button for button in self.app.button if button.label == "Save settings").click().run()
        self.assertFalse(self.app.exception)

    def test_success_is_local_one_shot_and_no_repeat_write(self):
        self.save()
        self.api_patch.assert_called_once_with("/admin/settings", json={"warning_percent": 90, "language": "en",
                                                                   "time_zone": "UTC", "thresholds": {}})
        self.changed.assert_called_once()
        self.assertEqual([item.value for item in self.app.success], ["Settings saved."])
        form = self.app.get("form")[0]
        self.assertEqual([item.value for item in form.get("success")], ["Settings saved."])
        self.app.run()
        self.api_patch.assert_called_once()
        self.assertFalse(self.app.success)

    def test_failed_next_save_preserves_values_and_clears_success(self):
        self.save()
        self.api_patch.side_effect = ApiError(503, "Database unavailable, please retry shortly")
        self.app.slider[0].set_value(85)
        self.app.number_input[0].set_value(82.0)
        self.save()
        self.assertEqual(self.app.slider[0].value, 85)
        self.assertEqual(self.app.number_input[0].value, 82.0)
        self.assertFalse(self.app.success)
        self.assertTrue(self.app.get("form")[0].get("error"))
        self.assertEqual(self.api_patch.call_count, 2)
        self.changed.assert_called_once()

    def test_submit_keeps_form_values_when_another_admin_changes_defaults(self):
        self.current["thresholds"] = [dict(category=category, threshold=90, default=90, alert_below=False)
                                      for category in UNITS]
        self.app.run()
        self.app.radio[0].set_value("en")
        self.app.selectbox[0].set_value("Asia/Taipei")
        self.app.slider[0].set_value(85)
        for index, field in enumerate(self.app.number_input):
            field.set_value(82.0 + index)
        # These are the next GET's defaults, not the values submitted by this admin.
        self.current.update(language="zh", time_zone="Asia/Kuala_Lumpur", warning_percent=76)
        for item in self.current["thresholds"]:
            item["threshold"] = 97.0
        self.save()
        self.api_patch.assert_called_once_with("/admin/settings", json={"warning_percent": 85, "language": "en",
                                                                 "time_zone": "Asia/Taipei",
                                                                 "thresholds": {c: 82.0 + i for i, c in enumerate(UNITS)}})

    def test_leaving_form_discards_draft_and_revisit_reads_current_defaults(self):
        # Exercise Streamlit's widget cleanup by omitting the view for one run.
        # Actual navigation through app.py requires separate browser verification.
        self.current["time_zone"] = "America/Chicago"
        self.app = AppTest.from_string(
            "import runpy\nimport streamlit as st\n"
            "if not st.toggle('Away', key='test-away'):\n"
            f"    runpy.run_path({str(PAGE)!r})\n"
        )
        self.app.session_state.user = {"id": 1, "role": "admin"}
        self.app.run()
        self.app.radio[0].set_value("zh")
        self.app.selectbox[0].set_value("America/Chicago")
        self.app.slider[0].set_value(85)
        self.app.number_input[0].set_value(82.0)
        self.app.toggle(key="test-away").set_value(True).run()
        self.assertFalse(self.app.exception)
        self.current.update(language="en", time_zone="Asia/Kuala_Lumpur", warning_percent=76)
        self.current["thresholds"][0]["threshold"] = 97.0
        self.app.toggle(key="test-away").set_value(False).run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.radio[0].value, "en")
        self.assertEqual(self.app.selectbox[0].value, "Asia/Kuala_Lumpur")
        self.assertFalse(any("America/Chicago" in option for option in self.app.selectbox[0].options))
        self.assertEqual(self.app.slider[0].value, 76)
        self.assertEqual(self.app.number_input[0].value, 97.0)
        self.api_patch.assert_not_called()

    def test_submit_preserves_custom_plant_when_another_admin_changes_plant(self):
        for remote_zone in ("Asia/Taipei", "Europe/London"):
            with self.subTest(remote_zone=remote_zone):
                self.current["time_zone"] = "America/Chicago"
                self.app = AppTest.from_file(str(PAGE))
                self.app.session_state.user = {"id": 1, "role": "admin"}
                self.app.run()
                self.assertEqual(self.app.selectbox[0].value, "America/Chicago")
                self.app.number_input[0].set_value(82.0)
                self.current["time_zone"] = remote_zone
                self.save()
                self.api_patch.assert_called_with("/admin/settings", json={"warning_percent": 90, "language": "en",
                                                                      "time_zone": "America/Chicago",
                                                                      "thresholds": {"cpu_temp": 82.0}})
                self.assertEqual(self.app.selectbox[0].value, "America/Chicago")

    def test_repeated_save_uses_current_values_with_persisted_defaults(self):
        def persist(_path, *, json):
            self.current.update({key: value for key, value in json.items() if key != "thresholds"})
            for item in self.current["thresholds"]:
                item["threshold"] = json["thresholds"].get(item["category"], item["threshold"])

        self.api_patch.side_effect = persist
        for warning, threshold in ((85, 82.0), (78, 72.0), (85, 82.0)):
            self.app.slider[0].set_value(warning)
            self.app.number_input[0].set_value(threshold)
            self.save()
            self.assertEqual(self.api_patch.call_args.kwargs["json"]["warning_percent"], warning)
            self.assertEqual(self.api_patch.call_args.kwargs["json"]["thresholds"], {"cpu_temp": threshold})
            self.assertEqual(self.app.slider[0].value, warning)
            self.assertEqual(self.app.number_input[0].value, threshold)
        self.assertEqual(self.api_patch.call_count, 3)


if __name__ == "__main__":
    unittest.main()
