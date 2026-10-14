"""Multi-file import contracts through the real HTTP/idempotency client."""

import hashlib
import json
import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]


def upload(name, title, rows=1):
    content = ("title,value,category,recorded_at,server\n" + "\n".join(
        f"{title} {index},0012,cpu_temp,2026-10-14T00:00:00Z," for index in range(rows))).encode()
    return SimpleNamespace(name=name, getvalue=lambda: content)


def response(status=200, replay=False):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps({"imported": 1} if status == 200 else {"detail": "Invalid file"}).encode()
    if replay:
        result.headers["Idempotent-Replayed"] = "true"
    return result


class BatchImport(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.st = MagicMock(session_state=self.state)

    def render(self, files, responses=(), *, submit=False, selected=0):
        self.st.reset_mock()
        self.st.file_uploader.return_value = files
        self.st.button.return_value = submit
        self.st.selectbox.side_effect = lambda _label, options, **_kwargs: options[selected]
        with patch("streamlit.session_state", self.state), patch("lib.api.st", self.st), \
                patch("lib.api.requests.request", side_effect=responses) as request, \
                patch("lib.session.can_edit", return_value=True), patch.dict("sys.modules", {"streamlit": self.st}):
            runpy.run_path(str(ROOT / "views/import_data.py"))
        return request

    def test_picker_accepts_multiple_files_without_automatic_request(self):
        request = self.render(None)
        self.assertTrue(self.st.file_uploader.call_args.kwargs.get("accept_multiple_files"))
        request.assert_not_called()

    def test_partial_failure_retains_success_and_retry_sends_only_failed_file(self):
        files = [upload("a.csv", "A"), upload("b.csv", "B"), upload("bad.csv", "Bad")]
        first = self.render(files, [response(), response(), response(422)], submit=True)
        self.assertEqual(first.call_count, 3)
        failed_key = first.call_args_list[2].kwargs["headers"]["Idempotency-Key"]
        retry = self.render(files, [response(), response(), response()], submit=True)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(retry.call_args.kwargs["files"]["file"][1], files[2].getvalue())
        self.assertEqual(retry.call_args.kwargs["headers"]["Idempotency-Key"], failed_key)
        self.render(files, submit=True).assert_not_called()
        self.assertEqual([self.state["import_outcomes"][hashlib.sha256(b"text/csv\0" + f.getvalue()).hexdigest()][0] for f in files],
                         ["imported", "imported", "imported"])

    def test_lost_response_key_survives_more_than_fifty_other_files(self):
        files = [upload(f"{index}.csv", f"Row{index}") for index in range(52)]
        first = self.render(files, [requests.Timeout(), *[response() for _ in range(51)]], submit=True)
        self.assertEqual(first.call_count, 52)
        original = first.call_args_list[0].kwargs
        retry = self.render(files, [response(replay=True)], submit=True)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(retry.call_args.kwargs["files"], original["files"])
        self.assertEqual(retry.call_args.kwargs["headers"]["Idempotency-Key"], original["headers"]["Idempotency-Key"])
        digest = hashlib.sha256(b"text/csv\0" + files[0].getvalue()).hexdigest()
        self.assertEqual(self.state["import_outcomes"][digest][0], "again")

    def test_identical_content_deduplicates_but_same_filename_different_content_imports(self):
        first = upload("same.csv", "A")
        copy = SimpleNamespace(name="copy.csv", getvalue=first.getvalue)
        other = upload("same.csv", "B")
        request = self.render([first, copy, other], [response(), response()], submit=True)
        self.assertEqual(request.call_count, 2)
        self.assertNotEqual(request.call_args_list[0].kwargs["files"]["file"][1],
                            request.call_args_list[1].kwargs["files"]["file"][1])
        summary = self.st.dataframe.call_args_list[-1].args[0]
        self.assertEqual(len(summary), 3)
        self.assertEqual(summary[1]["Status"], "Identical content — skipped")
        self.assertEqual([row["Imported records"] for row in summary], ["1", "—", "1"])
        self.assertEqual([call.args[0] for call in self.st.text.call_args_list],
                         ["1. same.csv", "2. copy.csv", "3. same.csv"])
        self.st.caption.assert_any_call("3 files selected · 2 unique files · 1 identical copies")

    def test_full_filename_and_pending_count_are_clear_before_submission(self):
        name = "factory_line_07_station_42_complete_measurement_export_2026_10_14.csv"
        self.render([upload(name, "A")]).assert_not_called()
        self.st.text.assert_called_once_with("1. " + name)
        summary = self.st.dataframe.call_args_list[-1].args[0]
        self.assertEqual(summary[0]["Status"], "Pending")
        self.assertEqual(summary[0]["Imported records"], "—")

    def test_switching_full_preview_does_not_post_or_show_another_files_outcome(self):
        files = [upload("same.csv", "A", 12), upload("same.csv", "B", 13)]
        for selected, title, count in [(0, "A 11", 12), (1, "B 12", 13)]:
            self.render(files, selected=selected).assert_not_called()
            preview = self.st.dataframe.call_args_list[0].args[0]
            self.assertIsInstance(preview, pd.DataFrame)
            self.assertEqual(len(preview), count)
            self.assertEqual(preview.iloc[-1]["title"], title)
            self.assertEqual(preview.iloc[-1]["value"], "0012")
            self.st.success.assert_not_called()

    def test_equal_bytes_with_different_parser_formats_are_not_deduplicated(self):
        csv = upload("data.csv", "CSV")
        wrong_format = SimpleNamespace(name="data.json", getvalue=csv.getvalue)
        request = self.render([csv, wrong_format], [response(), response(422)], submit=True)
        self.assertEqual(request.call_count, 2)
        first, second = (call.kwargs for call in request.call_args_list)
        self.assertEqual(first["files"]["file"][1], second["files"]["file"][1])
        self.assertNotEqual(first["headers"]["Idempotency-Key"], second["headers"]["Idempotency-Key"])
        self.assertEqual(second["files"]["file"][2], "application/json")

    def test_selected_success_does_not_display_another_files_failure(self):
        files = [upload("a.csv", "A"), upload("bad.csv", "Bad")]
        self.render(files, [response(), response(422)], submit=True, selected=0)
        self.render(files, selected=1).assert_not_called()
        self.st.error.assert_called_once()
        self.st.success.assert_not_called()
        self.render(files, selected=0).assert_not_called()
        self.st.error.assert_not_called()
        self.st.success.assert_called_once()


if __name__ == "__main__":
    unittest.main()
