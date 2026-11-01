"""Настоящий сервер в отдельном процессе: проверяем API снаружи, как его видят агент и браузер."""
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

from common import ROOT, SERVER
from luxmon.auth import hash_password


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    extra = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="luxmon-http-")
        cls.port = free_port()
        cls.config = os.path.join(cls.tmp, "server.json")
        cfg = {"listen": "127.0.0.1", "port": cls.port, "token": "secret", "db": os.path.join(cls.tmp, "d", "l.db")}
        cfg.update(cls.prepare())
        with open(cls.config, "w") as f:
            json.dump(cfg, f)
        env = dict(os.environ, LUXMON_CONFIG=cls.config, LUXMON_TG_ENV=os.path.join(cls.tmp, "tg"))
        cls.proc = subprocess.Popen([sys.executable, os.path.join(SERVER, "luxmon-server.py")], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                cls.req("GET", "/api/health")
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("сервер не запустился")

    @classmethod
    def prepare(cls):
        return {}

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def conn(cls):
        return http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)

    @classmethod
    def req(cls, method, path, body=None, headers=None):
        c = cls.conn()
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        c.request(method, path, data, headers or {})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, dict(r.getheaders()), raw

    def api(self, method, path, body=None, cookie=None):
        h = {"X-Luxmon": "1"}
        if cookie:
            h["Cookie"] = cookie
        return self.req(method, path, body, h)

    def push(self, body, token="secret"):
        return self.req("POST", "/api/push", body, {"Authorization": f"Bearer {token}"})


class Open(Base):
    """Без пользователей панель открыта: так выглядит первый запуск."""

    def test_health_needs_no_login(self):
        code, _, raw = self.req("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(raw)["ok"])

    def test_push_requires_token(self):
        self.assertEqual(self.push({"host": "x"}, token="wrong")[0], 403)
        self.assertEqual(self.req("POST", "/api/push", {"host": "x"})[0], 403)

    def test_push_with_non_ascii_token_is_rejected_cleanly(self):
        c = self.conn()
        c.putrequest("POST", "/api/push")
        c.putheader("Authorization", "Bearer токен".encode("utf-8"))
        c.putheader("Content-Length", "2")
        c.endheaders(b"{}")
        self.assertEqual(c.getresponse().status, 403)
        c.close()

    def test_push_then_overview(self):
        code, _, raw = self.push({"host": "web1", "metrics": {"cpu": 12.5}})
        self.assertEqual((code, json.loads(raw)["ok"]), (200, True))
        code, _, raw = self.api("GET", "/api/overview")
        self.assertEqual(code, 200)
        d = json.loads(raw)
        self.assertEqual([h["name"] for h in d["hosts"]], ["web1"])
        self.assertFalse(d["auth"])

    def test_malformed_push(self):
        self.assertEqual(self.push(b"not json")[0], 400)
        self.assertEqual(self.push({"nohost": 1})[0], 400)

    def test_negative_content_length_rejected(self):
        c = self.conn()
        c.putrequest("POST", "/api/login")
        c.putheader("X-Luxmon", "1")
        c.putheader("Content-Length", "-1")
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_csrf_header_required(self):
        self.assertEqual(self.req("POST", "/api/settings", {})[0], 403)

    def test_page_has_nonce_csp_and_scripts_use_it(self):
        code, h, raw = self.req("GET", "/")
        self.assertEqual(code, 200)
        csp = h["Content-Security-Policy"]
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertNotIn(b"<script>", raw)
        self.assertIn(f'<script nonce="{nonce}">'.encode(), raw)
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertNotEqual(nonce, self.req("GET", "/")[1]["Content-Security-Policy"].split("'nonce-")[1].split("'")[0])

    def test_login_page_served(self):
        self.assertEqual(self.req("GET", "/login")[0], 200)

    def test_settings_validation(self):
        self.assertEqual(self.api("POST", "/api/settings", {"rules": {"cpu_pct": 1}})[0], 400)
        code, _, raw = self.api("POST", "/api/settings", {"rules": {"cpu_pct": 70}})
        self.assertEqual(code, 200)
        value = next(x["value"] for x in json.loads(raw)["rules"] if x["key"] == "cpu_pct")
        self.assertEqual(value, 70)

    def test_silence_validation(self):
        self.assertEqual(self.api("POST", "/api/silence", {"scope": "nope", "minutes": 5})[0], 400)
        self.assertEqual(self.api("POST", "/api/silence", {"scope": "all", "minutes": 0})[0], 400)
        code, _, raw = self.api("POST", "/api/silence", {"scope": "ceph", "minutes": 5})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(self.api("POST", "/api/silence/delete", {"id": json.loads(raw)["id"]})[2])["ok"])

    def test_unknown_paths(self):
        self.assertEqual(self.req("GET", "/etc/passwd")[0], 404)
        self.assertEqual(self.api("GET", "/api/nope")[0], 404)


class Login(Base):
    @classmethod
    def prepare(cls):
        return {"users": {"admin": hash_password("correct-horse", iters=1000)}}

    def login(self, user, pw):
        return self.api("POST", "/api/login", {"user": user, "password": pw})

    def test_a_requires_login(self):
        self.assertEqual(self.api("GET", "/api/overview")[0], 401)
        code, h, _ = self.req("GET", "/")
        self.assertEqual((code, h["Location"]), (302, "/login"))

    def test_b_session_flow_and_revocation(self):
        code, h, _ = self.login("admin", "correct-horse")
        self.assertEqual(code, 200)
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Strict", h["Set-Cookie"])
        cookie = h["Set-Cookie"].split(";")[0]
        self.assertEqual(self.api("GET", "/api/overview", cookie=cookie)[0], 200)
        # смена пароля отзывает выданные сессии без перезапуска сервера
        with open(self.config) as f:
            cfg = json.load(f)
        cfg["users"]["admin"] = hash_password("another-password", iters=1000)
        with open(self.config, "w") as f:
            json.dump(cfg, f)
        self.assertEqual(self.api("GET", "/api/overview", cookie=cookie)[0], 401)
        cfg["users"]["admin"] = hash_password("correct-horse", iters=1000)
        with open(self.config, "w") as f:
            json.dump(cfg, f)

    def test_c_logout(self):
        cookie = self.login("admin", "correct-horse")[1]["Set-Cookie"].split(";")[0]
        self.assertEqual(self.api("POST", "/api/logout", {}, cookie=cookie)[0], 200)
        self.assertEqual(self.api("GET", "/api/overview", cookie=cookie)[0], 401)

    def test_d_rate_limit(self):
        # последний: блокирует адрес на 10 минут
        codes = [self.login("admin", "wrong")[0] for _ in range(5)]
        self.assertEqual(codes, [403] * 5)
        self.assertEqual(self.login("nobody", "wrong")[0], 429)
        self.assertEqual(self.login("admin", "correct-horse")[0], 429)


@unittest.skipUnless(shutil.which("openssl"), "нужен openssl")
class TLS(Base):
    @classmethod
    def prepare(cls):
        cls.crt, cls.key = os.path.join(cls.tmp, "c.crt"), os.path.join(cls.tmp, "c.key")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", cls.key, "-out", cls.crt],
                       check=True, capture_output=True)
        return {"tls_cert": cls.crt, "tls_key": cls.key}

    @classmethod
    def conn(cls):
        return http.client.HTTPSConnection("127.0.0.1", cls.port, timeout=10,
                                           context=ssl.create_default_context(cafile=cls.crt))

    def test_pinned_certificate_and_hsts(self):
        code, h, _ = self.req("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertIn("Strict-Transport-Security", h)

    def test_plain_client_does_not_block_server(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(b"GET / HTTP/1.0\r\n\r\n")
            self.assertEqual(self.req("GET", "/api/health")[0], 200)


if __name__ == "__main__":
    unittest.main()
