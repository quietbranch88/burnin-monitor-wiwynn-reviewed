"""Token issuance contract without a database or HTTP substitute."""

import asyncio
import threading
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from app.core import security


class TokenIssueOrdering(unittest.TestCase):
    def test_actual_signed_tokens_preserve_same_second_issuance_precision(self):
        settings = SimpleNamespace(
            jwt_secret="test-secret-that-is-at-least-thirty-two-characters",
            jwt_algorithm="HS256", jwt_expire_minutes=480,
        )
        second = (datetime.now(UTC) - timedelta(seconds=10)).replace(microsecond=0)
        moments = [second.replace(microsecond=100000), second.replace(microsecond=900000)]
        with patch.object(security, "get_settings", return_value=settings), patch.object(security, "datetime") as clock:
            clock.now.side_effect = moments
            old, old_lifetime = security.create_access_token(1, 0)
            new, new_lifetime = security.create_access_token(2, 0)
            # Actual PyJWT signature/time validation; no database or credentials.
            self.assertEqual(security.decode_access_token(old), (1, 0))
            self.assertEqual(security.decode_access_token(new), (2, 0))
        old_claims = jwt.decode(old, settings.jwt_secret, algorithms=["HS256"])
        new_claims = jwt.decode(new, settings.jwt_secret, algorithms=["HS256"])
        self.assertEqual(old_claims["iat"], moments[0].timestamp())
        self.assertEqual(new_claims["iat"], moments[1].timestamp())
        self.assertEqual(int(old_claims["iat"]), int(new_claims["iat"]))
        self.assertLess(old_claims["iat"], new_claims["iat"])
        self.assertEqual((old_lifetime, new_lifetime), (28800, 28800))

    def test_the_first_unknown_user_check_hashes_off_the_event_loop(self):
        loop_thread = []

        def hash_password(_password):
            loop_thread.append(threading.current_thread() is threading.main_thread())
            return security.bcrypt.hashpw(b"x", security.bcrypt.gensalt(4)).decode()

        security._dummy_hash.cache_clear()
        try:
            with patch.object(security, "hash_password", side_effect=hash_password):
                self.assertFalse(asyncio.run(security.verify_password_async("guess", None)))
        finally:
            security._dummy_hash.cache_clear()
        self.assertEqual(loop_thread, [False])

    def test_existing_integer_iat_token_still_verifies(self):
        settings = SimpleNamespace(
            jwt_secret="test-secret-that-is-at-least-thirty-two-characters", jwt_algorithm="HS256",
        )
        now = datetime.now(UTC)
        legacy = jwt.encode({"sub": "1", "ver": 0, "iat": now, "exp": now + timedelta(hours=1)},
                            settings.jwt_secret, algorithm="HS256")
        with patch.object(security, "get_settings", return_value=settings):
            self.assertEqual(security.decode_access_token(legacy), (1, 0))
