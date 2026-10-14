"""Users action contracts; password examples follow backend PasswordRules."""
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.api import ApiError
from streamlit.testing.v1 import AppTest

PAGE = Path(__file__).resolve().parents[1] / "views/admin_users.py"
USERS = [
    dict(id=1, username="admin", email="admin@example.test", role="admin", is_active=True,
         created_at="2026-10-14T00:00:00Z"),
    dict(id=2, username="engineer", email="engineer@example.test", role="user", is_active=True,
         created_at="2026-10-14T00:00:00Z"),
]


class AdminUserActions(unittest.TestCase):
    def setUp(self):
        def get(path, **_kwargs):
            return {"total": 0} if path == "/admin/logs" else {"items": USERS, "total": 2, "pages": 1}
        self.get = self.enterContext(patch("lib.api.get", side_effect=get))
        self.put = self.enterContext(patch("lib.api.put"))
        self.patch_user = self.enterContext(patch("lib.api.patch"))
        self.app = AppTest.from_file(str(PAGE))
        self.app.session_state.user = USERS[0]
        self.app.run()
        self.assertFalse(self.app.exception)

    def button(self, label):
        return next(button for button in self.app.button if button.label == label)

    def select(self, user_id=2):
        self.app.selectbox(key="admin-users-selected").select(user_id).run()
        self.assertFalse(self.app.exception)

    def open_password(self):
        self.select()
        self.button("Reset password").click().run()
        self.assertFalse(self.app.exception)

    def submit_password(self, password, confirmation):
        self.app.text_input(key="reset-password-2").set_value(password)
        self.app.text_input(key="reset-confirm-2").set_value(confirmation)
        self.button("Set new password").click().run()
        self.assertFalse(self.app.exception)

    def test_self_access_disabled_and_no_persistent_forms(self):
        self.select(1)
        self.assertTrue(self.button("Manage access").disabled)
        self.assertTrue(any("cannot change your own role" in item.value for item in self.app.caption))
        self.assertFalse(any(button.label == "Save access" for button in self.app.button))
        self.assertFalse(any(field.label == "New password" for field in self.app.text_input))
        self.patch_user.assert_not_called()

    def test_invalid_passwords_stay_local_and_preserve_inputs(self):
        self.open_password()
        for password, confirm, expected in [
            ("", "", "Enter a new password."),
            ("Short12", "Short12", "Use 8–64 characters."),
            ("A" * 64 + "1", "A" * 64 + "1", "Use 8–64 characters."),
            ("Onlyletters", "Onlyletters", "Include at least one letter and one digit."),
            ("123456789", "123456789", "Include at least one letter and one digit."),
            ("界" * 24 + "1", "界" * 24 + "1", "This password is too long. Try fewer characters."),
            ("FreshBlue42", "", "Confirm the new password."),
            ("FreshBlue42", "Different42", "Passwords do not match."),
        ]:
            with self.subTest(expected=expected, length=len(password)):
                self.submit_password(password, confirm)
                self.put.assert_not_called()
                self.assertIn(expected, [error.value for error in self.app.error])
                self.assertEqual(self.app.text_input(key="reset-password-2").value, password)
                self.assertEqual(self.app.text_input(key="reset-confirm-2").value, confirm)

    def test_api_refusal_keeps_password_fields_for_correction(self):
        self.open_password()
        self.put.side_effect = ApiError(422, "password is too easy to guess")
        self.submit_password("password123", "password123")
        self.assertEqual(self.app.text_input(key="reset-password-2").value, "password123")
        self.assertEqual(self.app.text_input(key="reset-confirm-2").value, "password123")
        self.assertTrue(self.app.error)

    def test_valid_password_payload_and_success_closes_dialog(self):
        self.open_password()
        self.submit_password("FreshBlue42", "FreshBlue42")
        self.put.assert_called_once_with("/admin/users/2/password", json={"password": "FreshBlue42"})
        self.assertFalse(any(field.label == "New password" for field in self.app.text_input))
        self.assertTrue(any("New password set" in success.value for success in self.app.success))

    def test_other_account_access_payload_and_success_closes_dialog(self):
        self.select()
        self.button("Manage access").click().run()
        self.assertFalse(self.app.exception)
        self.app.selectbox(key="access-role-2").select("viewer")
        self.app.checkbox(key="access-active-2").uncheck()
        self.button("Save access").click().run()
        self.assertFalse(self.app.exception)
        self.patch_user.assert_called_once_with("/admin/users/2", json={"role": "viewer", "is_active": False})
        self.assertFalse(any(button.label == "Save access" for button in self.app.button))
        self.assertTrue(any("Saved engineer" in success.value for success in self.app.success))


if __name__ == "__main__":
    unittest.main()
