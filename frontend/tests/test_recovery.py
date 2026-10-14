"""Regression contracts for live recovery, boundary colors and import feedback."""

import ast
import hashlib
import json
import runpy
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pyarrow as pa
import requests
import streamlit as st
from lib import ui
from lib.stream import RealtimeStream, fingerprint

ROOT = Path(__file__).resolve().parents[1]


def page_functions(*names, keep_decorators=False, **context):
    """Execute page helpers without top-level UI/network; optionally keep real decorators."""
    source = ast.parse((ROOT / "views/realtime.py").read_text(encoding="utf-8"))
    definitions = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names]
    if not keep_decorators:
        for definition in definitions:
            definition.decorator_list = []
    scope = {"ui": ui, "below": {"fan_rpm"}, "ALL": "all", "categories": [], "pa": pa,
             "datetime": datetime, "timedelta": timedelta, "config": {"warning_percent": 90},
             "t": lambda value, **kwargs: value.format(**kwargs), "st": st, "__name__": __name__} | context
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(ROOT / "views/realtime.py"), "exec"), scope)
    return scope


def tick(second):
    return {"type": "tick", "generated_at": (datetime(2026, 10, 13, tzinfo=UTC) + timedelta(seconds=second)).isoformat(),
            "summary": {"cpu_temp": {"anomalies": 1, "threshold": 90, "alert_below": False}}, "alerts": []}


class StreamRecovery(unittest.TestCase):
    def test_history_and_queued_live_ticks_count_once_without_time_regression(self):
        stream = RealtimeStream("ws://test.invalid", "test", summary_url="http://test.invalid", maxlen=4)
        stream.extend([tick(1)])
        messages = iter([tick(10), tick(11), tick(12), tick(13)])
        snapshots = []

        class Socket:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def recv(self, **_kwargs):
                snapshots.append(stream.snapshot())
                try:
                    return json.dumps(next(messages))
                except StopIteration:
                    stream.stop()
                    raise TimeoutError from None

        response = MagicMock()
        response.json.return_value = [tick(1), tick(10), tick(11), tick(12)]
        with patch("lib.stream.connect", return_value=Socket()), patch("lib.stream.requests.get", return_value=response):
            stream._run()
        for snapshot in snapshots:
            seconds = [datetime.fromisoformat(t["generated_at"]).second for t in snapshot]
            self.assertEqual(seconds, sorted(set(seconds)))
            self.assertLessEqual(len(seconds), 4)
        newest = [snapshot[-1]["generated_at"] for snapshot in snapshots]
        self.assertEqual(newest, sorted(newest))
        self.assertEqual([datetime.fromisoformat(t["generated_at"]).second for t in stream.snapshot()], [10, 11, 12, 13])
        functions = page_functions("fleet_view")
        final, digest = stream.snapshot_keyed()
        self.assertEqual(digest, fingerprint(final))  # the digests kept per tick match the buffer after the merge
        view = functions["fleet_view"](final[-1]["generated_at"], final[0]["generated_at"], 4, 300, "en", "UTC", digest, tuple(final))
        self.assertEqual(view["alerts_in_window"], 4)

    def test_out_of_order_live_readings_do_not_replace_newest_server_readings(self):
        stream = RealtimeStream("ws://test.invalid", "test", maxlen=2)
        messages = iter([tick(12), tick(11), tick(13)])
        latest = []

        class Socket:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def recv(self, **_kwargs):
                if stream.latest_readings():
                    latest.append(stream.latest_readings()[0]["generated_at"])
                try:
                    message = next(messages)
                    message["server_readings"] = [{"server": "S0001", "sensor": "CPU0", "category": "cpu_temp", "value": 90}]
                    return json.dumps(message)
                except StopIteration:
                    stream.stop()
                    raise TimeoutError from None

        with patch("lib.stream.connect", return_value=Socket()):
            stream._run()
        self.assertEqual(latest, [tick(12)["generated_at"], tick(12)["generated_at"], tick(13)["generated_at"]])


class FleetViewCache(unittest.TestCase):
    def setUp(self):
        functions = page_functions("fleet_view", "line_spec", "local_time", "worst_label", "load", keep_decorators=True,
                                   categories=["cpu_temp"], UNITS={"cpu_temp": "C"})
        self.view = functions["fleet_view"]
        self.view.clear()
        self.addCleanup(self.view.clear)

    @staticmethod
    def reading(second, anomalies):
        sample = tick(second)
        sample["summary"]["cpu_temp"].update(anomalies=anomalies, worst=90 + anomalies, average=50 + anomalies,
                                            worst_server="S0001", worst_sensor="CPU0")
        sample["alerts"] = [{"server": "S0001", "category": "cpu_temp", "sensor": "CPU0", "value": 90 + anomalies,
                             "threshold": 90, "generated_at": sample["generated_at"]}]
        return sample

    def render(self, samples):
        return self.view(samples[-1]["generated_at"], samples[0]["generated_at"], len(samples), 300, "en", "UTC",
                         fingerprint(samples), tuple(samples))

    def test_different_middle_history_has_its_own_total_chart_and_alert(self):
        first = self.render([self.reading(1, 1), self.reading(2, 20), self.reading(4, 4)])
        second = self.render([self.reading(1, 1), self.reading(3, 30), self.reading(4, 4)])
        self.assertEqual(first["alerts_in_window"], 25)
        self.assertEqual(second["alerts_in_window"], 35)
        self.assertEqual(second["charts"]["cpu_temp"]["layer"][0]["data"]["values"][3]["v"], 120)
        self.assertEqual(second["alerts"].to_pylist()[0]["% of limit"], 133.3)
        self.assertIsNot(first, second)

    def test_changed_same_instant_content_invalidates_but_equal_content_reuses(self):
        first = self.render([self.reading(1, 1), self.reading(2, 20), self.reading(4, 4)])
        same = self.render([self.reading(1, 1), self.reading(2, 20), self.reading(4, 4)])
        self.assertIs(first, same)  # Real resource reuse, including newly allocated equal dictionaries.
        changed = self.render([self.reading(1, 1), self.reading(2, 30), self.reading(4, 4)])
        self.assertEqual(changed["alerts_in_window"], 35)
        self.assertEqual(changed["charts"]["cpu_temp"]["layer"][0]["data"]["values"][3]["v"], 120)
        self.assertIsNot(first, changed)


class GridBoundary(unittest.TestCase):
    def test_classification_uses_unrounded_value_on_both_sides(self):
        functions = page_functions("grid_cells", "grid_spec", "severity", "load")
        for category, value, threshold, expected in [
            ("cpu_temp", 90.01, 90, "alert"), ("cpu_temp", 90, 90, "warning"),
            ("cpu_temp", 89.99, 90, "warning"), ("fan_rpm", 2999.99, 3000, "alert"),
            ("fan_rpm", 3000, 3000, "warning"), ("fan_rpm", 3000.01, 3000, "warning"),
        ]:
            with self.subTest(category=category, value=value):
                reading = dict(server="S0001", category=category, sensor="0", value=value, threshold=threshold)
                cell = functions["grid_cells"]("tick", "all", 90, "en", [reading])[0]
                self.assertEqual(cell["state"], expected)
                # The Vega threshold conditions consume this numeric field, not rounded text.
                self.assertEqual(cell["percent"] > 100, expected == "alert")


class LiveBars(unittest.TestCase):
    def test_trends_renders_latest_bars_and_line_charts_on_each_refresh(self):
        st = MagicMock(session_state={})
        st.tabs.side_effect = lambda labels, **_kwargs: [MagicMock(open=index == 0) for index, _label in enumerate(labels)]
        functions = page_functions("bar_spec", "detail_views", st=st, categories=["cpu_temp", "fan_rpm"])
        view = {"warning": 90, "charts": {"cpu_temp": {"mark": "line"}, "fan_rpm": {"mark": "line"}},
                "summary": {"cpu_temp": {"worst": 72, "threshold": 90, "alert_below": False},
                            "fan_rpm": {"worst": 6000, "threshold": 3000, "alert_below": True}}}
        with patch("lib.ui.keep_option"):
            functions["detail_views"](view, None)
            first = st.vega_lite_chart.call_args.kwargs["spec"]
            self.assertEqual(first["layer"][0]["mark"]["type"], "bar")
            self.assertEqual([p["percent"] for p in first["layer"][0]["data"]["values"]], [80, 50])
            self.assertEqual(first["layer"][1]["data"]["values"], [{"limit": 100}])
            self.assertEqual(st.columns.return_value.__getitem__.return_value.vega_lite_chart.call_count, 2)
            view["summary"]["cpu_temp"]["worst"] = 99
            functions["detail_views"](view, None)
            refreshed = st.vega_lite_chart.call_args.kwargs["spec"]
            self.assertAlmostEqual(refreshed["layer"][0]["data"]["values"][0]["percent"], 110)
            self.assertNotEqual(first, refreshed)


class State(dict):
    def __getattr__(self, name):
        return self[name]

    def __setattr__(self, name, value):
        self[name] = value


class ImportFeedback(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.st = MagicMock(session_state=self.state)
        self.st.columns.return_value = [MagicMock(), MagicMock()]
        self.st.button.return_value = True
        self.st.selectbox.side_effect = lambda _label, options, **_kwargs: options[0]

    def render(self, content, response):
        self.st.file_uploader.return_value = [SimpleNamespace(name="records.csv", getvalue=lambda: content)]
        with patch("streamlit.session_state", self.state), patch("lib.api.st", self.st), \
                patch("lib.api.requests.request", return_value=response) as request:
            with patch("lib.session.can_edit", return_value=True), patch.dict("sys.modules", {"streamlit": self.st}):
                runpy.run_path(str(ROOT / "views/import_data.py"))
        return request.call_args.kwargs["headers"]["Idempotency-Key"] if request.called else None

    @staticmethod
    def response(status=200, replay=False):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps({"imported": 1} if status == 200 else {"detail": "Invalid file"}).encode()
        if replay:
            response.headers["Idempotent-Replayed"] = "true"
        return response

    def test_failed_a_successful_b_then_retry_a_reports_actual_insert(self):
        a, b = b"title,value\nA,1", b"title,value\nB,2"
        key = self.render(a, self.response(422))
        self.render(b, self.response())
        self.assertEqual(self.render(a, self.response()), key)
        digest = hashlib.sha256(b"text/csv\0" + a).hexdigest()
        self.assertEqual(self.state.import_outcomes[digest], ("imported", 1))
        self.assertIsNone(self.render(a, self.response(replay=True)))
        self.assertEqual(self.state.import_outcomes[digest], ("imported", 1))

    def test_replay_response_is_authoritative_even_without_previous_outcome(self):
        self.render(b"title,value\nA,1", self.response(replay=True))
        self.assertEqual(list(self.state.import_outcomes.values()), [("again", None)])

    def test_preview_includes_last_row_without_submitting_csv_or_wrapped_json(self):
        csv = "title,value,category,recorded_at,server\n" + "\n".join(
            f"Reading {index},{index:04d},cpu_temp,2026-10-14T00:00:00Z," for index in range(1, 13))
        rows = [{"title": f"Reading {index}", "value": index} for index in range(1, 13)]
        for name, content, expected_value in [
            ("records.csv", csv.encode(), "0012"),
            ("records.json", json.dumps({"records": rows}).encode(), 12),
        ]:
            with self.subTest(name=name):
                self.st.reset_mock()
                self.st.button.return_value = False
                self.st.file_uploader.return_value = [SimpleNamespace(name=name, getvalue=lambda content=content: content)]
                with patch("lib.session.can_edit", return_value=True), patch("lib.api.post_once") as post, \
                        patch.dict("sys.modules", {"streamlit": self.st}):
                    runpy.run_path(str(ROOT / "views/import_data.py"))
                post.assert_not_called()
                self.assertEqual(self.st.dataframe.call_count, 2)
                preview = self.st.dataframe.call_args_list[0].args[0]
                self.assertEqual(len(preview), 12)
                self.assertEqual(preview.iloc[-1]["title"], "Reading 12")
                self.assertEqual(preview.iloc[-1]["value"], expected_value)
                if name.endswith(".csv"):
                    self.assertEqual(preview.iloc[-1]["server"], "")


if __name__ == "__main__":
    unittest.main()
