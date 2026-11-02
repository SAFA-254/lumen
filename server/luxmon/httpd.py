"""HTTP API и выдача страниц."""
import json
import os
import secrets
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import CFG, LOGIN_LIMIT, SILENCE_SCOPES
from .store import S, event
from .analytics import uptime
from .settings import save_settings, settings_payload
from .alerts import ALERTS
from .ingest import ingest
from .tokens import has_own_token, push_identity
from .queries import alert_history, annotations, overview, series, ssh_fails, ssh_sessions
from .auth import DUMMY_HASH, SESSIONS, check_password, users


def parse_cookie(header):
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "luxmon"
    timeout = 30  # медленный или зависший клиент не держит поток вечно

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", headers=(), csp="default-src 'none'; frame-ancestors 'none'"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if CFG["tls_cert"]:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def client_ip(self):
        return self.client_address[0]

    def session_token(self):
        return parse_cookie(self.headers.get("Cookie")).get("luxmon_session")

    def current_user(self):
        if not users():
            return "(без пароля)"
        return SESSIONS.user(self.session_token())

    def require_user(self, api=True):
        u = self.current_user()
        if u:
            return u
        if api:
            self.send(401, {"error": "login required"})
        else:
            self.send(302, b"", "text/plain", [("Location", "/login")])
        return None

    def body_len(self):
        n = int(self.headers.get("Content-Length", 0))
        if not 0 <= n <= 4 * 1024 * 1024:
            raise ValueError("bad content length")
        return n

    def read_json(self):
        return json.loads(self.rfile.read(self.body_len()) or b"{}")

    def static(self, name):
        # скрипты страницы запускаются только с одноразовым номером: внедрённый в данные <script> и
        # обработчики onclick="…" не выполнятся, даже если где-то пропущено экранирование
        nonce = secrets.token_urlsafe(16)
        with open(os.path.join(CFG["web"], name), "rb") as f:
            page = f.read().replace(b"<script>", f'<script nonce="{nonce}">'.encode())
        csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
               "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        return self.send(200, page, "text/html; charset=utf-8", csp=csp)

    # ---- POST
    def do_POST(self):
        if self.path == "/api/push":
            return self.push()
        # свой заголовок не даёт отправить запрос обычной формой с чужой страницы
        if self.headers.get("X-Luxmon") != "1":
            return self.send(403, {"error": "missing X-Luxmon header"})
        try:
            body = self.read_json()
            if self.path == "/api/login":
                return self.login(body)
            if self.path == "/api/logout":
                tok = self.session_token()
                if tok:
                    SESSIONS.drop(tok)
                return self.send(200, {"ok": True}, headers=[("Set-Cookie", self.cookie("", 0))])
            user = self.require_user()
            if not user:
                return
            if self.path == "/api/silence":
                scope = str(body.get("scope", ""))
                hosts = {r[0] for r in S.q("SELECT name FROM hosts")}
                if scope not in SILENCE_SCOPES and not (scope.startswith("host:") and scope[5:] in hosts):
                    return self.send(400, {"error": "bad scope"})
                minutes = int(body.get("minutes", 60))
                if not 1 <= minutes <= 7 * 1440:
                    return self.send(400, {"error": "bad minutes"})
                return self.send(200, ALERTS.add_silence(scope, minutes, str(body.get("reason", "")),
                                                         f"{user}@{self.client_ip()}"))
            if self.path == "/api/silence/delete":
                return self.send(200, {"ok": ALERTS.remove_silence(str(body.get("id")))})
            if self.path == "/api/settings":
                save_settings(body)
                return self.send(200, settings_payload())
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        except Exception as e:
            return self.send(400, {"error": repr(e)})
        self.send(404, {"error": "not found"})

    def cookie(self, value, max_age):
        secure = "; Secure" if CFG["tls_cert"] else ""
        return f"luxmon_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def login(self, body):
        ip = self.client_ip()
        if SESSIONS.blocked(ip):
            return self.send(429, {"error": "слишком много попыток, подождите 10 минут"})
        user, pw = str(body.get("user", "")), str(body.get("password", ""))
        stored = users().get(user)
        if not check_password(pw, stored or DUMMY_HASH) or not stored:
            SESSIONS.failed(ip)
            time.sleep(1)
            if SESSIONS.blocked(ip):
                event("warn", f"Вход в Luxmon: много неудачных попыток с {ip}")
                ALERTS.notify(f"🔐 Luxmon: {LOGIN_LIMIT[0]} неудачных попыток входа с {ip}, адрес заблокирован на 10 минут")
            return self.send(403, {"error": "неверный логин или пароль"})
        tok = SESSIONS.create(user, ip)
        event("info", f"Вход в Luxmon: {user} с {ip}")
        return self.send(200, {"ok": True}, headers=[("Set-Cookie", self.cookie(tok, CFG["session_days"] * 86400))])

    def push(self):
        ident = push_identity(self.headers.get("Authorization", ""))
        if not ident:
            return self.send(403, {"error": "bad token"})
        try:
            p = json.loads(self.rfile.read(self.body_len()))
            host = str(p["host"])[:64]
        except Exception as e:
            return self.send(400, {"error": repr(e)})
        # агент с чужим или общим токеном не может выдать себя за хост со своим токеном
        kind, owner = ident
        if kind == "host" and host != owner:
            return self.send(403, {"error": f"токен выдан другому хосту ({owner})"})
        if kind == "shared" and has_own_token(host):
            return self.send(403, {"error": f"у {host} свой токен, общий для него не действует"})
        try:
            return self.send(200, ingest(p, auth=kind))
        except Exception as e:
            return self.send(400, {"error": repr(e)})

    # ---- GET
    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/login":
                return self.static("login.html")
            if u.path in ("/", "/index.html"):
                if self.require_user(api=False):
                    return self.static("index.html")
                return
            if u.path == "/api/health":
                # без входа: для сторожевой проверки с другого узла, только возраст последних данных
                last = S.q("SELECT MAX(last_ts) FROM hosts")[0][0] or 0
                return self.send(200, {"ok": True, "last_push_age": round(time.time() - last, 1)})
            if not u.path.startswith("/api/"):
                return self.send(404, {"error": "not found"})
            user = self.require_user()
            if not user:
                return
            now = int(time.time())
            if u.path == "/api/overview":
                return self.send(200, dict(overview(), user=user, auth=bool(users())))
            if u.path == "/api/history":
                end = int(qs.get("end", now))
                start = int(qs.get("start", end - min(int(qs.get("range", 3600)), CFG["rollup_keep_days"] * 86400)))
                metrics = qs.get("metrics", "cpu").split(",")[:30]
                out = {m: series(qs["host"], m, start, end) for m in metrics}
                if qs.get("scope"):
                    # без явного конца берём с запасом, чтобы не потерять только что начавшееся
                    out["_annotations"] = annotations(qs["scope"], start, end if "end" in qs else end + 60)
                return self.send(200, out)
            if u.path == "/api/metrics":
                rows = S.q("SELECT DISTINCT metric FROM raw WHERE host=? AND ts>?", (qs["host"], now - 3600))
                return self.send(200, sorted(r[0] for r in rows))
            if u.path == "/api/ssh":
                since = now - min(int(qs.get("days", 7)), CFG["rollup_keep_days"]) * 86400
                return self.send(200, {"sessions": ssh_sessions(500, since, fails=True), "fails": ssh_fails(since)})
            if u.path == "/api/uptime":
                return self.send(200, uptime(min(int(qs.get("days", 30)), CFG["rollup_keep_days"])))
            if u.path == "/api/alerts/history":
                return self.send(200, alert_history(min(int(qs.get("days", 7)), CFG["rollup_keep_days"])))
            if u.path == "/api/settings":
                return self.send(200, settings_payload())
        except Exception as e:
            return self.send(500, {"error": repr(e)})
        self.send(404, {"error": "not found"})


class TLSServer(ThreadingHTTPServer):
    """TLS-рукопожатие выполняется в потоке запроса, а не в accept(): зависший клиент
    не блокирует приём остальных соединений."""

    def __init__(self, addr, handler, ctx):
        super().__init__(addr, handler)
        self.ctx = ctx

    def finish_request(self, request, client_address):
        request.settimeout(15)
        try:
            request = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return  # сканеры портов и клиенты без TLS
        super().finish_request(request, client_address)

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


class Redirect(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        base = CFG["public_url"] or f"https://{self.headers.get('Host', '').split(':')[0]}:{CFG['port']}"
        self.send_response(301)
        self.send_header("Location", base.rstrip("/") + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_GET
