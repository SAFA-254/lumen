#!/bin/bash
# Обновление Lumen. Сервер живёт в CT 108 (lumen, https://192.168.3.7) на pve2, агенты — на pve1-3,
# в CT 101 (etcd-arbiter), CT 108 и в VM 104-106 (ставятся через qemu-guest-agent).
# Запускать на pve1 из /root/lumen. Существующие конфиги (/etc/lumen/*.json, telegram.env, ключ TLS)
# не трогаются — обновляются код, юниты и сертификат сервера; агенту в VM конфиг создаётся только при первой установке.
set -euo pipefail
cd "$(dirname "$0")"
CT_NODE=root@192.168.3.4   # pve2: там CT 101 и CT 108
SERVER=https://192.168.3.7
TOKEN=$(python3 -c 'import json;print(json.load(open("/etc/lumen/agent.json"))["token"])')
# VM: vmid -> модули агента
declare -A VM_MODULES=([104]='["system","ssh"]' [105]='["system","ssh","postgres"]' [106]='["system","ssh","postgres"]')

push_ct() { # ct файл-на-узле путь-в-ct [права]
  ssh $CT_NODE "pct push $1 $2 $3 ${4:+--perms $4}"
}

tmp=$(ssh $CT_NODE mktemp -d)
tar -czf - -C server lumen-server.py tg-notify.py web | ssh $CT_NODE "cat > $tmp/app.tgz"
scp -q agent/lumen-agent.py deploy/lumen-server.service deploy/lumen-agent.service deploy/server.crt $CT_NODE:$tmp/

echo "== CT 108 lumen: сервер + агент"
push_ct 108 $tmp/app.tgz /tmp/app.tgz
push_ct 108 $tmp/lumen-agent.py /opt/lumen/lumen-agent.py
push_ct 108 $tmp/lumen-server.service /etc/systemd/system/lumen-server.service
push_ct 108 $tmp/lumen-agent.service /etc/systemd/system/lumen-agent.service
push_ct 108 $tmp/server.crt /etc/lumen/server.crt 644
ssh $CT_NODE "pct exec 108 -- sh -c 'tar -C /opt/lumen -xzf /tmp/app.tgz && rm /tmp/app.tgz && chmod 755 /opt/lumen/tg-notify.py /opt/lumen/lumen-server.py \
  && systemctl daemon-reload && systemctl restart lumen-server lumen-agent'"

echo "== CT 101 etcd-arbiter: агент"
push_ct 101 $tmp/lumen-agent.py /opt/lumen/lumen-agent.py
push_ct 101 $tmp/lumen-agent.service /etc/systemd/system/lumen-agent.service
push_ct 101 $tmp/server.crt /etc/lumen/server.crt 644
ssh $CT_NODE "pct exec 101 -- sh -c 'systemctl daemon-reload && systemctl restart lumen-agent'; rm -rf $tmp"

for ip in 192.168.3.3 192.168.3.4 192.168.3.5; do
  echo "== $ip: агент"
  scp -q agent/lumen-agent.py root@$ip:/opt/lumen/
  scp -q deploy/server.crt root@$ip:/etc/lumen/server.crt
  scp -q deploy/lumen-agent.service root@$ip:/etc/systemd/system/
  ssh root@$ip 'systemctl daemon-reload && systemctl restart lumen-agent'
done

# Сторож живёт на pve1 — не на узле с Lumen; шлёт в Telegram через /root/tg-notify.sh
echo "== pve1: сторож Lumen"
install -m 755 deploy/lumen-watchdog.py /opt/lumen/lumen-watchdog.py
install -m 644 deploy/lumen-watchdog.service deploy/lumen-watchdog.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --quiet --now lumen-watchdog.timer

# Команда в VM через qemu-guest-agent; stdin передаётся в гостя. Возвращает код выхода команды в госте.
guest() { # node vmid команда [stdin-файл]
  local node=$1 vmid=$2 cmd=$3 input=${4:-/dev/null}
  ssh root@$node "qm guest exec $vmid --timeout 60 --pass-stdin 1 -- sh -c '$cmd'" < "$input" | python3 -c '
import json, sys
d = json.load(sys.stdin)
sys.stdout.write(d.get("out-data", "")); sys.stderr.write(d.get("err-data", ""))
sys.exit(d.get("exitcode", 1))'
}
node_of() {
  pvesh get /cluster/resources --type vm --output-format json | python3 -c "
import json, sys
print(next(r['node'] for r in json.load(sys.stdin) if r['vmid'] == $1))"
}
ip_of_node() { getent hosts "$1" | awk '{print $1; exit}'; }

for vmid in "${!VM_MODULES[@]}"; do
  node=$(ip_of_node "$(node_of $vmid)")
  echo "== VM $vmid (через $node): агент"
  guest $node $vmid 'mkdir -p /opt/lumen /etc/lumen && chmod 700 /etc/lumen'
  guest $node $vmid 'cat > /opt/lumen/lumen-agent.py' agent/lumen-agent.py
  guest $node $vmid 'cat > /etc/lumen/server.crt' deploy/server.crt
  guest $node $vmid 'cat > /etc/systemd/system/lumen-agent.service' deploy/lumen-agent.service
  cfg=$(mktemp)
  echo "{\"server\":\"$SERVER\",\"token\":\"$TOKEN\",\"host\":\"\",\"modules\":${VM_MODULES[$vmid]},\"ca_file\":\"/etc/lumen/server.crt\"}" > "$cfg"
  # host берётся из hostname гостя; конфиг пишется только если его ещё нет
  guest $node $vmid 'test -f /etc/lumen/agent.json || { umask 077; python3 -c "import json,sys,socket;c=json.load(sys.stdin);c[\"host\"]=socket.gethostname();json.dump(c,open(\"/etc/lumen/agent.json\",\"w\"))"; }' "$cfg"
  rm -f "$cfg"
  guest $node $vmid 'systemctl daemon-reload && systemctl enable --quiet lumen-agent && systemctl restart lumen-agent && systemctl is-active lumen-agent'
done

sleep 20
curl -s -m5 --cacert deploy/server.crt -o /dev/null -w "сервер: HTTP %{http_code}\n" $SERVER/login || true
echo "Готово: $SERVER"
