"""Токены агентов: у каждого хоста свой, сервер хранит только sha256 от него.
Общий токен из server.json ("token") пока тоже принимается, но не для хостов, у которых есть свой."""
import hashlib
import hmac
import json
import re
import secrets
import sys
import time

from .config import CFG
from .store import S, event


def digest(tok):
    return hashlib.sha256(tok.encode("utf-8", "replace")).hexdigest()


def push_identity(header):
    """По заголовку Authorization: ("host", имя) — свой токен хоста, ("shared", None) — общий токен,
    None — токен неверный."""
    if not header.startswith("Bearer "):
        return None
    tok = header[7:]
    row = S.q("SELECT host FROM agent_tokens WHERE hash=?", (digest(tok),))
    if row:
        return "host", row[0][0]
    # сравнение байтов: строка с не-ASCII символами вызвала бы исключение
    if CFG["token"] and hmac.compare_digest(tok.encode("utf-8", "replace"), CFG["token"].encode()):
        return "shared", None
    return None


def has_own_token(host):
    return bool(S.q("SELECT 1 FROM agent_tokens WHERE host=?", (host,)))


def any_tokens():
    return bool(CFG["token"]) or bool(S.q("SELECT 1 FROM agent_tokens LIMIT 1"))


def issue_token(host, by="консоль"):
    """Новый токен хоста (прежний перестаёт действовать). Показывается один раз."""
    tok = "lxa_" + secrets.token_urlsafe(32)
    S.q("INSERT OR REPLACE INTO agent_tokens VALUES(?,?,?)", (host, digest(tok), time.time()))
    event("info", f"Выдан токен агента {host} ({by})")
    return tok


def revoke_token(host, by="консоль"):
    S.q("DELETE FROM agent_tokens WHERE host=?", (host,))
    event("info", f"Отозван токен агента {host} ({by})")


def forget_host(host, by):
    """Убрать хост из списка (выведен из работы). История графиков остаётся, токен отзывается."""
    S.tx(lambda db: (db.execute("DELETE FROM hosts WHERE name=?", (host,)),
                     db.execute("DELETE FROM agent_tokens WHERE host=?", (host,))))
    event("info", f"Хост {host} убран из списка ({by})")


def agents():
    """Хосты с агентами: когда прислали данные, каким токеном, версия агента и модули."""
    own = dict(S.q("SELECT host, created FROM agent_tokens"))
    out = []
    for name, last, latest in S.q("SELECT name, last_ts, latest FROM hosts ORDER BY name"):
        d = json.loads(latest)
        i = d.get("info") or {}
        out.append({"name": name, "last": last, "auth": d.get("auth"), "token_since": own.pop(name, None),
                    "version": i.get("agent"), "modules": i.get("modules"), "os": i.get("os"),
                    "ips": (i.get("ips") or [])[:3], "interval": d.get("interval")})
    # токен выдан, а данных с ним ещё не было
    out += [{"name": h, "last": None, "auth": None, "token_since": t} for h, t in sorted(own.items())]
    return out


HOST_RE = re.compile(r"[\w.\-]{1,64}")


def cmd_token(args):
    """token add ХОСТ — выдать (или заменить) токен, token del ХОСТ — отозвать, token list — кто с каким токеном."""
    if args[:1] == ["list"] and len(args) == 1:
        own = dict(S.q("SELECT host, created FROM agent_tokens"))
        seen = {h for (h,) in S.q("SELECT name FROM hosts")}
        for h in sorted(own.keys() | seen):
            kind = "свой токен с " + time.strftime("%Y-%m-%d %H:%M", time.localtime(own[h])) if h in own \
                else "общий токен"
            print(f"{h:24} {kind}")
        return
    if len(args) != 2 or args[0] not in ("add", "del") or not HOST_RE.fullmatch(args[1]):
        sys.exit("использование: token add ХОСТ | token del ХОСТ | token list")
    action, host = args
    if action == "del":
        revoke_token(host)
        print(f"Токен {host} отозван." + (" Хост сможет присылать данные только с общим токеном."
                                          if CFG["token"] else ""))
        return
    print(issue_token(host))
    print(f"Впишите его в \"token\" в /etc/luxmon/agent.json на {host} и перезапустите агента. "
          f"Общий токен для {host} больше не действует.", file=sys.stderr)
