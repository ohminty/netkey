"""Retro-LAN protocol counters.

An nft table on the party bridge (br-retro, inside the party namespace) counts every frame once, as it enters the
bridge, by protocol and by where it came from: "lan" = this box's retro port, "party" = the tunnel to other boxes.
The table is loaded once per namespace and never reloaded, so party changes don't reset it. Only counters: no
payload is stored. Frames the Pi sends itself (DHCP/DNS answers) are not counted.
"""
import json

TABLE = "netkey_stats"
DIRS = ("lan", "party")

# key, label, group -- in the order the GUI lists them
PROTOCOLS = [
    ("ipx_8023", "IPX · Ethernet_802.3", "IPX"),
    ("ipx_8022", "IPX · Ethernet_802.2", "IPX"),
    ("ipx_ii", "IPX · Ethernet_II", "IPX"),
    ("ipx_snap", "IPX · Ethernet_SNAP", "IPX"),
    ("netbeui", "NetBEUI", "NetBEUI"),
    ("appletalk", "AppleTalk (Macs)", "AppleTalk"),
    ("nbt", "NetBIOS/SMB over TCP/IP", "TCP/IP"),
    ("tcp", "TCP", "TCP/IP"),
    ("udp", "UDP", "TCP/IP"),
    ("icmp", "ICMP", "TCP/IP"),
    ("ip_other", "Other IPv4", "TCP/IP"),
    ("arp", "ARP", "TCP/IP"),
    ("ipv6", "IPv6 (blocked)", "Other"),
    ("other", "Other", "Other"),
]
KEYS = [p[0] for p in PROTOCOLS]
NEWEST = "appletalk"    # the last key added: a table without its counter is from an older version, made again
IPX_FRAME_TYPES = {"ipx_8023": "Ethernet_802.3", "ipx_8022": "Ethernet_802.2", "ipx_ii": "Ethernet_II",
                   "ipx_snap": "Ethernet_SNAP"}

LLC = "ether type 0x0000-0x05ff"            # 802.3 frame: the type field is a length
# first match wins (each rule ends in "return")
MATCH = [
    ("ipx_ii", ["ether type 0x8137"]),
    ("ipx_8023", [f"{LLC} @ll,112,16 0xffff"]),                    # raw 802.3: IPX checksum 0xFFFF right away
    ("ipx_8022", [f"{LLC} @ll,112,16 0xe0e0"]),                    # LLC DSAP/SSAP E0 (Novell)
    ("ipx_snap", [f"{LLC} @ll,112,16 0xaaaa @ll,160,16 0x8137"]),  # LLC AA + SNAP type 8137
    ("netbeui", [f"{LLC} @ll,112,16 0xf0f0"]),                     # LLC DSAP/SSAP F0 (NetBIOS)
    # EtherTalk Phase 2 (every Mac with Ethernet): LLC AA + SNAP type 809B (DDP) or 80F3 (AARP); Phase 1: Ethernet II
    ("appletalk", [f"{LLC} @ll,112,16 0xaaaa @ll,160,16 0x809b", f"{LLC} @ll,112,16 0xaaaa @ll,160,16 0x80f3",
                   "ether type 0x809b", "ether type 0x80f3"]),
    ("nbt", ["ether type ip udp dport { 137, 138 }", "ether type ip udp sport { 137, 138 }",
             "ether type ip tcp dport { 139, 445 }", "ether type ip tcp sport { 139, 445 }"]),
    ("tcp", ["ether type ip ip protocol tcp"]),
    ("udp", ["ether type ip ip protocol udp"]),
    ("icmp", ["ether type ip ip protocol icmp"]),
    ("ip_other", ["ether type ip"]),
    ("arp", ["ether type arp"]),
    ("ipv6", ["ether type ip6"]),
    ("other", [""]),
]


def ruleset():
    """nft script that creates the counter table (load it only when the table doesn't exist yet)."""
    out = [f"table bridge {TABLE} {{"]
    out += [f"    counter {d}_{k} {{ }}" for d in DIRS for k in KEYS]
    for d in DIRS:
        out.append(f"    chain {d} {{")
        for key, exprs in MATCH:
            out += [f"        {e + ' ' if e else ''}counter name {d}_{key} return" for e in exprs]
        out.append("    }")
    out += ["    chain prerouting {", "        type filter hook prerouting priority -300; policy accept;",
            '        iifname "vx-*" jump party', "        jump lan", "    }", "}", ""]
    return "\n".join(out)


def parse_counters(text):
    """`nft -j list counters table bridge netkey_stats` -> {"lan": {key: [packets, bytes]}, "party": {...}}."""
    out = {d: {k: [0, 0] for k in KEYS} for d in DIRS}
    try:
        items = json.loads(text or "{}").get("nftables", [])
    except ValueError:
        return out
    for item in items:
        c = item.get("counter") if isinstance(item, dict) else None
        if not c or c.get("table") != TABLE:
            continue
        d, _, key = str(c.get("name", "")).partition("_")
        if d in out and key in out[d]:
            out[d][key] = [int(c.get("packets", 0)), int(c.get("bytes", 0))]
    return out


def deltas(prev, cur):
    """Counter growth since the previous reading; a counter that went down was reset (table recreated)."""
    out = {}
    for d in DIRS:
        for k in KEYS:
            p, b = cur.get(d, {}).get(k, [0, 0])
            pp, pb = (prev or {}).get(d, {}).get(k, [0, 0])
            if p < pp or b < pb:
                pp = pb = 0
            if p - pp or b - pb:
                out[(d, k)] = (p - pp, b - pb)
    return out


def ipx_mismatch(packets_by_key):
    """IPX frame types seen (by packets) -- more than one means PCs that can't see each other."""
    seen = [IPX_FRAME_TYPES[k] for k in IPX_FRAME_TYPES if packets_by_key.get(k, 0) > 0]
    return seen, len(seen) > 1
