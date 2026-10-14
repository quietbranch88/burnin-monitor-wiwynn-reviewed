"""User-facing errors follow the API's field/message and import-row contract."""

import unittest
from unittest.mock import MagicMock, patch

from lib import api, i18n


class ErrorFeedback(unittest.TestCase):
    def setUp(self):
        self.st = MagicMock()
        self.enterContext(patch("lib.api.st", self.st))
        self.enterContext(patch("lib.i18n.lang", return_value="en"))

    def test_password_error_names_field_and_constraint_without_diagnostic_panels(self):
        api.show_error(api.ApiError(422, "Request validation failed", [
            {"field": "password", "message": "String should have at least 8 characters", "input": "secret-fixture"},
        ]))
        self.st.error.assert_called_once_with("**Password:** Use at least 8 characters.")
        self.st.caption.assert_not_called()
        self.st.dataframe.assert_not_called()
        self.assertNotIn("secret-fixture", str(self.st.mock_calls))

    def test_multiple_fields_keep_each_actionable_error_with_translated_labels(self):
        with patch("lib.i18n.lang", return_value="zh"):
            api.show_error(api.ApiError(422, "Request validation failed", [
                {"field": "username", "message": "Field required"},
                {"field": "email", "message": "value is not a valid email address"},
            ]))
            expected = ("輸入的資料有誤：- **帳號:** Field required\n"
                        "- **Email:** value is not a valid email address")
            self.st.error.assert_called_once_with(expected)
        self.st.caption.assert_not_called()
        self.st.dataframe.assert_not_called()

    def test_bulk_import_preserves_every_row_and_numbering_guidance(self):
        errors = [{"row": row, "field": "value", "message": "value must be a finite number"}
                  for row in range(2, 7)]
        api.show_error(api.ApiError(422, "5 validation error(s); nothing was imported", errors))
        rows = self.st.dataframe.call_args.args[0]
        self.assertEqual([row["Row"] for row in rows], [2, 3, 4, 5, 6])
        self.assertEqual([row["Field"] for row in rows], ["Value"] * 5)
        self.st.caption.assert_called_once_with(
            "In a CSV, row 1 is the header and row 2 the first record; in JSON, row 1 is the first record.")
        self.st.error.assert_called_once_with("5 validation error(s); nothing was imported")

    def test_server_error_does_not_expose_internal_exception_detail(self):
        api.show_error(api.ApiError(500, "Traceback: database password=secret-fixture"))
        self.st.error.assert_called_once_with("The service could not complete your request. Please try again.")
        self.st.caption.assert_not_called()
        self.st.dataframe.assert_not_called()
        self.assertNotIn("secret-fixture", str(self.st.mock_calls))

    def test_database_unavailability_keeps_existing_retry_guidance(self):
        api.show_error(api.ApiError(503, "Database unavailable, please retry shortly"))
        self.st.error.assert_called_once_with("Database unavailable, please retry shortly")
        self.st.caption.assert_not_called()

    def test_network_failure_has_actionable_message_without_exception_class(self):
        api.show_error(api.ApiError(0, "API unreachable: ConnectionError"))
        self.st.error.assert_called_once_with("Cannot connect to the service. Check your connection and try again.")
        self.st.caption.assert_not_called()

    def test_nested_field_and_body_problems_remain_visible_without_raw_paths(self):
        api.show_error(api.ApiError(422, "Request validation failed", [
            {"field": "thresholds.cpu_temp", "message": "Input should be greater than 0"},
            {"field": "body", "message": "Invalid JSON"},
        ]))
        self.st.error.assert_called_once_with("- **Threshold / CPU temp:** Enter a number greater than 0.\n- Invalid JSON")
        self.st.dataframe.assert_not_called()
        self.st.caption.assert_not_called()

    def test_timeout_keeps_retry_guidance_and_localized_connection_error_is_safe(self):
        message = "The API did not answer within 60 s; it may be busy. Try again."
        api.show_error(api.ApiError(0, message))
        self.st.error.assert_called_once_with(message)
        self.st.reset_mock()
        with patch("lib.i18n.lang", return_value="zh"):
            detail = i18n.t("API unreachable: {error}", error="ConnectionError")
            api.show_error(api.ApiError(0, detail))
            self.st.error.assert_called_once_with(i18n.t("Cannot connect to the service. Check your connection and try again."))
        self.assertNotIn("ConnectionError", str(self.st.mock_calls))

    def test_common_form_constraints_are_actionable_and_unknown_policy_is_preserved(self):
        cases = [
            ("Field required", "Enter a value."),
            ("String should have at most 64 characters", "Use no more than 64 characters."),
            ("Input should be greater than or equal to 50", "Enter a number of at least 50."),
            ("Input should be less than 100", "Enter a number less than 100."),
            ("Input should be less than or equal to 99", "Enter a number no greater than 99."),
            ("Input should be a valid number, unable to parse string as a number", "Enter a valid number."),
            ("Input should be a valid integer, got a number with a fractional part", "Enter a whole number."),
            ("value must be a finite number", "value must be a finite number"),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.st.reset_mock()
                api.show_error(api.ApiError(422, "Request validation failed", [{"field": "value", "message": message}]))
                self.st.error.assert_called_once_with(f"**Value:** {expected}")

    def test_bulk_row_constraints_keep_original_api_message(self):
        api.show_error(api.ApiError(422, "1 validation error(s); nothing was imported", [
            {"row": 2, "field": "title", "message": "String should have at most 200 characters"},
        ]))
        self.assertEqual(self.st.dataframe.call_args.args[0][0]["Problem"], "String should have at most 200 characters")


if __name__ == "__main__":
    unittest.main()
