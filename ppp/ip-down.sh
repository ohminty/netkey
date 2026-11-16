#!/bin/sh
# netkey: PPP session ended (installed as /etc/ppp/ip-down.d/netkey).
case "$PPP_IPPARAM" in netkey[0-9]*) ;; *) exit 0 ;; esac
rm -f "/run/netkey/ppp${PPP_IPPARAM#netkey}.json"
exit 0
