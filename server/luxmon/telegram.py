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
    # TELEGRAM_X509_STRICT=0 — для прокси, подменяющих сертификат: их корень бывает без Authority Key
    # Identifier, а Python 3.13 по умолчанию требует его. Цепочка и имя хоста проверяются всё равно.
    ctx = ssl.create_default_context()
    if tg_env().get("TELEGRAM_X509_STRICT") == "0":
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def tg_call(token, method, params, timeout=40):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data,
                                timeout=timeout, context=tg_ctx()) as r:
        return json.loads(r.read())
