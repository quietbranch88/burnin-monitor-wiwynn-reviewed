"""Stream recovery, the Alerts route and database diagnostics."""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


def function(relative, name, scope):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), relative, "exec"), scope)
    return scope[name]


class NavigationAndDiagnostics(unittest.TestCase):
    def test_idle_dead_reader_requests_full_app_recovery_before_snapshot(self):
        class AppRerun(Exception):
            pass

        stream = MagicMock(status="connected", alive=False)
        st = MagicMock()
        st.rerun.side_effect = AppRerun
        live = function("views/realtime.py", "live", {"st": st, "stream": stream})
        with self.assertRaises(AppRerun):
            live()
        st.rerun.assert_called_once_with(scope="app")
        stream.snapshot_keyed.assert_not_called()

    def test_alerts_remains_a_registered_monitoring_route_for_all_signed_in_roles(self):
        st = SimpleNamespace(Page=lambda source, **kwargs: SimpleNamespace(source=source, **kwargs))
        groups = function("app.py", "page_groups", {"st": st, "t": lambda value: value})()
        alerts = [page for page in groups["Monitoring"] if page.url_path == "alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].source, "views/alerts.py")

    def test_database_diagnostics_keep_generator_alert_writer_and_backoff(self):
        writer = {"pending": 2, "written": 19, "dropped": 0, "backoff_seconds": 8,
                  "last_flush_at": "2026-10-14T00:00:00Z", "last_error": None}
        status = {"server_version": "11.7", "ping_ms": 2, "websocket_clients": 3,
                  "generator_running": False, "skipped_ticks": 0,
                  "table_rows": {"records": 5, "realtime_alerts": 9}, "approximate_rows": [],
                  "pool": {"checked_in": 1, "checked_out": 2, "configured_size": 10,
                           "overflow": 0, "configured_max_overflow": 20},
                  **{name: writer for name in ("realtime_writer", "minute_writer", "alert_writer", "log_writer")}}
        with patch("lib.session.is_admin", return_value=True), patch("lib.api.get", return_value=status):
            app = AppTest.from_file(str(ROOT / "views/admin_database.py"), default_timeout=10).run()
        self.assertFalse(app.exception)
        metrics = {metric.label: metric.value for metric in app.metric}
        self.assertEqual(metrics["Connection"], "OK")
        self.assertEqual(metrics["Fleet generator"], "off")
        self.assertEqual(metrics["Alert events"], "9")
        frame = app.dataframe[0].value
        self.assertIn("alert events", frame["writer"].tolist())
        self.assertEqual(frame["backoff_seconds"].tolist(), [8, 8, 8, 8])


if __name__ == "__main__":
    unittest.main()
