#!/usr/bin/env python3
"""Alpha Luxmon, сервер: принимает метрики от агентов, хранит в SQLite, отдаёт API и веб-интерфейс,
шлёт алерты, утреннюю сводку и отвечает на команды в Telegram. Только стандартная библиотека Python.

Использование:
  luxmon-server.py               запустить сервер
  luxmon-server.py passwd USER   задать пароль для входа в веб-интерфейс"""
import ssl
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from luxmon.config import CFG, CONFIG_PATH
from luxmon.analytics import rollup_and_prune
from luxmon.alerts import ALERTS
from luxmon.reports import maybe_send_summary
from luxmon.bot import bot_loop
from luxmon.auth import cmd_passwd
from luxmon.httpd import Handler, Redirect, TLSServer


def background():
    tick = 0
    while True:
        time.sleep(15)
        tick += 1
        try:
            ALERTS.evaluate()
            if tick % 20 == 1:
                rollup_and_prune()
            if tick % 4 == 0:
                maybe_send_summary()
        except Exception as e:  # фоновый цикл не должен падать
            print("background error:", repr(e), file=sys.stderr, flush=True)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "passwd":
        return cmd_passwd(sys.argv[2])
    if not CFG["token"]:
        sys.exit("token не задан в " + CONFIG_PATH)
    threading.Thread(target=background, daemon=True).start()
    threading.Thread(target=bot_loop, daemon=True).start()
    if CFG["tls_cert"]:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(CFG["tls_cert"], CFG["tls_key"])
        srv = TLSServer((CFG["listen"], CFG["port"]), Handler, ctx)
    else:
        srv = ThreadingHTTPServer((CFG["listen"], CFG["port"]), Handler)
    if CFG["redirect_port"]:
        red = ThreadingHTTPServer((CFG["listen"], CFG["redirect_port"]), Redirect)
        threading.Thread(target=red.serve_forever, daemon=True).start()
    print(f"luxmon server on {'https' if CFG['tls_cert'] else 'http'}://{CFG['listen']}:{CFG['port']}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
