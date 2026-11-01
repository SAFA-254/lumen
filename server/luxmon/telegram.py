"""Вызовы Telegram Bot API."""
import json
import ssl
import urllib.parse
import urllib.request

from .config import TG_ENV_PATH


def tg_env():
    env = {}
    try:
        with open(TG_ENV_PATH) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k] = v
    except OSError:
        pass
    return env


def tg_ctx():
    # корень USIAG установлен в системе, но в нём нет Authority Key Identifier (см. tg-notify.py)
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def tg_call(token, method, params, timeout=40):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data,
                                timeout=timeout, context=tg_ctx()) as r:
        return json.loads(r.read())
