"""Действующие пороги, настройки и список проверок сервисов."""
import ipaddress
import re

from .config import CFG, DEFAULT_SETTINGS, RULES_META
from .store import S, event
from .telegram import tg_env
from .analytics import FORECAST


def rules():
    """Действующие пороги: значения по умолчанию, затем server.json, затем правки из интерфейса."""
    r = {k: v[0] for k, v in RULES_META.items()}
    r.update(CFG.get("rules") or {})
    r.update(S.meta_get("rules_override", {}))
    return r


def settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(S.meta_get("settings", {}))
    return s


def checks_list():
    return S.meta_get("checks")  # None, пока список не перенесён с агента


def validate_check(c):
    t = c.get("type")
    name = str(c.get("name", "")).strip()[:60]
    if not name or t not in ("http", "tcp", "ping"):
        raise ValueError(f"проверка «{name}»: нужно имя и тип http/tcp/ping")
    out = {"name": name, "type": t}
    if t == "http":
        url = str(c.get("url", "")).strip()
        if not re.match(r"^https?://[^\s]+$", url):
            raise ValueError(f"проверка «{name}»: неверный URL")
        out["url"] = url
        if c.get("insecure"):
            out["insecure"] = True
        if c.get("expect_text"):
            out["expect_text"] = str(c["expect_text"])[:200]
    else:
        host = str(c.get("host", "")).strip()
        if not re.match(r"^[\w.:][\w.\-:]*$", host):
            raise ValueError(f"проверка «{name}»: неверный адрес")
        out["host"] = host
        if t == "tcp":
            port = int(c.get("port", 0))
            if not 1 <= port <= 65535:
                raise ValueError(f"проверка «{name}»: неверный порт")
            out["port"] = port
    return out


def settings_payload():
    r = rules()
    return {"rules": [{"key": k, "value": r[k], "default": v[0], "label": v[1], "unit": v[2], "min": v[3], "max": v[4]}
                      for k, v in RULES_META.items()],
            "settings": settings(), "checks": checks_list() or [],
            "hosts": [x[0] for x in S.q("SELECT name FROM hosts ORDER BY name")],
            "bot": bool(tg_env().get("LUXMON_BOT_TOKEN"))}


def save_settings(body):
    if "rules" in body:
        over = {}
        for k, v in body["rules"].items():
            if k not in RULES_META:
                raise ValueError(f"неизвестное правило {k}")
            v = float(v)
            lo, hi = RULES_META[k][3], RULES_META[k][4]
            if not lo <= v <= hi:
                raise ValueError(f"{RULES_META[k][1]}: допустимо от {lo} до {hi}")
            if v != RULES_META[k][0]:
                over[k] = int(v) if v.is_integer() else v
        S.meta_set("rules_override", over)
    st = settings()
    if "summary_time" in body:
        t = str(body["summary_time"]).strip()
        if t and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise ValueError("время сводки в формате ЧЧ:ММ или пусто, чтобы выключить")
        st["summary_time"] = t
    if "weekly_report" in body:
        st["weekly_report"] = bool(body["weekly_report"])
    if "checks_runner" in body:
        st["checks_runner"] = str(body["checks_runner"])[:64]
    if "ssh_known" in body:
        known = []
        for x in body["ssh_known"]:
            x = str(x).strip()
            if x:
                try:
                    ipaddress.ip_network(x.split()[0], strict=False)  # после адреса можно подписать имя
                except ValueError:
                    raise ValueError(f"неверный адрес или подсеть: {x}")
                known.append(x)
        st["ssh_known"] = known
    S.meta_set("settings", st)
    if "checks" in body:
        S.meta_set("checks", [validate_check(c) for c in body["checks"]])
    FORECAST["ts"] = 0
    event("info", "Настройки изменены")
