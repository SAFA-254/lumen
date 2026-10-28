#!/bin/bash
# Обновление Lumen. Сервер живёт в CT 108 (lumen, 192.168.3.37) на pve2, агенты — на pve1-3,
# в CT 101 (etcd-arbiter) и в CT 108. Запускать на pve1 из /root/lumen.
# Конфиги (/etc/lumen/*.json, telegram.env) не трогаются — обновляются только код и юниты.
set -euo pipefail
cd "$(dirname "$0")"
CT_NODE=root@192.168.3.4   # pve2: там CT 101 и CT 108

push_ct() { # ct файл-на-узле путь-в-ct [права]
  ssh $CT_NODE "pct push $1 $2 $3 ${4:+--perms $4}"
}

tmp=$(ssh $CT_NODE mktemp -d)
tar -czf - -C server lumen-server.py tg-notify.py web | ssh $CT_NODE "cat > $tmp/app.tgz"
scp -q agent/lumen-agent.py deploy/lumen-server.service deploy/lumen-agent.service $CT_NODE:$tmp/

echo "== CT 108 lumen: сервер + агент"
push_ct 108 $tmp/app.tgz /tmp/app.tgz
push_ct 108 $tmp/lumen-agent.py /opt/lumen/lumen-agent.py
push_ct 108 $tmp/lumen-server.service /etc/systemd/system/lumen-server.service
push_ct 108 $tmp/lumen-agent.service /etc/systemd/system/lumen-agent.service
ssh $CT_NODE "pct exec 108 -- sh -c 'tar -C /opt/lumen -xzf /tmp/app.tgz && rm /tmp/app.tgz && chmod 755 /opt/lumen/tg-notify.py \
  && systemctl daemon-reload && systemctl restart lumen-server lumen-agent'"

echo "== CT 101 etcd-arbiter: агент"
push_ct 101 $tmp/lumen-agent.py /opt/lumen/lumen-agent.py
push_ct 101 $tmp/lumen-agent.service /etc/systemd/system/lumen-agent.service
ssh $CT_NODE "pct exec 101 -- sh -c 'systemctl daemon-reload && systemctl restart lumen-agent'; rm -rf $tmp"

for ip in 192.168.3.3 192.168.3.4 192.168.3.5; do
  echo "== $ip: агент"
  scp -q agent/lumen-agent.py root@$ip:/opt/lumen/
  scp -q deploy/lumen-agent.service root@$ip:/etc/systemd/system/
  ssh root@$ip 'systemctl daemon-reload && systemctl restart lumen-agent'
done

sleep 20
curl -s -m5 http://192.168.3.37:8095/api/overview | python3 -c 'import json,sys;d=json.load(sys.stdin);print("хосты:", [(h["name"], round(h["age"])) for h in d["hosts"]])'
echo "Готово: http://192.168.3.37:8095"
