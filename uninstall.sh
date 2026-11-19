#!/usr/bin/env bash
# Remove netkey: service, NAT table, project files and pap-secrets block. Packages are kept.
#   sudo ./uninstall.sh [--purge-config]
set -euo pipefail
PURGE=0
[[ "${1:-}" == "--purge-config" ]] && PURGE=1
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

systemctl stop netkey.target netkey-nat.service netkey-modem.service netkey-web.service \
    netkey-users.path netkey-users.service netkey-lan.service netkey-update.path 2>/dev/null || true
[[ -x /opt/netkey/modem.py ]] && python3 /opt/netkey/modem.py --config /etc/netkey/config.toml \
    --lan-teardown 2>/dev/null || true
rm -f /etc/NetworkManager/conf.d/90-netkey-lan.conf
command -v nmcli >/dev/null && nmcli general reload conf 2>/dev/null || true
systemctl disable netkey.target netkey-nat.service netkey-modem.service netkey-web.service \
    netkey-users.path netkey-lan.service netkey-update.path 2>/dev/null || true
nft delete table inet netkey_lan 2>/dev/null || true
nft delete table inet netkey_nat 2>/dev/null || true
rm -f /etc/systemd/system/netkey-modem.service \
      /etc/systemd/system/netkey-nat.service /etc/systemd/system/netkey.target \
      /etc/udev/rules.d/80-netkey.rules /etc/ppp/peers/netkey /etc/ppp/peers/netkey[0-9]* \
      /etc/sysctl.d/90-netkey-forwarding.conf /etc/netkey/netkey.nft \
      /etc/systemd/system/netkey-web.service /etc/systemd/system/netkey-users.service \
      /etc/systemd/system/netkey-users.path /etc/ppp/ip-up.d/netkey /etc/ppp/ip-down.d/netkey \
      /etc/systemd/system/netkey-lan.service /etc/avahi/services/netkey.service \
      /etc/systemd/system/netkey-update.service /etc/systemd/system/netkey-update.path
rm -f /usr/local/sbin/netkey-passreset
rm -rf /opt/netkey /etc/systemd/system/netkey.target.wants
if [[ -f /etc/ppp/pap-secrets ]]; then
    sed -i '/^# BEGIN netkey/,/^# END netkey/d' /etc/ppp/pap-secrets
fi
((PURGE)) && rm -rf /etc/netkey /var/lib/netkey /var/lib/netkey-update
systemctl unmask -q smbd.service nmbd.service samba-ad-dc.service 2>/dev/null || true   # masked by install.sh
systemctl daemon-reload
udevadm control --reload
id netkey &>/dev/null && userdel netkey
((PURGE)) && id netkey-share &>/dev/null && userdel netkey-share
echo "netkey removed. net.ipv4.ip_forward left at $(sysctl -n net.ipv4.ip_forward) (reboot resets it)."
((PURGE)) || echo "Config, TLS certificate and user DB kept in /etc/netkey and /var/lib/netkey (use --purge-config to remove)."
echo "The shared folder /srv/netkey is kept (your files); remove it yourself if you don't need it."
