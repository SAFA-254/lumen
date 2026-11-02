#!/bin/bash
# Обновление Luxmon. Сервер живёт в CT 108 (https://192.168.3.7), агенты — на pve1-3,
# в CT 101 (etcd-arbiter), CT 108 и в VM 104-106 (ставятся через qemu-guest-agent).
# Запускать на pve1 из каталога репозитория. Существующие конфиги (/etc/luxmon/*.json, telegram.env, ключ TLS)
# не трогаются — обновляются код, юниты и сертификат сервера; агенту в VM конфиг создаётся только при первой установке.
set -euo pipefail
cd "$(dirname "$0")"
SERVER=$(python3 -c 'import json;print(json.load(open("/etc/luxmon/agent.json"))["server"])')
# VM: vmid -> модули агента
declare -A VM_MODULES=([104]='["system","ssh","logs","procs","updates"]' [105]='["system","ssh","logs","procs","updates","postgres"]' [106]='["system","ssh","logs","procs","updates","postgres"]')

# Гости под HA переезжают между узлами, поэтому узел определяем при каждом запуске
node_of() {
  pvesh get /cluster/resources --type vm --output-format json | python3 -c "
import json, sys
print(next(r['node'] for r in json.load(sys.stdin) if r['vmid'] == $1))"
}
ip_of_node() { getent hosts "$1" | awk '{print $1; exit}'; }

push_ct() { # ct файл-на-узле путь-в-ct [права]
  ssh $CT_NODE "pct push $1 $2 $3 ${4:+--perms $4}"
}
ct_begin() { # ct: выбрать узел контейнера и положить на него файлы
  CT_NODE=root@$(ip_of_node "$(node_of $1)")
  tmp=$(ssh $CT_NODE mktemp -d)
  scp -q agent/luxmon-agent.py deploy/luxmon-server.service deploy/luxmon-agent.service deploy/luxmon-server-setup.sh \
    deploy/server.crt $CT_NODE:$tmp/
}

echo "== CT 108: сервер + агент"
ct_begin 108
tar -czf - --exclude=__pycache__ -C server luxmon-server.py luxmon tg-notify.py web | ssh $CT_NODE "cat > $tmp/app.tgz"
push_ct 108 $tmp/app.tgz /tmp/app.tgz
push_ct 108 $tmp/luxmon-agent.py /opt/luxmon/luxmon-agent.py
push_ct 108 $tmp/luxmon-server.service /etc/systemd/system/luxmon-server.service
push_ct 108 $tmp/luxmon-agent.service /etc/systemd/system/luxmon-agent.service
push_ct 108 $tmp/server.crt /etc/luxmon/server.crt 644
push_ct 108 $tmp/luxmon-server-setup.sh /opt/luxmon/luxmon-server-setup.sh 755
# сервер работает от пользователя luxmon: setup создаёт его и отдаёт ему базу и конфиг сервера
ssh $CT_NODE "pct exec 108 -- sh -c 'tar -C /opt/luxmon -xzf /tmp/app.tgz && rm /tmp/app.tgz && chmod 755 /opt/luxmon/tg-notify.py /opt/luxmon/luxmon-server.py \
  && /opt/luxmon/luxmon-server-setup.sh && systemctl daemon-reload && systemctl restart luxmon-server luxmon-agent'; rm -rf $tmp"
SERVER_NODE=$CT_NODE

echo "== CT 101 etcd-arbiter: агент"
ct_begin 101
push_ct 101 $tmp/luxmon-agent.py /opt/luxmon/luxmon-agent.py
push_ct 101 $tmp/luxmon-agent.service /etc/systemd/system/luxmon-agent.service
push_ct 101 $tmp/server.crt /etc/luxmon/server.crt 644
ssh $CT_NODE "pct exec 101 -- sh -c 'systemctl daemon-reload && systemctl restart luxmon-agent'; rm -rf $tmp"

for ip in 192.168.3.3 192.168.3.4 192.168.3.5; do
  echo "== $ip: агент"
  scp -q agent/luxmon-agent.py root@$ip:/opt/luxmon/
  scp -q deploy/server.crt root@$ip:/etc/luxmon/server.crt
  scp -q deploy/luxmon-agent.service root@$ip:/etc/systemd/system/
  ssh root@$ip 'systemctl daemon-reload && systemctl restart luxmon-agent'
done

# Сторож живёт на pve1 — не на узле с Luxmon; шлёт в Telegram через /root/tg-notify.sh
echo "== pve1: сторож Luxmon"
install -m 755 deploy/luxmon-watchdog.py /opt/luxmon/luxmon-watchdog.py
install -m 644 deploy/luxmon-watchdog.service deploy/luxmon-watchdog.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --quiet --now luxmon-watchdog.timer

# Команда в VM через qemu-guest-agent; stdin передаётся в гостя. Возвращает код выхода команды в госте.
guest() { # node vmid команда [stdin-файл]
  local node=$1 vmid=$2 cmd=$3 input=${4:-/dev/null}
  ssh root@$node "qm guest exec $vmid --timeout 60 --pass-stdin 1 -- sh -c '$cmd'" < "$input" | python3 -c '
import json, sys
d = json.load(sys.stdin)
sys.stdout.write(d.get("out-data", "")); sys.stderr.write(d.get("err-data", ""))
sys.exit(d.get("exitcode", 1))'
}

for vmid in "${!VM_MODULES[@]}"; do
  node=$(ip_of_node "$(node_of $vmid)")
  echo "== VM $vmid (через $node): агент"
  guest $node $vmid 'mkdir -p /opt/luxmon /etc/luxmon && chmod 700 /etc/luxmon'
  guest $node $vmid 'cat > /opt/luxmon/luxmon-agent.py' agent/luxmon-agent.py
  guest $node $vmid 'cat > /etc/luxmon/server.crt' deploy/server.crt
  guest $node $vmid 'cat > /etc/systemd/system/luxmon-agent.service' deploy/luxmon-agent.service
  # конфиг пишется только при первой установке: имя хоста — hostname гостя, токен — свой для этого хоста
  if ! guest $node $vmid 'test -f /etc/luxmon/agent.json'; then
    host=$(guest $node $vmid hostname | tr -d '[:space:]')
    token=$(ssh $SERVER_NODE "pct exec 108 -- /opt/luxmon/luxmon-server.py token add $host")
    cfg=$(mktemp)
    echo "{\"server\":\"$SERVER\",\"token\":\"$token\",\"host\":\"$host\",\"modules\":${VM_MODULES[$vmid]},\"ca_file\":\"/etc/luxmon/server.crt\"}" > "$cfg"
    guest $node $vmid 'umask 077; cat > /etc/luxmon/agent.json' "$cfg"
    rm -f "$cfg"
  fi
  guest $node $vmid 'systemctl daemon-reload && systemctl enable --quiet luxmon-agent && systemctl restart luxmon-agent && systemctl is-active luxmon-agent'
done

sleep 20
curl -s -m5 --cacert deploy/server.crt -o /dev/null -w "сервер: HTTP %{http_code}\n" $SERVER/login || true
echo "Готово: $SERVER"
