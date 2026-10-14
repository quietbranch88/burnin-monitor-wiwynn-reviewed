"""Execute the rendered cookie script; API/DOM are controlled test boundaries."""

import base64
import json
import shutil
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from lib import session


class State(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__


def token(user, issued=1000):
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": str(user), "iat": issued, "exp": 4102444800,
    }).encode()).decode().rstrip("=")
    return f"test.{payload}.signature"


NODE_RUNNER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const cookies = new Map(Object.entries(input.cookies || {}));
const storage = new Map();
const document = {};
Object.defineProperty(document, 'cookie', {
  get: () => [...cookies].map(([k, v]) => `${k}=${v}`).join('; '),
  set: value => {
    const [pair, ...attrs] = value.split('; ');
    const index = pair.indexOf('=');
    const key = pair.slice(0, index), content = pair.slice(index + 1);
    if (attrs.includes('Max-Age=0')) cookies.delete(key);
    else cookies.set(key, content);
  },
});
const localStorage = {
  getItem: key => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: key => storage.delete(key),
};
for (const script of input.scripts) {
  vm.runInNewContext(script, {document, location: {protocol: 'https:'}, atob, localStorage});
}
console.log(JSON.stringify(Object.fromEntries(cookies)));
"""


@unittest.skipUnless(shutil.which("node"), "Node is required to execute the rendered JavaScript")
class CookieIdentity(unittest.TestCase):
    def login(self, value):
        state = State()
        with patch.object(session, "st", MagicMock(session_state=state)), patch.object(
            session.api, "post", return_value={"access_token": value, "user": {"id": 1}, "expires_in": 3600},
        ):
            session.login("fixture", "fixture")
        return state

    def render(self, state):
        st = MagicMock(session_state=state)
        with patch.object(session, "st", st):
            session.sync_cookie()
        return st.html.call_args.args[0].removeprefix("<script>").removesuffix("</script>")

    def execute(self, scripts, current=None):
        result = subprocess.run(
            ["node", "-e", NODE_RUNNER],
            input=json.dumps({"scripts": scripts, "cookies": {session.COOKIE: current} if current else {}}),
            text=True, capture_output=True, check=True,
        )
        return json.loads(result.stdout).get(session.COOKIE)

    def test_same_second_new_login_wins_and_stale_periodic_sync_cannot_undo_it(self):
        old, new = token(1, 1000.1), token(2, 1000.2)
        old_tab, new_tab = self.login(old), self.login(new)
        self.assertEqual(self.execute([self.render(new_tab)], current=old), new)
        self.assertEqual(self.execute([
            self.render(old_tab), self.render(new_tab), self.render(old_tab),
        ]), new)

    def test_delayed_initial_script_cannot_undo_a_newer_explicit_login(self):
        old_tab, new_tab = self.login(token(1, 1000.1)), self.login(token(2, 1000.2))
        self.assertEqual(self.execute([self.render(new_tab), self.render(old_tab)]), token(2, 1000.2))

    def test_lost_first_script_is_retried_on_the_next_run(self):
        state = self.login(token(2, 1000.2))
        self.render(state)  # Streamlit replaced this element before it executed.
        self.assertEqual(self.execute([self.render(state)], current=token(1)), token(2, 1000.2))

    @unittest.expectedFailure
    def test_exact_clock_tie_new_explicit_login_remains_unresolved(self):
        # Intentionally retain this counterexample: fractional timestamps reduce
        # ties, but neither uniqueness nor a persistent global order is promised.
        new_tab = self.login(token(2))
        self.assertEqual(self.execute([self.render(new_tab)], current=token(1)), token(2))

    def test_old_tab_logout_does_not_clear_the_new_login(self):
        old_tab, new_tab = self.login(token(1)), self.login(token(2))
        with patch.object(session, "st", MagicMock(session_state=old_tab)):
            session.logout()
        self.assertEqual(self.execute([self.render(new_tab), self.render(old_tab)]), token(2))

    def test_restored_legacy_session_cannot_replace_a_different_cookie(self):
        state = State(token=token(1), expires_at=4102444740)
        self.assertEqual(self.execute([self.render(state)], current=token(2)), token(2))

    def test_identical_legacy_cookie_is_refreshed(self):
        state = State(token=token(1), expires_at=4102444740)
        self.assertEqual(self.execute([self.render(state)], current=token(1)), token(1))
