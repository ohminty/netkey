#!/usr/bin/env bash
# NetKey health report (read-only: changes nothing, safe during a party). Paste its output when asking for help.
#   sudo /opt/netkey/diagnose.sh
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

h() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
line_numbers() { compgen -G '/etc/ppp/peers/netkey[0-9]*' | sed 's#.*/netkey##' | sort -n; }

report() {
    h "System"
    . /etc/os-release; echo "$PRETTY_NAME, $(uname -m), kernel $(uname -r)"
    grep -E '^Model' /proc/cpuinfo
    /usr/sbin/pppd --version 2>&1 | head -1; nft --version; python3 --version
    stat -c 'pppd: %A %U:%G' /usr/sbin/pppd

    h "Lines (plug-and-play registry /var/lib/netkey/lines.json, $(line_numbers | wc -l) provisioned)"
    python3 - <<'EOF'
import json, os, subprocess
try:
    lines = json.load(open("/var/lib/netkey/lines.json"))["lines"]
except (OSError, ValueError, KeyError):
    lines = {}
up = subprocess.run(["ip", "-br", "link", "show", "type", "ppp"], capture_output=True, text=True).stdout
for key, n in sorted(lines.items(), key=lambda kv: kv[1]):
    dev = "/dev/" + key
    tty = os.path.realpath(dev) if os.path.exists(dev) else "(unplugged)"
    call = "IN CALL" if f"ppp{n} " in up else "idle"
    print(f"line {n}: {tty:13} {call:8} {key}")
if not lines:
    print("no adapters seen yet")
EOF
    ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null

    h "Services"
    systemctl --no-pager list-units --all 'netkey*'
    journalctl -u netkey-modem -n 25 --no-pager -o short

    h "Web GUI"
    journalctl -u netkey-web -n 5 --no-pager -o cat
    curl -sk --max-time 5 https://localhost/api/session && echo || echo "web GUI not answering on https://localhost/"
    echo "users file: $(stat -c '%y' /var/lib/netkey/pap-users.json 2>/dev/null || echo missing)," \
         "applied: $(cat /var/lib/netkey/pap-synced.json 2>/dev/null || echo never)"

    h "LAN party port"
    systemctl is-active netkey-lan.service 2>/dev/null | sed 's/^/service: /'
    python3 - <<'EOF'
import json
try:
    s = json.load(open("/run/netkey-lan/status.json"))
except (OSError, ValueError):
    print("no status (service not running)")
else:
    nic = s.get("nic") or {}
    print(f"state: {s['state']} {s.get('detail', '')}".rstrip())
    print(f"nic: {nic.get('name', '-')} {nic.get('mac', '')} carrier={nic.get('carrier')} speed={nic.get('speed')}")
    print(f"subnet {s['subnet']} gw {s['gateway']}, share internet: {s['share_internet']}, leases: {len(s['leases'])}")
    for l in s["leases"]:
        print(f"  {l['ip']:15} {l['mac']} {l['hostname']}")
EOF
    ip netns list 2>/dev/null | sed 's/^/netns: /'
    nft list table inet netkey_lan >/dev/null 2>&1 && echo "nft table inet netkey_lan loaded" || echo "nft table inet netkey_lan not loaded"

    h "PPP"
    ip -br addr show type ppp 2>/dev/null || true
    journalctl -t pppd -n 10 --no-pager -o short 2>/dev/null | tail -10

    h "Routing / NAT"
    echo "ip_forward=$(sysctl -n net.ipv4.ip_forward)"
    ip route get 1.1.1.1 | head -1
    nft list table inet netkey_nat 2>/dev/null || echo "table inet netkey_nat NOT loaded"
    if command -v iptables >/dev/null && iptables -S FORWARD 2>/dev/null | grep -q -- '-P FORWARD DROP'; then
        echo "WARNING: iptables FORWARD policy is DROP (e.g. Docker); add ppp+ exceptions"
    fi
    ping -c1 -W2 1.1.1.1 >/dev/null && echo "upstream ping OK" || echo "upstream ping FAILED"
    getent hosts example.com >/dev/null && echo "upstream DNS OK" || echo "upstream DNS FAILED"

    h "Conflicts"
    for s in ModemManager brltty; do echo "$s: $(systemctl is-active $s 2>/dev/null)"; done
}

report
