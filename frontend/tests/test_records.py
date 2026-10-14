"""Exercise record selection across real Streamlit reruns; only HTTP is substituted."""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


def record(identifier, title):
    return {"id": identifier, "title": title, "value": 42.0, "category": "cpu_temp",
            "server": "S0001", "owner_id": 7, "owner_username": "engineer",
            "recorded_at": "2026-10-14T00:00:00Z", "created_at": "2026-10-14T00:00:00Z",
            "updated_at": "2026-10-14T00:00:00Z"}


class RecordsSave(unittest.TestCase):
    def setUp(self):
        self.rows = [record(1, "First record"), record(2, "Second record")]
        self.saved = []
        self.created = []
        self.deleted = []
        self.requested = []
        self.fail_create = False
        self.remove_saved = False
        self.fail_save = False
        self.paginate = False
        self.enterContext(patch("requests.request", side_effect=self.request))
        self.app = AppTest.from_file(str(ROOT / "views/records.py"), default_timeout=15)
        self.app.session_state["user"] = {"id": 7, "role": "user"}
        self.app.session_state["display_zone"] = "UTC"
        self.app.run()
        self.assertFalse(self.app.exception)

    def request(self, method, url, **kwargs):
        status = 200
        if url.endswith("/records/categories"):
            body = ["cpu_temp"]
        elif method == "POST" and url.endswith("/records"):
            self.created.append((kwargs["json"], kwargs["headers"]))
            body = {**record(9, ""), **kwargs["json"]}
            if self.fail_create:
                status, body = 503, {"detail": "Temporarily unavailable"}
        elif method == "DELETE":
            identifier = int(url.rsplit("/", 1)[1])
            self.deleted.append(identifier)
            self.rows = [row for row in self.rows if row["id"] != identifier]
            body = None
        elif method == "PATCH":
            identifier = int(url.rsplit("/", 1)[1])
            self.saved.append((identifier, kwargs["json"]))
            current = next((row for row in self.rows if row["id"] == identifier), record(identifier, ""))
            body = {**current, **kwargs["json"]}
            if self.fail_save:
                status, body = 503, {"detail": "Temporarily unavailable"}
            elif self.remove_saved:
                self.rows = [r for r in self.rows if r["id"] != identifier]
            else:
                self.rows = [body if r["id"] == identifier else r for r in self.rows]
        elif url.endswith("/records"):
            self.requested.append(dict(kwargs["params"]))
            if self.paginate:
                page, size = kwargs["params"]["page"], kwargs["params"]["page_size"]
                body = {"items": self.rows[(page - 1) * size:page * size], "total": len(self.rows),
                        "page": page, "pages": (len(self.rows) + size - 1) // size}
            else:
                body = {"items": self.rows, "total": len(self.rows), "page": 1, "pages": 2}
        else:
            raise AssertionError((method, url))
        return Mock(status_code=status, content=b"json", json=lambda: body)

    def save(self):
        self.open_edit()
        next(button for button in self.app.button if button.label == "Save changes").click().run()
        self.assertFalse(self.app.exception)
        self.assertIn("Save changes", [button.label for button in self.app.button])

    def open_edit(self):
        if not any(button.label == "Save changes" for button in self.app.button):
            next(button for button in self.app.button if button.label == "Edit record").click().run()
            self.assertFalse(self.app.exception)

    def close_edit(self):
        if any(button.key == "records-edit-close" for button in self.app.button):
            self.app.button(key="records-edit-close").click().run()

    def test_forms_require_an_explicit_record_action(self):
        labels = [button.label for button in self.app.button]
        self.assertNotIn("Create", labels)
        self.assertNotIn("Save changes", labels)
        self.assertNotIn("Delete", labels)
        self.assertIn("New record", labels)
        self.assertIn("Edit record", labels)

    def test_compact_filters_preserve_request_parameters_and_reset_page(self):
        self.app.number_input(key="keep:records-page").set_value(2).run()
        self.app.text_input(key="keep:records-q").set_value("burn in")
        self.app.selectbox(key="keep:records-category").set_value("cpu_temp")
        self.app.text_input(key="keep:records-server").set_value("17")
        self.app.number_input(key="keep:records-min").set_value(0.0)
        self.app.number_input(key="keep:records-max").set_value(80.0)
        self.app.selectbox(key="keep:records-sort").set_value("value")
        self.app.radio(key="keep:records-order").set_value("asc")
        self.app.toggle(key="keep:records-mine").set_value(True)
        self.app.selectbox(key="keep:records-rows").set_value(50).run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.requested[-1], {
            "page": 1, "page_size": 50, "sort": "value", "order": "asc", "mine": True,
            "category": "cpu_temp", "server": "S0017", "q": "burn in", "min_value": 0.0, "max_value": 80.0,
        })
        captions = [caption.value for caption in self.app.caption]
        summary = next(value for value in captions if value.startswith("Active filters"))
        for expected in ("Title contains: burn in", "Category: CPU temp", "Server: S0017",
                         "Min value: 0.0", "Max value: 80.0", "Only my records"):
            self.assertIn(expected, summary)
        self.assertIn("Sort by: Value · Smallest first · Rows per page: 50", captions)
        self.assertEqual(len(self.app.expander), 0)

    def test_custom_date_range_is_visible_in_summary_and_sent_with_original_bounds(self):
        from datetime import date

        self.app.selectbox(key="keep:records-preset").set_value("Custom").run()
        self.app.date_input(key="keep:records-dates").set_value((date(2026, 10, 1), date(2026, 10, 3))).run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.requested[-1]["start"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(self.requested[-1]["end"], "2026-10-03T23:59:59.999999+00:00")
        summary = next(caption.value for caption in self.app.caption if caption.value.startswith("Active filters"))
        self.assertIn("Time range: Custom · 2026-10-01 – 2026-10-03", summary)

    def test_closing_edit_dialog_does_not_write_or_reopen(self):
        self.select(2)
        self.assertIn("Second record", [field.value for field in self.app.text_input])
        self.close_edit()
        self.app.run()
        self.assertNotIn("Save changes", [button.label for button in self.app.button])
        self.assertNotIn("records_dialog", self.app.session_state)
        self.assertEqual(self.saved, [])

    def fill_create(self):
        next(button for button in self.app.button if button.label == "New record").click().run()
        self.assertFalse(self.app.exception)
        self.app.text_input(key="create-0-title").set_value("Created through dialog")
        self.app.text_input(key="create-0-server").set_value("S0001")
        self.app.selectbox(key="create-0-category").set_value("cpu_temp")

    def test_create_closes_dialog_and_selects_returned_record_for_edit(self):
        self.fill_create()
        next(button for button in self.app.button if button.label == "Create").click().run()
        self.assertFalse(self.app.exception)
        self.assertNotIn("records_dialog", self.app.session_state)
        self.assertNotIn("Create", [button.label for button in self.app.button])
        self.assertEqual(self.app.session_state["records_edit_id"], 9)
        self.assertEqual(len(self.created), 1)
        self.save()
        self.save()
        self.assertEqual([identifier for identifier, _ in self.saved], [9, 9])

    def test_failed_create_keeps_input_and_retry_reuses_request_identity(self):
        self.fail_create = True
        self.fill_create()
        next(button for button in self.app.button if button.label == "Create").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.text_input(key="create-0-title").value, "Created through dialog")
        self.fail_create = False
        next(button for button in self.app.button if button.label == "Retry").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(len(self.created), 2)
        self.assertEqual(self.created[0], self.created[1])
        self.assertNotIn("records_dialog", self.app.session_state)

    def test_delete_requires_confirmation_and_closes_after_confirmed_mock_response(self):
        self.select(2)
        next(button for button in self.app.button if button.label == "Delete").click().run()
        self.assertEqual(self.deleted, [])
        self.assertIn("Tick the box first to delete the record.", [warning.value for warning in self.app.warning])
        next(box for box in self.app.checkbox if box.label == "I want to delete record #2").check()
        next(button for button in self.app.button if button.label == "Delete").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.deleted, [2])
        self.assertNotIn("records_dialog", self.app.session_state)
        self.assertNotIn("Delete", [button.label for button in self.app.button])

    def select(self, identifier):
        self.close_edit()
        next(box for box in self.app.selectbox if box.key.startswith("records-edit-")).set_value(identifier).run()
        self.open_edit()

    def test_repeated_save_keeps_off_page_created_record_and_updated_values(self):
        self.app.session_state["records_just_created"] = (
            record(9, "Created off page"), self.app.session_state["records-filters"], 1)
        self.app.session_state["records_edit_id"] = 9
        self.app.run()
        self.open_edit()
        next(field for field in self.app.text_input if field.value == "Created off page").set_value("Updated off page")
        self.save()
        self.assertEqual(self.app.session_state["records_edit_id"], 9)
        self.assertIn("Updated off page", [field.value for field in self.app.text_input])
        self.save()
        self.assertEqual([identifier for identifier, _ in self.saved], [9, 9])
        self.assertEqual(self.saved[-1][1]["title"], "Updated off page")

    def test_edit_that_moves_existing_record_off_page_keeps_target(self):
        self.select(2)
        self.remove_saved = True
        self.save()
        self.assertEqual(self.app.session_state["records_edit_id"], 2)
        self.save()
        self.assertEqual([identifier for identifier, _ in self.saved], [2, 2])

    def test_failed_save_preserves_target_and_inputs(self):
        self.select(2)
        self.fail_save = True
        next(field for field in self.app.text_input if field.value == "Second record").set_value("Retry title")
        self.save()
        self.assertEqual(self.app.session_state["records_edit_id"], 2)
        self.assertIn("Retry title", [field.value for field in self.app.text_input])
        self.fail_save = False
        self.save()
        self.assertEqual([identifier for identifier, _ in self.saved], [2, 2])

    def pin_off_page(self):
        self.app.session_state["records_just_created"] = (
            record(9, "Off page"), self.app.session_state["records-filters"], 1)
        self.app.session_state["records_edit_id"] = 9
        self.app.run()

    def test_explicit_selection_releases_pinned_record(self):
        self.pin_off_page()
        self.select(2)
        self.assertNotIn("records_just_created", self.app.session_state)
        self.save()
        self.assertEqual(self.saved[-1][0], 2)

    def test_filter_change_releases_pinned_record(self):
        self.pin_off_page()
        self.close_edit()
        self.app.text_input(key="keep:records-q").set_value("First").run()
        self.assertNotIn("records_just_created", self.app.session_state)
        self.assertNotEqual(self.app.session_state["records_edit_id"], 9)

    def test_page_change_releases_pinned_record(self):
        self.pin_off_page()
        self.close_edit()
        self.app.number_input(key="keep:records-page").set_value(2).run()
        self.assertNotIn("records_just_created", self.app.session_state)
        self.assertNotEqual(self.app.session_state["records_edit_id"], 9)

    def test_fresh_list_row_takes_precedence_over_saved_snapshot(self):
        self.save()
        self.rows[0] = record(1, "New server-side title")
        self.app.run()
        self.assertIn("New server-side title", [field.value for field in self.app.text_input])
        self.save()
        self.assertEqual(self.saved[-1][1]["title"], "New server-side title")

    def test_last_matching_row_on_last_page_keeps_target_after_automatic_page_clamp(self):
        self.paginate = True
        self.rows = [record(i, f"Record {i}") for i in range(1, 22)]
        self.app.run()
        self.app.number_input(key="keep:records-page").set_value(2).run()
        self.assertEqual(self.app.session_state["records_edit_id"], 21)
        self.remove_saved = True
        self.save()
        self.assertEqual(self.app.session_state["keep:records-page"], 1)
        self.assertEqual(self.app.session_state["records_edit_id"], 21)
        self.save()
        self.assertEqual([identifier for identifier, _ in self.saved], [21, 21])

    def test_demoted_admin_cannot_keep_editing_another_owners_pinned_record(self):
        self.app.session_state["user"] = {"id": 7, "role": "admin"}
        self.rows[1] = {**self.rows[1], "owner_id": 8, "owner_username": "another-engineer"}
        self.app.run()
        self.select(2)
        self.save()
        self.app.session_state["user"] = {"id": 7, "role": "user"}
        self.app.run()
        selector = next(box for box in self.app.selectbox if box.key.startswith("records-edit-"))
        self.assertFalse(any(label.startswith("#2 ") for label in selector.options))
        self.assertNotIn("records_just_created", self.app.session_state)
        self.assertEqual(self.app.session_state["records_edit_id"], 1)
        self.assertEqual([identifier for identifier, _ in self.saved], [2])


if __name__ == "__main__":
    unittest.main()
