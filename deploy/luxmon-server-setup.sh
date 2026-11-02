#!/bin/sh
# Пользователь luxmon для сервера и права на его файлы. Можно запускать повторно.
set -e
id luxmon >/dev/null 2>&1 || useradd --system --home-dir /var/lib/luxmon --shell /usr/sbin/nologin luxmon
mkdir -p /var/lib/luxmon
chown -R luxmon:luxmon /var/lib/luxmon
chmod 700 /var/lib/luxmon
# в /etc/luxmon может лежать и конфиг агента (root, 600): каталог открыт группе luxmon только на чтение
chgrp luxmon /etc/luxmon
chmod 750 /etc/luxmon
python3 - <<'PY'
import json, os, shutil
cfg = json.load(open("/etc/luxmon/server.json"))
for p in ["/etc/luxmon/server.json", "/etc/luxmon/telegram.env", cfg.get("tls_key"), cfg.get("tls_cert")]:
    if p and os.path.exists(p):
        shutil.chown(p, "luxmon", "luxmon")
PY
