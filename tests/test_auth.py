import unittest

from common import reset
from luxmon.auth import SESSIONS, check_password, hash_password, pw_fp
from luxmon.httpd import parse_cookie


class Passwords(unittest.TestCase):
    def test_roundtrip(self):
        h = hash_password("пароль", iters=1000)
        self.assertTrue(check_password("пароль", h))
        self.assertFalse(check_password("другой", h))

    def test_salt_is_random(self):
        self.assertNotEqual(hash_password("a", iters=1000), hash_password("a", iters=1000))

    def test_garbage_hash_is_not_an_error(self):
        self.assertFalse(check_password("a", "мусор"))

    def test_fingerprint(self):
        self.assertIsNone(pw_fp(None))
        self.assertNotEqual(pw_fp("a"), pw_fp("b"))


class Sessions(unittest.TestCase):
    def setUp(self):
        reset()
        SESSIONS.data, SESSIONS.fails = {}, {}

    def test_token_not_stored_in_clear(self):
        tok = SESSIONS.create("admin", "1.1.1.1")
        self.assertNotIn(tok, SESSIONS.data)
        self.assertEqual(len(next(iter(SESSIONS.data))), 64)

    def test_unknown_user_session_invalid(self):
        tok = SESSIONS.create("nobody", "1.1.1.1")
        self.assertIsNone(SESSIONS.user(tok))
        self.assertIsNone(SESSIONS.user(None))

    def test_block_after_five_failures(self):
        for _ in range(4):
            SESSIONS.failed("1.1.1.1")
        self.assertFalse(SESSIONS.blocked("1.1.1.1"))
        SESSIONS.failed("1.1.1.1")
        self.assertTrue(SESSIONS.blocked("1.1.1.1"))
        self.assertFalse(SESSIONS.blocked("2.2.2.2"))


class Cookies(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_cookie("a=1; luxmon_session=x=y"), {"a": "1", "luxmon_session": "x=y"})
        self.assertEqual(parse_cookie(None), {})


if __name__ == "__main__":
    unittest.main()
