"""Токены агентов: у каждого хоста свой, сервер хранит только sha256 от него.
Общий токен из server.json ("token") пока тоже принимается, но не для хостов, у которых есть свой."""
import hashlib
import hmac
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
    if len(args) != 2 or args[0] not in ("add", "del") or not re.fullmatch(r"[\w.\-]{1,64}", args[1]):
        sys.exit("использование: token add ХОСТ | token del ХОСТ | token list")
    action, host = args
    if action == "del":
        S.q("DELETE FROM agent_tokens WHERE host=?", (host,))
        event("info", f"Отозван токен агента {host}")
        print(f"Токен {host} отозван." + (" Хост сможет присылать данные только с общим токеном."
                                          if CFG["token"] else ""))
        return
    tok = "lxa_" + secrets.token_urlsafe(32)
    S.q("INSERT OR REPLACE INTO agent_tokens VALUES(?,?,?)", (host, digest(tok), time.time()))
    event("info", f"Выдан токен агента {host}")
    print(tok)
    print(f"Впишите его в \"token\" в /etc/luxmon/agent.json на {host} и перезапустите агента. "
          f"Общий токен для {host} больше не действует.", file=sys.stderr)
