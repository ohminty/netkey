#!/usr/bin/env bash
# Install or update netkey on Raspberry Pi OS / Debian 12-13. Idempotent; run as root from this directory.
#   sudo ./install.sh [--force-config] [--run-as-root] [--force] [--hostname NAME | --keep-hostname]
# A first install names the box "netkey" (reachable as netkey.local); updates keep the current name.
# Plug-and-play: any USB serial adapter becomes a modem line (up to lines.max in the config).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
FORCE=0 FORCE_CONFIG=0 RUN_AS_ROOT=0 CHANGED=0 NEW_HOSTNAME="" KEEP_HOSTNAME=0
DEFAULT_HOSTNAME=netkey
PKGS=(ppp nftables socat python3 usbutils openssl curl dnsmasq-base busybox iproute2 wireguard-tools iputils-arping iw rfkill
      avahi-daemon)

log() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force-config) FORCE_CONFIG=1; shift ;;
        --run-as-root) RUN_AS_ROOT=1; shift ;;
        --force) FORCE=1; shift ;;
        --hostname) NEW_HOSTNAME=${2:-}; shift 2 || die "--hostname needs a name" ;;
        --keep-hostname) KEEP_HOSTNAME=1; shift ;;
        -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

# put SRC DST MODE OWNER:GROUP -- install only when content differs
put() {
    local src=$1 dst=$2 mode=$3 own=$4
    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        chmod "$mode" "$dst"; chown "$own" "$dst"; return
    fi
    install -D -m "$mode" -o "${own%:*}" -g "${own#*:}" "$src" "$dst"
    echo "    updated $dst"
    CHANGED=1
}

[[ $EUID -eq 0 ]] || die "run as root (sudo ./install.sh)"
if [[ -n $NEW_HOSTNAME ]]; then
    NEW_HOSTNAME=${NEW_HOSTNAME%.local}
    [[ $NEW_HOSTNAME =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] ||
        die "--hostname: use lowercase letters, digits and dashes (e.g. netkey or netkey-attic)"
elif ((!KEEP_HOSTNAME)) && [[ ! -e /var/lib/netkey/netkey.db && ! -e /var/lib/pi-modem ]]; then
    NEW_HOSTNAME=$DEFAULT_HOSTNAME          # first install only: updates never rename a box
fi

# set_hostname NAME -- rename the box so it answers as NAME.local (mDNS via avahi)
set_hostname() {
    local new=$1 old
    old=$(hostname)
    if [[ $old != "$new" ]]; then
        rename_host "$old" "$new"
    else
        echo "    hostname is already $new"
    fi
    keep_hostname_on_boot
}

rename_host() {
    local old=$1 new=$2
    if grep -q '^127\.0\.1\.1[[:space:]]' /etc/hosts; then
        sed -i "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t$new/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$new" >>/etc/hosts
    fi
    hostnamectl set-hostname "$new" 2>/dev/null || { echo "$new" >/etc/hostname; hostname "$new"; }
    systemctl try-restart avahi-daemon 2>/dev/null || true
    echo "    renamed this box: $old -> $new (reachable as $new.local)"
}

keep_hostname_on_boot() {
    if [[ -d /etc/cloud ]]; then
        # cloud-init re-applies the hostname from its first-boot data on every boot; keep ours
        printf '# written by NetKey install.sh: keep the hostname NetKey set\npreserve_hostname: true\n' \
            >/etc/cloud/cloud.cfg.d/99-netkey.cfg
        if [[ -f /boot/firmware/user-data && ! -e /etc/cloud/cloud-init.disabled ]] &&
            [[ $(cloud-init status 2>/dev/null || true) == *"status: done"* ]]; then
            # Raspberry Pi Imager settings (user, Wi-Fi, SSH) were applied on the first boot; without this,
            # cloud-init would also rewrite /etc/hosts with the old name at every boot
            printf 'Disabled by NetKey install.sh after the first-boot setup (it would undo the hostname).\n' \
                >/etc/cloud/cloud-init.disabled
            echo "    cloud-init: first-boot setup already done; disabled so it keeps the new hostname"
        fi
    fi
}

log "Installing NetKey $(cat "$SRC/VERSION" 2>/dev/null || echo dev)"
log "Checking system"
. /etc/os-release
if [[ ! "${ID:-}" =~ ^(raspbian|debian)$ || ! "${VERSION_ID:-}" =~ ^(12|13)$ ]]; then
    ((FORCE)) || die "unsupported OS ${PRETTY_NAME:-unknown}; tested on Raspberry Pi OS/Debian 12-13 (use --force)"
fi
echo "    $PRETTY_NAME, $(uname -m)"

log "Packages"
missing=()
for p in "${PKGS[@]}"; do dpkg -s "$p" &>/dev/null || missing+=("$p"); done
if ((${#missing[@]})); then
    echo "    installing: ${missing[*]}"
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${missing[@]}"
else
    echo "    all present"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python3 >= 3.11 required"
if [[ "$(stat -c '%a %U %G' /usr/sbin/pppd)" != "4754 root dip" ]]; then
    ((RUN_AS_ROOT)) || die "/usr/sbin/pppd is not 4754 root:dip; re-run with --run-as-root"
fi

# Upgrade from the pre-rename "pi-modem": keep config, certificate, users and history; drop the old units.
if [[ -d /opt/pi-modem || -d /etc/pi-modem || -d /var/lib/pi-modem ]] || id pimodem &>/dev/null; then
    log "Migrating pi-modem -> netkey"
    systemctl stop pi-modem.target 'pi-modem@*.service' pi-modem-lan.service pi-modem.service pi-modem-web.service \
        pi-modem-users.path pi-modem-users.service pi-modem-nat.service 2>/dev/null || true
    systemctl disable pi-modem.target pi-modem-nat.service pi-modem.service pi-modem-web.service \
        pi-modem-users.path pi-modem-lan.service 2>/dev/null || true
    ip netns del pimodem-lan 2>/dev/null || true
    nft delete table inet pi_modem_lan 2>/dev/null || true
    nft delete table inet pi_modem 2>/dev/null || true
    rm -rf /etc/systemd/system/pi-modem.target.wants /opt/pi-modem
    rm -f /etc/systemd/system/pi-modem*.service /etc/systemd/system/pi-modem.target \
          /etc/systemd/system/pi-modem-users.path /etc/udev/rules.d/80-pi-modem.rules \
          /etc/ppp/peers/pi-modem /etc/ppp/peers/pi-modem[0-9]* /etc/sysctl.d/90-pi-modem-forwarding.conf \
          /etc/ppp/ip-up.d/pi-modem /etc/ppp/ip-down.d/pi-modem /etc/NetworkManager/conf.d/90-pi-modem-lan.conf
    [[ -f /etc/ppp/pap-secrets ]] && sed -i '/^# BEGIN pi-modem/,/^# END pi-modem/d' /etc/ppp/pap-secrets
    if [[ -d /etc/pi-modem && ! -e /etc/netkey ]]; then
        mv /etc/pi-modem /etc/netkey
        rm -f /etc/netkey/pi-modem.nft
        [[ -f /etc/netkey/config.toml ]] && sed -i -e 's/pi-modem/netkey/g' -e 's/pimodem/netkey/g' /etc/netkey/config.toml
        echo "    moved /etc/pi-modem -> /etc/netkey (config and TLS certificate kept)"
    fi
    if [[ -d /var/lib/pi-modem && ! -e /var/lib/netkey ]]; then
        mv /var/lib/pi-modem /var/lib/netkey
        for x in "" -wal -shm; do
            [[ -e /var/lib/netkey/pimodem.db$x ]] && mv "/var/lib/netkey/pimodem.db$x" "/var/lib/netkey/netkey.db$x"
        done
        rm -f /var/lib/netkey/party-state.json     # old invite codes are no longer valid
        echo "    moved /var/lib/pi-modem -> /var/lib/netkey (users, call history, hub registration kept)"
    fi
    if id pimodem &>/dev/null && ! id netkey &>/dev/null; then
        pkill -u pimodem 2>/dev/null && sleep 1
        usermod -l netkey pimodem
        getent group pimodem >/dev/null && groupmod -n netkey pimodem
        echo "    renamed user pimodem -> netkey (same uid, files keep their owner)"
    fi
    if command -v nmcli >/dev/null; then
        if nmcli -t -f NAME connection show 2>/dev/null | grep -qx pi-modem-wifi; then
            nmcli connection modify pi-modem-wifi connection.id netkey-wifi && echo "    renamed Wi-Fi profile -> netkey-wifi"
        fi
        for c in pi-modem-hotspot pi-modem-wifi-new; do nmcli connection delete "$c" &>/dev/null || true; done
        nmcli general reload conf 2>/dev/null || true
    fi
    systemctl daemon-reload
    CHANGED=1
fi

log "USB serial adapters (plug-and-play)"
put "$SRC/udev/80-netkey.rules" /etc/udev/rules.d/80-netkey.rules 0644 root:root
udevadm control --reload
udevadm trigger --action=change --subsystem-match=tty
udevadm settle
ls /dev/serial/by-path/ 2>/dev/null | grep -v usbv | sed 's/^/    present: /' || echo "    none plugged in yet"

log "Service account"
if ! id netkey &>/dev/null; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin netkey
    echo "    created user netkey"
fi
usermod -aG dialout,dip netkey

log "Files"
shopt -s nullglob                  # tests/ only ships in developer builds
for f in "$SRC"/README.md "$SRC"/VERSION "$SRC"/netkey/*.py "$SRC"/tests/*.py "$SRC"/ppp/netkey.peer.in "$SRC"/web/*; do
    f=${f#"$SRC/"}
    put "$SRC/$f" "/opt/netkey/$f" 0644 root:root
done
for f in /opt/netkey/netkey/*.py /opt/netkey/tests/*.py /opt/netkey/web/*; do
    [[ -e "$SRC/${f#/opt/netkey/}" ]] || { rm -f "$f"; echo "    removed $f"; CHANGED=1; }
done
shopt -u nullglob
for f in modem.py diagnose.sh uninstall.sh netkey-passreset; do
    put "$SRC/$f" "/opt/netkey/$f" 0755 root:root
done
if [[ -e /opt/netkey/selftest-party.sh ]]; then     # moved to the developer tools (netkey-dev/pitests) in 0.10.4
    rm -f /opt/netkey/selftest-party.sh; echo "    removed /opt/netkey/selftest-party.sh"
fi
ln -sfn /opt/netkey/netkey-passreset /usr/local/sbin/netkey-passreset     # forgot the GUI password: sudo netkey-passreset
install -d -m 0750 -o root -g netkey /etc/netkey
if [[ ! -f /etc/netkey/config.toml ]] || ((FORCE_CONFIG)); then
    put "$SRC/config/config.toml" /etc/netkey/config.toml 0640 root:netkey
else
    echo "    keeping /etc/netkey/config.toml (use --force-config to replace)"
    chown root:netkey /etc/netkey/config.toml; chmod 0640 /etc/netkey/config.toml
fi
put "$SRC/ppp/ip-up.sh" /etc/ppp/ip-up.d/netkey 0755 root:root
put "$SRC/ppp/ip-down.sh" /etc/ppp/ip-down.d/netkey 0755 root:root
put "$SRC/nft/netkey.nft" /etc/netkey/netkey.nft 0644 root:root
put "$SRC/sysctl/90-netkey-forwarding.conf" /etc/sysctl.d/90-netkey-forwarding.conf 0644 root:root
MAX_LINES=$(python3 /opt/netkey/modem.py --config /etc/netkey/config.toml --print-max-lines)
echo "    provisioning $MAX_LINES line(s) (lines.max)"
for ((n = 0; n < MAX_LINES; n++)); do
    tmp=$(mktemp)
    python3 /opt/netkey/modem.py --config /etc/netkey/config.toml --line "$n" \
        --render-peer "$SRC/ppp/netkey.peer.in" >"$tmp"
    put "$tmp" "/etc/ppp/peers/netkey$n" 0640 root:dip
    rm -f "$tmp"
done
for f in /etc/ppp/peers/netkey /etc/ppp/peers/netkey[0-9]*; do
    [[ -e "$f" ]] || continue
    n=${f#/etc/ppp/peers/netkey}
    if [[ -z "$n" ]] || ((n >= MAX_LINES)); then rm -f "$f"; echo "    removed $f"; CHANGED=1; fi
done
for u in netkey-modem.service netkey-web.service; do
    unit=$(mktemp)
    if ((RUN_AS_ROOT)); then
        grep -vE '^(User|Group|SupplementaryGroups)=' "$SRC/systemd/$u" >"$unit"
    else
        cp "$SRC/systemd/$u" "$unit"
    fi
    put "$unit" "/etc/systemd/system/$u" 0644 root:root
    rm -f "$unit"
done
for u in netkey-nat.service netkey-users.service netkey-users.path netkey-lan.service; do
    put "$SRC/systemd/$u" "/etc/systemd/system/$u" 0644 root:root
done
put "$SRC/systemd/netkey.target" /etc/systemd/system/netkey.target 0644 root:root

log "Name on the local network"
if [[ -n $NEW_HOSTNAME ]]; then
    set_hostname "$NEW_HOSTNAME"
else
    echo "    keeping hostname $(hostname) (reachable as $(hostname).local)"
fi

log "Web GUI certificate (/etc/netkey/tls)"
install -d -m 0750 -o root -g netkey /etc/netkey/tls
if [[ ! -s /etc/netkey/tls/cert.pem || ! -s /etc/netkey/tls/key.pem ]]; then
    host=$(hostname)
    san="DNS:$host,DNS:$host.local,DNS:localhost,IP:127.0.0.1"
    for ip in $(hostname -I 2>/dev/null); do [[ $ip == *:* ]] || san+=",IP:$ip"; done
    openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -days 3650 -sha256 \
        -subj "/CN=$host/O=NetKey" -addext "subjectAltName=$san" \
        -keyout /etc/netkey/tls/key.pem -out /etc/netkey/tls/cert.pem 2>/dev/null
    echo "    created self-signed certificate for $san"
    CHANGED=1
else
    echo "    keeping existing certificate (delete /etc/netkey/tls/*.pem to regenerate)"
fi
chown root:netkey /etc/netkey/tls/cert.pem /etc/netkey/tls/key.pem
chmod 0644 /etc/netkey/tls/cert.pem
chmod 0640 /etc/netkey/tls/key.pem

log "Dial-in users and web GUI login (/var/lib/netkey)"
install -d -m 0750 -o netkey -g netkey /var/lib/netkey
install -d -m 2770 -o netkey -g netkey /var/lib/netkey/party-cmd    # LAN party commands from the GUI
state_user=netkey; ((RUN_AS_ROOT)) && state_user=root
runuser -u "$state_user" -- python3 /opt/netkey/modem.py --config /etc/netkey/config.toml \
    --state-dir /var/lib/netkey --init-db | sed 's/^/    /'
runuser -u "$state_user" -- python3 /opt/netkey/modem.py --config /etc/netkey/config.toml \
    --state-dir /var/lib/netkey --reset-admin --if-unset | sed 's/^/    /'
python3 /opt/netkey/modem.py --config /etc/netkey/config.toml --state-dir /var/lib/netkey \
    --sync-pap-secrets | sed 's/^/    /'

log "Validating"
python3 -m py_compile /opt/netkey/modem.py /opt/netkey/netkey/*.py
python3 -c 'import sqlite3, ssl' || die "python3 lacks sqlite3/ssl"
nft -c -f /etc/netkey/netkey.nft
dry_user=netkey; ((RUN_AS_ROOT)) && dry_user=root
for ((n = 0; n < MAX_LINES; n++)); do
    if ! out=$(runuser -u "$dry_user" -- script -qec "/usr/sbin/pppd call netkey$n 115200 dryrun" /dev/null 2>&1); then
        echo "$out" >&2
        die "pppd rejected /etc/ppp/peers/netkey$n"
    fi
done
echo "    pppd accepted $MAX_LINES peers file(s)"
systemd-analyze verify /etc/systemd/system/netkey-modem.service /etc/systemd/system/netkey-nat.service \
    /etc/systemd/system/netkey-web.service /etc/systemd/system/netkey-users.service \
    /etc/systemd/system/netkey-users.path /etc/systemd/system/netkey-lan.service \
    /etc/systemd/system/netkey.target 2>&1 | grep -v '^$' | sed 's/^/    /' || true
if compgen -G "/opt/netkey/tests/test_*.py" >/dev/null; then
    (cd /opt/netkey && runuser -u "$dry_user" -- python3 -m unittest discover -s tests -q 2>&1 | tail -3)
fi

log "Announcing the web GUI on the LAN (mDNS)"
read -r bind web_port http_port < <(python3 -c 'import sys; sys.path.insert(0, "/opt/netkey")
from netkey.config import load_config; w = load_config("/etc/netkey/config.toml")["web"]
print(w["bind"] or "0.0.0.0", w["port"], w["http_port"])')
if [[ $bind == 127.* || $bind == localhost ]]; then
    rm -f /etc/avahi/services/netkey.service
    echo "    web GUI bound to $bind: not announced"
else
    tmp=$(mktemp)
    cat >"$tmp" <<EOS
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- written by NetKey install.sh: the web GUI shows up in Bonjour/mDNS browsers -->
<service-group>
  <name replace-wildcards="yes">NetKey on %h</name>
  <service><type>_https._tcp</type><port>$web_port</port><txt-record>path=/</txt-record></service>
</service-group>
EOS
    put "$tmp" /etc/avahi/services/netkey.service 0644 root:root
    rm -f "$tmp"
fi
systemctl enable -q --now avahi-daemon 2>/dev/null || warn "avahi-daemon did not start: $(hostname).local will not work"
# avahi takes the hostname only when it starts (e.g. before cloud-init renamed the box at boot); keep "NAME-2" style
# names, which mean another device on the LAN already uses NAME.local
announced=$(busctl call org.freedesktop.Avahi / org.freedesktop.Avahi.Server GetHostName 2>/dev/null |
    sed -n 's/^s "\(.*\)"$/\1/p')
if [[ -n $announced && $announced != "$(hostname)" && $announced != "$(hostname)"-[0-9]* ]]; then
    systemctl restart avahi-daemon && sleep 2
    echo "    avahi announced the old name $announced.local; restarted it as $(hostname).local"
fi

log "Enabling"
sysctl -q -p /etc/sysctl.d/90-netkey-forwarding.conf
systemctl daemon-reload
units=(netkey.target netkey-nat.service netkey-modem.service netkey-web.service netkey-users.path netkey-lan.service)
systemctl enable -q "${units[@]}"
if ((CHANGED)); then
    systemctl restart netkey.target
    echo "    restarted netkey.target"
fi
systemctl start "${units[@]}"
sleep 5
systemctl --no-pager list-units --all 'netkey*' | sed -n '1,4p' | sed 's/^/    /' || true
journalctl -u netkey-modem -n 6 --no-pager -o cat | sed 's/^/    /'

journalctl -u netkey-web -n 3 --no-pager -o cat | sed 's/^/    /'

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
# the name avahi really announces: NAME-2.local etc. when another device on the LAN already uses NAME.local
mdns=$(busctl call org.freedesktop.Avahi / org.freedesktop.Avahi.Server GetHostNameFqdn 2>/dev/null |
    sed -n 's/^s "\(.*\)"$/\1/p')
log "Done. NetKey is running."
if [[ $bind == 127.* || $bind == localhost ]]; then
    echo "    Web GUI: only on this machine ($bind). From your computer: ssh -L 8443:127.0.0.1:443 $(hostname),"
    echo "    then https://localhost:8443/"
else
    [[ -n $mdns ]] && echo "    Web GUI: https://$mdns/  (or https://${ip:-$(hostname)}/)" ||
        echo "    Web GUI: https://${ip:-$(hostname)}/"
    echo "    The certificate is self-signed: accept the browser warning once."
fi
echo "    First sign-in: admin / admin; NetKey then asks you to choose your own password."
echo "    No network? The box opens Wi-Fi NetKey-XXXX (password netkeyrescue) with the GUI at http://192.168.4.1"
