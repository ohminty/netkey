#!/bin/sh
# netkey: tell the line worker / web GUI who logged in (installed as /etc/ppp/ip-up.d/netkey).
# pppd runs this as root with PPP_IFACE, PPP_REMOTE, PPP_IPPARAM and PEERNAME in the environment.
case "$PPP_IPPARAM" in netkey[0-9]*) ;; *) exit 0 ;; esac
line=${PPP_IPPARAM#netkey}
dir=/run/netkey
[ -d "$dir" ] || exit 0
user=$(printf '%s' "$PEERNAME" | tr -cd 'A-Za-z0-9._@-' | cut -c1-64)
iface=$(printf '%s' "$PPP_IFACE" | tr -cd 'a-z0-9')
ip=$(printf '%s' "$PPP_REMOTE" | tr -cd '0-9.')
tmp="$dir/.ppp$line.$$"
printf '{"user": "%s", "iface": "%s", "ip": "%s", "up_at": %s}\n' "$user" "$iface" "$ip" "$(date +%s)" >"$tmp" &&
    chown netkey:netkey "$tmp" 2>/dev/null; mv -f "$tmp" "$dir/ppp$line.json"
exit 0
