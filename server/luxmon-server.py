#!/usr/bin/env python3
"""Alpha Luxmon, сервер: принимает метрики от агентов, хранит в SQLite, отдаёт API и веб-интерфейс,
шлёт алерты, утреннюю сводку и отвечает на команды в Telegram. Только стандартная библиотека Python.

Использование:
  luxmon-server.py               запустить сервер
  luxmon-server.py passwd USER   задать пароль для входа в веб-интерфейс
  luxmon-server.py role USER admin|viewer
                                 viewer — только просмотр, без настроек и обслуживания
  luxmon-server.py token add HOST | token del HOST | token list
                                 токены агентов: у каждого хоста свой"""
import os
import ssl
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from luxmon.config import CFG


def as_db_owner():
    """Команды из консоли запускают от root, а база принадлежит пользователю сервера:
    работаем от его имени, иначе созданные файлы базы окажутся серверу недоступны."""
    d = os.path.dirname(CFG["db"])
    if os.geteuid() == 0 and os.path.isdir(d) and os.stat(d).st_uid:
        st = os.stat(d)
        os.setgroups([])
        os.setgid(st.st_gid)
        os.setuid(st.st_uid)


def background():
    from luxmon.analytics import rollup_and_prune
    from luxmon.alerts import ALERTS
    from luxmon.reports import maybe_send_summary
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
        from luxmon.passwd import cmd_passwd
        return cmd_passwd(sys.argv[2])
    if len(sys.argv) == 4 and sys.argv[1] == "role":
        from luxmon.passwd import cmd_role
        return cmd_role(sys.argv[2], sys.argv[3])
    if len(sys.argv) >= 2 and sys.argv[1] == "token":
        as_db_owner()
        from luxmon.tokens import cmd_token
        return cmd_token(sys.argv[2:])
    if len(sys.argv) > 1:
        sys.exit(__doc__)
    from luxmon.bot import bot_loop
    from luxmon.httpd import Handler, Redirect, TLSServer
    from luxmon.tokens import any_tokens
    if not any_tokens():
        print("токенов агентов нет: выдайте их командой token add ХОСТ", file=sys.stderr, flush=True)
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
