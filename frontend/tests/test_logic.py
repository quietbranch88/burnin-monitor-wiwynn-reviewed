"""Unit tests for the frontend's pure logic; no Streamlit server or API needed.

Run in the frontend image: docker compose run --rm --no-deps frontend python -m unittest discover -s tests
"""

import string
import unittest
import unittest.mock

from lib import i18n, ui
from lib.stream import _complete


class Translations(unittest.TestCase):
    def test_every_translation_keeps_the_placeholders(self):
        # A placeholder missing in Chinese would raise KeyError in t() at runtime.
        for english, chinese in i18n.ZH.items():
            fields = {name for _, name, _, _ in string.Formatter().parse(english) if name is not None}
            translated = {name for _, name, _, _ in string.Formatter().parse(chinese) if name is not None}
            self.assertEqual(fields, translated, english)

    def test_api_errors_keep_the_api_text_behind_a_line_for_their_status(self):
        with unittest.mock.patch.object(i18n, "lang", return_value="zh"):
            self.assertEqual(i18n.api_message(422, "value must be a finite number"), "輸入的資料有誤：value must be a finite number")
            self.assertEqual(i18n.api_message(0, "API unreachable"), "API unreachable")  # no status: already in the language
        with unittest.mock.patch.object(i18n, "lang", return_value="en"):
            self.assertEqual(i18n.api_message(422, "value must be a finite number"), "value must be a finite number")


class LoadPercent(unittest.TestCase):
    def test_share_of_the_limit_on_the_alerting_side(self):
        self.assertEqual(ui.load(85, 85, below=False), 100)
        self.assertEqual(ui.load(42.5, 85, below=False), 50)
        self.assertEqual(ui.load(6000, 3000, below=True), 50)  # a fast fan is far from its limit
        self.assertEqual(ui.load(1500, 3000, below=True), 200)

    def test_a_stopped_fan_is_capped_so_charts_get_valid_json(self):
        self.assertEqual(ui.load(0, 3000, below=True), ui.LOAD_CAP)


class ServerIds(unittest.TestCase):
    def test_a_number_means_the_live_page_id(self):
        self.assertEqual(ui.server_id(" 17 "), "S0017")
        self.assertEqual(ui.server_id("S0017"), "S0017")
        self.assertEqual(ui.server_id("SN-2041A"), "SN-2041A")
        self.assertEqual(ui.server_id(""), "")


class PerServerReadings(unittest.TestCase):
    def test_the_tick_fills_in_time_threshold_and_alert(self):
        tick = {"generated_at": "2026-10-13T08:00:00Z", "summary": {
            "gpu_temp": {"threshold": 85.0, "alert_below": False},
            "fan_rpm": {"threshold": 3000.0, "alert_below": True},
        }}
        readings = [{"server": "S0001", "category": "gpu_temp", "sensor": "GPU3", "value": 86.0},
                    {"server": "S0001", "category": "fan_rpm", "sensor": "Fan2", "value": 2900.0},
                    {"server": "S0002", "category": "fan_rpm", "sensor": "Fan0", "value": 9000.0}]
        out = _complete(readings, tick)
        self.assertEqual([r["is_anomaly"] for r in out], [True, True, False])
        self.assertEqual({r["generated_at"] for r in out}, {"2026-10-13T08:00:00Z"})
        self.assertEqual([r["threshold"] for r in out], [85.0, 3000.0, 3000.0])


if __name__ == "__main__":
    unittest.main()
