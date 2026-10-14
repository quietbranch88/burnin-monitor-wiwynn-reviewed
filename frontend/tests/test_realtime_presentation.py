"""Presentation checks; the separate recovery suite owns stream/cache behavior."""

import ast
import json
import unittest
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pyarrow as pa
from lib import ui
from lib.config import LABELS
from streamlit.proto.WidgetStates_pb2 import WidgetStates
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


def helpers():
    tree = ast.parse((ROOT / "views/realtime.py").read_text(encoding="utf-8"))
    names = {"tile_palette", "tiles_html", "severity", "line_spec", "bar_spec", "detail_views", "pick_server"}
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    scope = {"ui": ui, "escape": escape, "categories": list(LABELS),
             "t": lambda text, **values: text.format(**values)}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), "realtime.py", "exec"), scope)
    return scope


def contrast(first, second):
    def luminance(color):
        components = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in components]
        return sum(c * weight for c, weight in zip(linear, [0.2126, 0.7152, 0.0722], strict=True))
    values = sorted([luminance(first), luminance(second)])
    return (values[1] + 0.05) / (values[0] + 0.05)


class RealtimePresentation(unittest.TestCase):
    def setUp(self):
        self.scope = helpers()
        self.summary = {category: {"worst": 50, "threshold": 100, "average": 45,
                                   "alert_below": False, "worst_server": "S0001", "worst_sensor": "0"}
                        for category in LABELS}
        self.summary["fan_rpm"].update(worst=6000, threshold=3000, average=7000, alert_below=True)
        self.view = {"summary": self.summary, "warning": 90}

    def test_all_seven_cards_render_with_per_viewer_theme_and_readable_text(self):
        light = self.scope["tiles_html"](self.view, "light")
        dark = self.scope["tiles_html"](self.view, "dark")
        self.assertNotEqual(light, dark)
        self.assertEqual(light, self.scope["tiles_html"](self.view, "light"))
        for theme, html in [("light", light), ("dark", dark)]:
            palette = self.scope["tile_palette"](theme)
            self.assertEqual(html.count('title="Limit '), 7)
            for category in LABELS:
                self.assertIn(escape(ui.label(category)), html)
            for field in ("text", "muted"):
                self.assertGreaterEqual(contrast(palette[field], palette["surface"]), 4.5)
        self.assertNotIn("background:#FFFFFF", dark)

    def test_html_escapes_source_identifiers_and_preserves_unrounded_alert_state(self):
        self.summary["cpu_temp"].update(worst=100.001, worst_server='<img src=x onerror="x">')
        self.summary["fan_rpm"].update(worst=2999.99)
        html = self.scope["tiles_html"](self.view, "dark")
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)
        self.assertEqual(html.count(f"border-left:3px solid {ui.RED}"), 2)
        self.assertEqual(html.count("100% ·"), 2)  # display rounds, severity does not

    def test_line_series_accent_has_contrast_in_both_modes_without_changing_data(self):
        points = [{"t": "2026-10-14T00:00:00", "series": "fleet average", "v": 42.75}]
        chart = self.scope["line_spec"]("CPU temp", points, "fleet max")
        layer = chart["layer"][0]
        self.assertIs(layer["data"]["values"], points)
        self.assertEqual(layer["mark"]["type"], "line")
        accent = layer["encoding"]["color"]["scale"]["range"][1]
        for surface in ("#FFFFFF", "#12151A"):
            self.assertGreaterEqual(contrast(accent, surface), 3)

    def test_latest_bar_keeps_all_metrics_and_inverse_fan_limit(self):
        with patch("lib.ui.format_value", side_effect=lambda value, _category: str(value)):
            chart = self.scope["bar_spec"](self.summary, 90)
        points = chart["layer"][0]["data"]["values"]
        self.assertEqual(len(points), 7)
        self.assertEqual(points[-1]["percent"], 50)
        self.assertEqual(chart["layer"][0]["mark"]["type"], "bar")
        self.assertEqual(chart["layer"][1]["data"]["values"], [{"limit": 100}])


class State(dict):
    def __setattr__(self, name, value):
        self[name] = value


class LiveDetailNavigation(unittest.TestCase):
    def setUp(self):
        self.scope = helpers()
        self.state = State({"live-grid-key": "fleet-grid"})
        self.st = MagicMock(session_state=self.state)
        self.scope.update(st=self.st, i18n=SimpleNamespace(lang=lambda: "en"),
                          config={"servers": 2}, load=lambda reading: reading["value"])
        self.details = self.scope["server_details"] = MagicMock()
        self.table = self.scope["server_table"] = MagicMock(return_value=pa.Table.from_pylist([
            {"server": "S0001"}, {"server": "S0017"}]))
        self.readings = [{"server": "S0001", "value": 70, "generated_at": "2026-10-14T00:00:00Z"},
                         {"server": "S0017", "value": 80, "generated_at": "2026-10-14T00:00:00Z"}]
        self.view = {"warning": 90, "summary": {}, "charts": {category: {"mark": "line"} for category in LABELS}}
        self.enterContext(patch("lib.ui.keep_option"))
        self.st.tabs.side_effect = self.tabs

    def tabs(self, labels, *, key, on_change):
        # Streamlit 1.64's documented contract: keyed state is a label and .open
        # identifies the active container. Browser interaction remains an E2E gate.
        self.on_change = on_change
        return [MagicMock(open=label == self.state[key]) for label in labels]

    def render(self):
        self.scope["detail_views"](self.view, self.readings)

    def test_native_tabs_have_plain_labels_and_only_default_charts_execute(self):
        self.render()
        self.assertEqual(self.st.tabs.call_args.args[0], ["Reading trends", "Server details", "All servers"])
        self.assertEqual(self.state["live-details-tabs:0"], "Reading trends")
        self.assertEqual(self.st.vega_lite_chart.call_count, 1)
        self.assertEqual(self.st.columns.return_value.__getitem__.return_value.vega_lite_chart.call_count, 7)
        self.details.assert_not_called()
        self.table.assert_not_called()

    def test_tab_callback_keeps_stable_id_and_skips_inactive_charts(self):
        self.render()
        self.state["live-details-tabs:0"] = "Server details"
        self.on_change()
        self.assertEqual(self.state["keep:live-view"], "details")
        self.st.vega_lite_chart.reset_mock()
        self.render()
        self.assertEqual(self.state["live-details-tabs:0"], "Server details")
        self.details.assert_called_once_with(["S0001", "S0017"], "S0017")
        self.table.assert_not_called()
        self.st.vega_lite_chart.assert_not_called()

    def test_grid_selection_opens_details_and_label_translation_keeps_identity(self):
        self.state["fleet-grid"] = {"selection": {"pick": [{"server": "S0017"}]}}
        self.scope["pick_server"]("fleet-grid")
        self.assertEqual(self.state["detail_server"], "S0017")
        self.assertEqual(self.state["keep:live-view"], "details")
        self.scope["t"] = lambda text, **_values: {"Server details": "伺服器詳細資料"}.get(text, text)
        self.render()
        self.assertEqual(self.state["live-details-tabs:1"], "伺服器詳細資料")
        self.details.assert_called_once()
        self.st.vega_lite_chart.assert_not_called()

    def test_all_servers_shows_count_separately_and_only_table_executes(self):
        self.state["keep:live-view"] = "servers"
        self.render()
        self.assertEqual(self.state["live-details-tabs:0"], "All servers")
        self.assertIn(("2 servers",), [call.args for call in self.st.caption.call_args_list])
        self.table.assert_called_once()
        self.details.assert_not_called()
        self.st.vega_lite_chart.assert_not_called()

    def test_old_callback_cannot_override_grid_but_current_callback_can_navigate(self):
        self.render()
        old_callback = self.on_change
        self.state["fleet-grid"] = {"selection": {"pick": [{"server": "S0017"}]}}
        self.scope["pick_server"]("fleet-grid")
        self.state["live-details-tabs:0"] = "All servers"
        old_callback()
        self.assertEqual(self.state["keep:live-view"], "details")
        self.render()
        self.state["live-details-tabs:1"] = "All servers"
        self.on_change()
        self.assertEqual(self.state["keep:live-view"], "servers")


class NativeLiveNavigation(unittest.TestCase):
    """Real Streamlit callback ordering, without a browser or network boundary."""

    def app(self):
        tree = ast.parse((ROOT / "views/realtime.py").read_text(encoding="utf-8"))
        names = {"detail_views", "grid_spec", "pick_server", "bar_spec", "fleet_grid", "server_details"}
        definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        code = ast.unparse(ast.Module(body=definitions, type_ignores=[]))
        script = '''
import streamlit as st
import pyarrow as pa
from types import SimpleNamespace
from lib import ui
t = lambda text, **values: text.format(**values)
categories = []
config = {'servers': 2}
ALL = 'all'
i18n = SimpleNamespace(lang=lambda: 'en')
load = lambda reading: reading['value']
api = SimpleNamespace(get=lambda *args, **kwargs: [], ApiError=RuntimeError)
def server_table(*args):
    return pa.Table.from_pylist([{'server': 'S0001'}, {'server': 'S0017'}])
def grid_cells(*args):
    return cells
''' + code + '''
readings = [{'server': 'S0001', 'value': 70, 'category': 'cpu_temp', 'sensor': '0', 'generated_at': '2026-10-14T00:00:00Z'},
            {'server': 'S0017', 'value': 80, 'category': 'cpu_temp', 'sensor': '0', 'generated_at': '2026-10-14T00:00:00Z'}]
if st.session_state.get('remove_first_server'):
    readings = readings[1:]
cells = [{'x': i, 'y': 0, 'server': r['server'], 'short': str(i), 'metric': 'CPU',
          'sensor': '0', 'value': '70 C', 'percent': 70, 'state': 'normal'}
         for i, r in enumerate(readings)]
@st.fragment(run_every='1s')
def draw():
    fleet_grid(readings, 90)
    detail_views({'summary': {}, 'warning': 90, 'charts': {}}, readings)
if st.toggle('Show Live', value=True):
    draw()
'''
        return AppTest.from_string(script, default_timeout=10).run()

    def tab_id(self, app):
        generation = app._session_state.filtered_state.get('live-tabs-generation', 0)
        return app._session_state._state._key_id_mapper._key_id_mapping[f'live-details-tabs:{generation}']

    def event(self, app, label, server=None, grid_first=False, tab_id=None):
        # AppTest 1.64 has no tab/vega click API. Feed its real widget protocol
        # into the native session runner, exercising both callback batch orders.
        states = app._tree.get_widget_states()
        mapping = app._session_state._state._key_id_mapper._key_id_mapping
        tab = states.widgets.add()
        tab.id = tab_id or self.tab_id(app)
        tab.string_value = label
        if server:
            grid = states.widgets.add()
            generation = app._session_state.filtered_state.get('live-grid-generation', 0)
            grid.id = mapping[f'fleet-grid:{generation}']
            grid.string_value = json.dumps({'selection': {'pick': [{'server': server}]}})
        if grid_first:
            states.widgets.reverse()
        app._run(states)
        self.assertFalse(app.exception)

    def test_grid_selection_wins_same_batch_tab_change_in_either_callback_order(self):
        for grid_first in (False, True):
            with self.subTest(grid_first=grid_first):
                app = self.app()
                self.event(app, 'All servers', 'S0017', grid_first)
                self.assertEqual(app.session_state['keep:live-view'], 'details')
                self.assertEqual(app.selectbox(key='detail_server').value, 'S0017')
                self.event(app, 'Server details')
                self.assertEqual(app.selectbox(key='detail_server').value, 'S0017')
                self.event(app, 'All servers')
                self.assertEqual(app.session_state['keep:live-view'], 'servers')
                self.event(app, 'All servers', 'S0001', grid_first)
                self.assertEqual(app.session_state['keep:live-view'], 'details')
                self.assertEqual(app.selectbox(key='detail_server').value, 'S0001')

    def test_delayed_old_tab_event_cannot_undo_grid_jump_and_current_tab_works(self):
        app = self.app()
        self.event(app, 'All servers')
        old_tab_id = self.tab_id(app)
        self.event(app, 'All servers', 'S0017')
        self.event(app, 'All servers', tab_id=old_tab_id)
        self.assertEqual(app.session_state['keep:live-view'], 'details')
        self.assertEqual(app.selectbox(key='detail_server').value, 'S0017')
        self.event(app, 'All servers')
        self.assertEqual(app.session_state['keep:live-view'], 'servers')

    def test_current_grid_wins_same_batch_deliberate_detail_exit(self):
        for grid_first in (False, True):
            with self.subTest(grid_first=grid_first):
                app = self.app()
                self.event(app, 'Reading trends', 'S0017')
                self.event(app, 'All servers', 'S0001', grid_first)
                self.assertEqual(app.session_state['keep:live-view'], 'details')
                self.assertEqual(app.selectbox(key='detail_server').value, 'S0001')

    def test_same_server_can_be_selected_again_after_deliberately_leaving_details(self):
        app = self.app()
        self.event(app, 'Reading trends', 'S0017')
        self.assertEqual(app.selectbox(key='detail_server').value, 'S0017')
        before = app._session_state._state._key_id_mapper._key_id_mapping['fleet-grid:0']
        self.event(app, 'All servers')
        self.assertEqual(app.session_state['live-grid-generation'], 1)
        after = app._session_state._state._key_id_mapper._key_id_mapping['fleet-grid:1']
        self.assertNotEqual(before, after)
        app.run()
        self.assertEqual(app.session_state['live-grid-generation'], 1)  # refresh alone keeps the chart identity
        self.event(app, 'All servers', 'S0017')
        self.assertEqual(app.session_state['keep:live-view'], 'details')
        self.assertEqual(app.selectbox(key='detail_server').value, 'S0017')


    def test_manual_server_survives_tab_widget_cleanup_and_page_return(self):
        app = self.app()
        self.event(app, 'Server details')
        app.selectbox(key='detail_server').set_value('S0001').run()
        self.event(app, 'All servers')
        self.assertNotIn('detail_server', app._session_state.filtered_state)
        self.event(app, 'Server details')
        self.assertEqual(app.selectbox(key='detail_server').value, 'S0001')
        app.toggle[0].set_value(False).run()
        self.assertNotIn('detail_server', app._session_state.filtered_state)
        app.toggle[0].set_value(True).run()
        self.assertEqual(app.selectbox(key='detail_server').value, 'S0001')
        self.event(app, 'Server details', 'S0017')
        self.assertEqual(app.selectbox(key='detail_server').value, 'S0017')

    def test_removed_server_falls_back_to_current_fleet_default(self):
        app = self.app()
        self.event(app, 'Server details')
        app.selectbox(key='detail_server').set_value('S0001').run()
        app.session_state['remove_first_server'] = True
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key='detail_server').value, 'S0017')

    def test_retired_grid_event_cannot_override_newer_tab_or_manual_server(self):
        app = self.app()
        self.event(app, 'Reading trends', 'S0017')
        self.event(app, 'All servers')
        self.event(app, 'Server details')
        app.selectbox(key='detail_server').set_value('S0001').run()
        old_grid_id = app._session_state._state._key_id_mapper._key_id_mapping['fleet-grid:1']
        self.event(app, 'All servers')
        self.assertEqual(app.session_state['live-grid-generation'], 2)
        states = app._tree.get_widget_states()
        retired_grid = states.widgets.add()
        retired_grid.id = old_grid_id
        retired_grid.string_value = json.dumps({'selection': {'pick': [{'server': 'S0017'}]}})
        app._run(states)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['keep:live-view'], 'servers')
        self.event(app, 'Server details')
        self.assertEqual(app.selectbox(key='detail_server').value, 'S0001')
        self.event(app, 'Server details', 'S0017')
        self.assertEqual(app.selectbox(key='detail_server').value, 'S0017')


class LiveFreshness(unittest.TestCase):
    """A connected socket with no recent tick must not claim Live or show old counts as Now."""

    def badges(self, age_seconds):
        tree = ast.parse((ROOT / "views/realtime.py").read_text(encoding="utf-8"))
        definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "status_line"]
        script = '''
import streamlit as st
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
t = lambda text, **values: text.format(**values)
stream = SimpleNamespace(status='connected')
config = {'servers': 2, 'sensors': {'cpu_temp': ['0']}, 'readings_per_second': 2}
STALE_AFTER_SECONDS = 5
load = lambda reading: 100.0
severity = lambda percent, warning: 'warning'
local_time = lambda value: value
''' + ast.unparse(ast.Module(body=definitions, type_ignores=[])) + f'''
last = (datetime.now(UTC) - timedelta(seconds={age_seconds})).isoformat()
ticks = [{{'generated_at': last, 'summary': {{'cpu_temp': {{'anomalies': 2}}}}}}]
status_line(ticks, [{{'value': 1}}], 90, last)
'''
        app = AppTest.from_string(script, default_timeout=10).run()
        self.assertFalse(app.exception)
        return " ".join(element.value for element in app.markdown)

    def test_recent_tick_is_live_with_current_counts(self):
        text = self.badges(1)
        self.assertIn("Live", text)
        self.assertIn("Now: 2 past the limit", text)

    def test_old_tick_is_reported_as_stopped_without_now_counts(self):
        text = self.badges(120)
        self.assertIn("No new data for 120 s", text)
        self.assertNotIn("Live", text)
        self.assertNotIn("Now:", text)


class NativeLiveWindow(unittest.TestCase):
    def app(self, *, empty_choice=False):
        tree = ast.parse((ROOT / "views/realtime.py").read_text(encoding="utf-8"))
        definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "status_line"]
        code = ast.unparse(ast.Module(body=definitions, type_ignores=[]))
        script = '''
import streamlit as st
from types import SimpleNamespace
t = lambda text, **values: text.format(**values)
stream = SimpleNamespace(status='connected')
config = {'servers': 2, 'sensors': {'cpu_temp': ['0']}, 'readings_per_second': 2}
''' + code + '''
st.session_state['observed_window'] = status_line([], None, 90, None)
'''
        app = AppTest.from_string(script, default_timeout=10)
        if empty_choice:
            app.session_state['keep:live-window'] = None
        return app.run()

    def test_preexisting_empty_window_visibly_returns_to_sixty_seconds(self):
        app = self.app(empty_choice=True)
        self.assertFalse(app.exception)
        self.assertEqual(app.get('button_group')[0].value, 60)
        self.assertEqual(app.session_state['observed_window'], 60)
        self.assertIn('last 60 seconds', app.caption[-1].value)

    def test_window_requires_selection_and_repeated_valid_selection_stays_aligned(self):
        app = self.app()
        self.assertFalse(app.exception)
        self.assertTrue(app.get('button_group')[0].proto.required)
        for seconds in (120, 120, 300, 30, 60):
            # AppTest accepts even invalid empty protocol values; required deselection
            # prevention belongs to the browser. Exercise only valid values here.
            states = WidgetStates()
            state = states.widgets.add(id=app.get('button_group')[0].proto.id)
            state.string_array_value.data.append(f'{seconds} s')
            app._run(states)
            self.assertFalse(app.exception)
            self.assertEqual(app.get('button_group')[0].value, seconds)
            self.assertEqual(app.session_state['observed_window'], seconds)
            self.assertIn(f'last {seconds} seconds', app.caption[-1].value)


if __name__ == "__main__":
    unittest.main()
