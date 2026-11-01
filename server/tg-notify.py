#!/usr/bin/env python3
"""Отправка алерта в Telegram. Использование: tg-notify.py "текст".
Токен и чат берутся из /etc/luxmon/telegram.env (TELEGRAM_TOKEN=..., TELEGRAM_CHAT=...).
С 23:00 до 08:00 сообщения приходят без звука; TG_SILENT=1/0 переопределяет."""
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

env = {}
with open(os.environ.get("LUXMON_TG_ENV", "/etc/luxmon/telegram.env")) as f:
    for line in f:
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            env[k] = v

hour = time.localtime().tm_hour
silent = os.environ.get("TG_SILENT")
silent = silent == "1" if silent is not None else (hour >= 23 or hour < 8)

# Шлюз USIAG подменяет сертификат; его корень установлен в системе, но в нём нет
# Authority Key Identifier, а Python 3.13 по умолчанию требует его (VERIFY_X509_STRICT).
# Цепочка и имя хоста по-прежнему проверяются.
ctx = ssl.create_default_context()
ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT

data = urllib.parse.urlencode({"chat_id": env["TELEGRAM_CHAT"], "text": sys.argv[1],
                               "disable_notification": "true" if silent else "false"}).encode()
try:
    with urllib.request.urlopen(f"https://api.telegram.org/bot{env['TELEGRAM_TOKEN']}/sendMessage",
                                data=data, timeout=20, context=ctx) as r:
        print(r.status)
except Exception as e:
    print(f"telegram send failed: {e!r}", file=sys.stderr)
    sys.exit(1)
