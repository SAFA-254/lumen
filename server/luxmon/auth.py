"""Пароли, сессии и пользователи веб-интерфейса."""
import hashlib
import json
import secrets
import threading
import time

from .config import CFG, CONFIG_PATH, LOGIN_LIMIT
from .store import S
from .passwd import check_password, hash_password  # noqa: F401


def pw_fp(stored):
    """Отпечаток хеша пароля: сессия перестаёт действовать, когда пароль сменили или пользователя убрали."""
    return hashlib.sha256(stored.encode()).hexdigest()[:16] if stored else None


DUMMY_HASH = hash_password("luxmon-dummy")  # чтобы вход с неизвестным именем занимал столько же времени


class Sessions:
    """Сессии хранятся как sha256 от токена, чтобы утечка базы не давала готовых cookie."""

    def __init__(self):
        self.data = S.meta_get("sessions", {})
        self.fails = {}  # ip -> [время неудачных попыток]
        self.lock = threading.Lock()

    @staticmethod
    def _h(tok):
        return hashlib.sha256(tok.encode()).hexdigest()

    def create(self, user, ip):
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.data[self._h(tok)] = {"user": user, "ip": ip, "created": time.time(),
                                       "exp": time.time() + CFG["session_days"] * 86400, "pw": pw_fp(users().get(user))}
            self.data = {k: v for k, v in self.data.items() if v["exp"] > time.time()}
            S.meta_set("sessions", self.data)
        return tok

    def user(self, tok):
        s = self.data.get(self._h(tok)) if tok else None
        if not s or s["exp"] <= time.time():
            return None
        fp = pw_fp(users().get(s["user"]))
        if not fp:
            return None
        if s.get("pw") is None:
            s["pw"] = fp  # сессия создана до привязки к паролю
        return s["user"] if s["pw"] == fp else None

    def drop(self, tok):
        with self.lock:
            self.data.pop(self._h(tok), None)
            S.meta_set("sessions", self.data)

    def blocked(self, ip):
        now = time.time()
        self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < LOGIN_LIMIT[1]]
        if len(self.fails) > 1000:  # адреса, с которых давно не пробовали, не копим
            self.fails = {k: v for k, v in self.fails.items() if v}
        return len(self.fails[ip]) >= LOGIN_LIMIT[0]

    def failed(self, ip):
        self.fails.setdefault(ip, []).append(time.time())


SESSIONS = Sessions()


def users():
    # пароль меняется командой passwd без перезапуска сервера
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("users") or {}
    except (OSError, ValueError):
        return CFG.get("users") or {}
