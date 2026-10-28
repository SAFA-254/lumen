#!/usr/bin/env python3
"""Сторож Lumen: запускается таймером раз в минуту на узле, где Lumen НЕ живёт (pve1).
Если сервер не отвечает или перестал получать данные от агентов дольше DOWN_AFTER секунд,
пишет в Telegram; напоминает раз в REMIND секунд; сообщает, когда Lumen снова работает.

Lumen со своей стороны следит за pve1 (агент на pve1), так что они страхуют друг друга."""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request

URL = os.environ.get("LUMEN_HEALTH_URL", "https://192.168.3.7/api/health")
CA = os.environ.get("LUMEN_CA", "/etc/lumen/server.crt")
NOTIFY = os.environ.get("LUMEN_NOTIFY", "/root/tg-notify.sh")
STATE = "/var/lib/lumen-watchdog/state.json"
DOWN_AFTER = 180      # сколько терпеть, прежде чем писать
STALE_PUSH = 180      # сервер отвечает, но данные от агентов старше этого — тоже проблема
REMIND = 2 * 3600


def check():
    try:
        ctx = ssl.create_default_context(cafile=CA)
        with urllib.request.urlopen(URL, timeout=15, context=ctx) as r:
            d = json.loads(r.read())
    except Exception as e:
        return f"сервер не отвечает ({getattr(e, 'reason', e)})"
    if d.get("last_push_age", 0) > STALE_PUSH:
        return f"сервер работает, но данных от агентов нет {int(d['last_push_age'] // 60)} мин"
    return None


def notify(text):
    subprocess.run([NOTIFY, text], capture_output=True, timeout=60)


def main():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    try:
        st = json.load(open(STATE))
    except (OSError, ValueError):
        st = {}
    now = time.time()
    problem = check()
    host = os.uname().nodename
    if problem:
        st.setdefault("since", now)
        st["problem"] = problem
        down = now - st["since"]
        since = time.strftime("%H:%M", time.localtime(st["since"]))
        if down >= DOWN_AFTER and now - st.get("notified", 0) >= REMIND:
            first = "notified" not in st
            notify(("🔴 Lumen недоступен" if first else "🔴 Lumen всё ещё недоступен") +
                   f" с {since}: {problem}. Алерты кластера сейчас не приходят. (проверка с {host})")
            st["notified"] = now
    elif st:
        if "notified" in st:
            notify(f"✅ Lumen снова работает (не работал {int((now - st['since']) // 60)} мин). (проверка с {host})")
        st = {}
    tmp = STATE + ".new"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)
    print(problem or "ok")


if __name__ == "__main__":
    sys.exit(main())
