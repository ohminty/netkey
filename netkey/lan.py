"""LAN party port: a second Ethernet NIC (retro-LAN) bridged into an isolated network namespace.

Runs as root (netkey-lan.service): `modem.py --lan`. Reconciles every few seconds:
  main ns:  eth0 (WAN, mgmt) ... pm-lan0 <transit /30> --- NAT --- WAN       nft table inet netkey_lan
  party ns: br-retro = retro NIC + VXLAN ports to party members, gateway .1, dnsmasq (DHCP/DNS), NAT to pm-lan1
Roles: standalone, host (as standalone + WireGuard hub for up to 15 members), member (pure L2 bridge of
the retro NIC into the tunnel to the host; no IP, DHCP or NAT on the party side). See party.py.
The retro NIC starts in a quarantine bridge (br-probe) and joins br-retro only after a leak probe: the Pi sends a
gratuitous ARP on its WAN NIC; if that frame shows up on the retro NIC, the port is cabled into the home network.
The party namespace has no route to the home LAN, dial-in clients or the Pi; it may reach the internet
only when lan.share_internet is on. Root never opens the SQLite DB: GUI settings come from settings.json.
"""
import collections
import ipaddress
import queue
import json
import re
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from . import fileshare as fs
from . import hubclient as hc
from . import identity as ident
from . import party as pt
from . import protostats as ps
from .config import apply_overrides, box_name
from .store import SETTINGS_EXPORT, read_json, write_json_atomic
from .uplink import NET_FILE, Uplink

log = logging.getLogger("netkey")

RUN_DIR = "/run/netkey-lan"
WATCH_FILE = "party-watch.json"   # planned public events to join automatically when their host presses Start
WATCH_EARLY_S = 30 * 60          # hold the hub's event channel from 30 min before an event's start...
WATCH_LATE_S = 2 * 3600          # ...until 2 h after it (the hub's start window)
STATUS_FILE = "status.json"
LEASE_FILE = "leases"
# Short DHCP leases: PCs renew every 5 minutes. A PC that kept its own box's address into a party then renews
# with the host, whose dnsmasq refuses an address another PC holds, and the PC asks for a fresh one.
LEASE_TIME = "10m"
GARP_AFTER_S = (1, 10, 45, 120)        # gateway announcements after the layout or the party's members change
SCAN_EVERY_S = 60                      # duplicate-address scan of the retro LAN
STATIC_SEEN_S = 900                    # a PC with a fixed address counts as connected this long after it was heard
NAME_RETRY_S = 600                     # Windows name lookups of fixed-address PCs that didn't answer
NM_CONF = "/etc/NetworkManager/conf.d/90-netkey-lan.conf"
RETRO_PORT_FILE = "retro-port.json"     # state dir: MAC of the NIC last used as the retro port (kept off when disabled)
EXTRA_PORTS_FILE = "extra-ports.json"   # state dir: MACs of extra wired adapters this box switched off
ADOPT_MAX, ADOPT_WINDOW_S = 6, 120
ADOPTED_FILE = "adopted.json"           # run dir (cleared at boot): ifindexes already moved into the namespace
# NICs that stop delivering frames to the retro LAN (no errors, link fine) when moved into the namespace a second
# time; a driver reset gives a fresh device. lan78xx: the Pi 3B+ built-in port, seen at 10 Mbit half duplex.
RESET_DRIVERS = {"lan78xx"}
VETH_MAIN, VETH_NS, BRIDGE = "pm-lan0", "pm-lan1", "br-retro"
LOOP_S = 2.0
PROBE_BRIDGE = "br-probe"
PROBE_EVERY_S = 60
RELAY_RE = re.compile(r"^[a-z0-9-]{0,32}$")


def relay_host(value):
    """A relay address from the hub: an IPv4 address, or None for the hub's own relay."""
    if not value:
        return None
    try:
        return str(ipaddress.IPv4Address(str(value)))
    except ValueError:
        raise ValueError("bad relay address from the hub") from None


def clean_relay(value):
    return "".join(ch for ch in str(value or "") if ch.isalnum() or ch in "-._")[:32] or None
SKIP_PREFIXES = ("lo", "veth", "br", "wg", "ppp", "tun", "tap", "docker", "vxlan", "vx-", "pm-", "virbr", "dummy", "bond",
                 "tailscale", "zt")
PRIVATE_V4 = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
              "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4"]


# pure helpers (unit-tested)
def addresses(cfg):
    lan = cfg["lan"]
    net = ipaddress.IPv4Network(lan["subnet"])
    transit = ipaddress.IPv4Network(lan["transit"])
    hosts = list(net.hosts())
    first = hosts[min(99, len(hosts) // 2)]
    return {
        "net": str(net), "prefix": net.prefixlen, "netmask": str(net.netmask), "gateway": str(hosts[0]),
        "broadcast": str(net.broadcast_address),
        "dhcp_first": str(first), "dhcp_last": str(hosts[-2] if len(hosts) > 4 else hosts[-1]),
        "transit": str(transit), "transit_main": str(list(transit.hosts())[0]),
        "transit_ns": str(list(transit.hosts())[1]),
    }


def ip_in(ip, net):
    try:
        return ipaddress.IPv4Address(ip) in net
    except ValueError:
        return False


def read_sys(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def candidate_nics(sys_net="/sys/class/net", exclude=()):
    """Physical, wired Ethernet NICs that may become the retro-LAN port (never the WAN/default-route NIC)."""
    out = []
    try:
        names = sorted(os.listdir(sys_net))
    except OSError:
        return out
    for name in names:
        base = os.path.join(sys_net, name)
        if name in exclude or name.startswith(SKIP_PREFIXES):
            continue
        if read_sys(os.path.join(base, "type")) != "1" or not os.path.exists(os.path.join(base, "device")):
            continue
        if os.path.exists(os.path.join(base, "wireless")) or os.path.exists(os.path.join(base, "phy80211")):
            continue
        out.append({"name": name, "mac": (read_sys(os.path.join(base, "address")) or "").lower()})
    return out


def choose_nic(configured, candidates, all_ifaces, exclude, prefer=None):
    """(name, error). configured = "" (auto), "none" (no retro port), an interface name or a MAC. Never the WAN NIC.
    Auto with several adapters: the one used last time (prefer = its MAC), else the first by name."""
    configured = (configured or "").strip().lower()
    if configured == "none":
        return None, None
    if configured:
        for iface in all_ifaces:
            if configured in (iface["name"].lower(), iface["mac"]):
                if iface["name"] in exclude:
                    return None, f"{iface['name']} carries the default route (WAN); refusing to use it as retro-LAN"
                return iface["name"], None
        return None, f"configured retro-LAN interface {configured} not found"
    if not candidates:
        return None, None
    return next((c["name"] for c in candidates if prefer and c["mac"] == prefer), candidates[0]["name"]), None


def main_ruleset(cfg, share):
    """nft table in the main namespace: the party may only reach the internet (and only when shared)."""
    a = addresses(cfg)
    private = ", ".join(PRIVATE_V4)
    egress = (f'iifname "{VETH_MAIN}" ip daddr {{ {private} }} counter drop\n'
              f'        iifname "{VETH_MAIN}" counter accept') if share else \
        f'iifname "{VETH_MAIN}" counter drop'
    return f"""table inet netkey_lan
delete table inet netkey_lan
table inet netkey_lan {{
    chain input {{
        type filter hook input priority filter; policy accept;
        iifname "{VETH_MAIN}" ct state established,related accept
        iifname "{VETH_MAIN}" counter drop
    }}
    chain forward {{
        type filter hook forward priority filter - 1; policy accept;
        iifname "{VETH_MAIN}" tcp flags syn / syn,rst tcp option maxseg size set rt mtu
        iifname "{VETH_MAIN}" oifname "ppp*" counter drop
        iifname "ppp*" oifname "{VETH_MAIN}" counter drop
        oifname "{VETH_MAIN}" ct state established,related accept
        oifname "{VETH_MAIN}" counter drop
        {egress}
    }}
    chain postrouting {{
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr {a["transit"]} oifname != "{VETH_MAIN}" masquerade
    }}
}}
"""


def ns_ruleset(cfg, share, tunnel_rules=(), role="standalone"):
    """nft table inside the party namespace (default drop; IPv6 is disabled there as well).
    Members have no gateway: nothing is routed, they only bridge the retro port into the tunnel."""
    extra = "".join(f"        {r}\n" for r in tunnel_rules)
    if role == "member":
        return f"""table inet netkey_party
delete table inet netkey_party
table inet netkey_party {{
    chain input {{
        type filter hook input priority filter; policy drop;
        iif "lo" accept
        ct state established,related accept
{extra}        counter drop
    }}
    chain forward {{
        type filter hook forward priority filter; policy drop;
        counter drop
    }}
}}
"""
    a = addresses(cfg)
    tcp_share = ", ".join(map(str, fs.TCP_PORTS)) + f", {fs.PASV_PORTS[0]}-{fs.PASV_PORTS[1]}"   # file sharing
    udp_share = ", ".join(map(str, fs.UDP_PORTS))
    out = f'iifname "{BRIDGE}" oifname "{VETH_NS}" counter accept' if share else \
        f'iifname "{BRIDGE}" oifname "{VETH_NS}" counter drop'
    return f"""table inet netkey_party
delete table inet netkey_party
table inet netkey_party {{
    chain input {{
        type filter hook input priority filter; policy drop;
        iif "lo" accept
        ct state established,related accept
        iifname "{BRIDGE}" ip daddr != {{ {a["gateway"]}, {a["broadcast"]}, 255.255.255.255 }} counter drop
        iifname "{BRIDGE}" udp dport {{ 53, 67 }} accept
        iifname "{BRIDGE}" tcp dport 53 accept
        iifname "{BRIDGE}" tcp dport {{ {tcp_share} }} accept
        iifname "{BRIDGE}" udp dport {{ {udp_share} }} accept
        iifname "{BRIDGE}" icmp type {{ echo-request, destination-unreachable, time-exceeded }} accept
{extra}        counter drop
    }}
    chain forward {{
        type filter hook forward priority filter; policy drop;
        ct state established,related accept
        {out}
        counter drop
    }}
    chain output {{
        type filter hook output priority filter; policy accept;
    }}
    chain postrouting {{
        type nat hook postrouting priority srcnat; policy accept;
        oifname "{VETH_NS}" masquerade
    }}
}}
"""


def dnsmasq_cmd(cfg, share, run_dir=RUN_DIR):
    a = addresses(cfg)
    cmd = ["dnsmasq", "--keep-in-foreground", "--conf-file=/dev/null", f"--interface={BRIDGE}", "--bind-interfaces",
           f"--dhcp-range={a['dhcp_first']},{a['dhcp_last']},{a['netmask']},{LEASE_TIME}", "--dhcp-authoritative",
           "--dhcp-broadcast",                 # old DOS/Win9x clients can miss unicast offers before they have an IP
           f"--dhcp-option=option:router,{a['gateway']}", f"--dhcp-option=option:dns-server,{a['gateway']}",
           f"--dhcp-leasefile={os.path.join(run_dir, LEASE_FILE)}", "--pid-file=", "--domain=lan", "--local=/lan/",
           "--expand-hosts", "--no-resolv", "--no-hosts", "--log-facility=-", "--user=nobody", "--group=nogroup"]
    # the box by name for the file share: http://netkey/, ftp://netkey, \\NETKEY (Win9x also asks DNS)
    names = sorted({"netkey", fs.netbios_name().lower()})
    cmd.append(f"--host-record={','.join(names)},{a['gateway']}")
    if share:
        cmd += [f"--server={d}" for d in cfg["lan"]["dns"]]
    return cmd


def parse_leases(text):
    """dnsmasq lease file lines: expiry mac ip hostname client-id."""
    leases = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 4:
            leases.append({"expires": int(parts[0]) if parts[0].isdigit() else None, "mac": parts[1],
                           "ip": parts[2], "hostname": "" if parts[3] == "*" else parts[3]})
    return sorted(leases, key=lambda x: tuple(int(p) for p in x["ip"].split(".")) if x["ip"].count(".") == 3 else ())


def nm_conf(macs):
    return "# written by netkey-lan: NetworkManager must leave the retro-LAN port alone\n[keyfile]\n" \
           f"unmanaged-devices={';'.join(f'mac:{m}' for m in sorted(macs))}\n"


# the reconciler (root)
LEAK_TEXT = ("The retro-LAN port is cabled into your home network (it sees this Pi's own network traffic). "
             "It stays isolated until it is connected to retro PCs or a separate hub.")


class LanDaemon:
    def __init__(self, base_cfg, state_dir, interface=None, run_dir=RUN_DIR, sys_net="/sys/class/net",
                 share=None, netns=None):
        self.base = base_cfg
        self.state_dir = state_dir
        self.override_iface = interface
        self.share_override = share
        self.netns_override = netns
        self.run_dir = run_dir
        self.sys_net = sys_net
        self.stopping = False
        self.dnsmasq = None
        self.dns_share = None
        self.cfg = base_cfg
        self.settings_mtime = None
        self.uplink_chosen = False        # network.uplink set in the GUI (else Uplink learns it once)
        self.status = {}
        self.last_error = None
        self.party = pt.PartyState(state_dir)
        self.lock = self.party.lock
        self.server = None
        self.built = None            # (role, share) the namespace layout was built for
        self.stats_since = None      # when the protocol counter table was created in the current namespace
        self.adopted_at = []         # recent namespace moves of the retro NIC (rebuild-loop guard)
        self.parked = set()          # retro port kept switched off while the LAN party port is disabled
        self.extra = {}              # name -> MAC: wired adapters beyond the retro port, kept switched off
        self.share_procs = {}        # file sharing: smbd, nmbd, files (FTP + web) -> Popen
        self.share_sig = None
        self.share_fails = {}        # name -> recent exit times (restart back-off)
        self.share_errors = {}
        self.share_status = {"active": False, "paused": "The LAN service is starting"}
        self.ftp_src = None          # (mtime, size) of the GUI's user export the FTP users were made from
        self.garp_due = []           # monotonic times to announce the gateway (gratuitous ARP)
        self.last_members = frozenset()
        self.conflicts = []          # IP addresses two PCs on the retro LAN answer for
        self.static_pcs = []         # PCs with a fixed address (no DHCP lease) heard on the retro LAN
        self.pc_names = {}           # MAC -> (NetBIOS name or None, when asked)
        self.scan_thread, self.scan_next = None, 0.0
        self.rules_sig = None
        self.wg_text = None
        self.wg_addr = None
        self.nic = None
        self.nic_ok = False          # passed the leak probe and sits in br-retro
        self.leak = None
        self.last_probe = 0.0
        self.results = collections.deque(maxlen=20)
        self.member_rt = {}          # host: member id -> {rtt, last}
        self.ctrl_ids = []           # host: members that may reach the control port (incl. just-kicked ones)
        self.hub = None              # HubClient (cloud hub mode)
        self.hub_key = None
        self.agent = None            # HubAgent: long-poll event channel
        self.dns_cache = {}
        self.relay_probe = None      # last relay measurement {at, relays: [{id, name, rtt_ms}]}
        self.relay_probe_error = None    # (not self.probe: that is the retro-port leak probe method)
        self.prober = None           # RelayProbe thread while one runs
        self.host_rt = {}            # member: link to the host
        self.uplink = Uplink(self)   # Wi-Fi uplink, automatic built-in Ethernet port, rescue hotspot

    def stop(self, *_):
        self.stopping = True

    # helpers
    def ns(self):
        return self.netns_override or self.cfg["lan"]["netns"]

    def run(self, *cmd, check=True, input_text=None):
        r = subprocess.run(list(cmd), input=input_text, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()}")
        return r

    def nsrun(self, *cmd, check=True, input_text=None):
        return self.run("ip", "netns", "exec", self.ns(), *cmd, check=check, input_text=input_text)

    def load_cfg(self):
        path = os.path.join(self.state_dir, SETTINGS_EXPORT)
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self.settings_mtime:
            self.settings_mtime = mtime
            data = read_json(path, {}) or {}
            self.cfg = apply_overrides(self.base, data.get("settings", {}), None, log.warning)
            self.uplink_chosen = "network.uplink" in (data.get("settings") or {})

    def default_route(self):
        r = self.run("ip", "-j", "route", "show", "default", check=False)
        try:
            routes = [x for x in json.loads(r.stdout or "[]") if "dev" in x]
        except ValueError:
            routes = []
        return routes

    def default_ifaces(self):
        return {x["dev"] for x in self.default_route()}

    def wan_info(self):
        """dev, ip, mac of the WAN (default-route) NIC and its gateway's MAC, for the leak probe."""
        routes = self.default_route()
        if not routes:
            return None
        dev, gw = routes[0]["dev"], routes[0].get("gateway")
        r = self.run("ip", "-j", "-4", "addr", "show", "dev", dev, check=False)
        try:
            ip = json.loads(r.stdout)[0]["addr_info"][0]["local"]
        except (ValueError, IndexError, KeyError):
            return None
        info = {"dev": dev, "ip": ip, "mac": (read_sys(os.path.join(self.sys_net, dev, "address")) or "").lower(),
                "gw_mac": None}
        if gw:
            r = self.run("ip", "-j", "neigh", "show", gw, "dev", dev, check=False)
            try:
                info["gw_mac"] = (json.loads(r.stdout)[0].get("lladdr") or "").lower() or None
            except (ValueError, IndexError):
                pass
        return info

    def all_ifaces(self):
        return [{"name": n, "mac": (read_sys(os.path.join(self.sys_net, n, "address")) or "").lower()}
                for n in sorted(os.listdir(self.sys_net))]

    def ns_exists(self):
        return os.path.exists(f"/run/netns/{self.ns()}")

    def ns_links(self):
        if not self.ns_exists():
            return {}
        r = self.run("ip", "-n", self.ns(), "-j", "-d", "-s", "link", "show", check=False)
        try:
            return {l["ifname"]: l for l in json.loads(r.stdout or "[]")}
        except ValueError:
            return {}

    @staticmethod
    def is_retro_candidate(link):
        name = link.get("ifname", "")
        kind = link.get("linkinfo", {}).get("info_kind")
        return name not in ("lo", BRIDGE, PROBE_BRIDGE, VETH_NS, "wg-party") and not name.startswith("vx-") and \
            link.get("link_type") == "ether" and kind in (None, "veth")

    # namespace layout
    def ensure_layout(self, role, share):
        if self.built and self.built[0] != role:
            log.info("lan: role %s -> %s; rebuilding the party namespace", self.built[0], role)
            self.teardown()
        ns = self.ns()
        if not self.ns_exists():
            self.run("ip", "netns", "add", ns)
            log.info("lan: created network namespace %s", ns)
            self.built = None
            self.stats_since = None
        if self.built == (role, share):
            return
        self.run("ip", "-n", ns, "link", "set", "lo", "up")
        # unprivileged ports: the file servers (user netkey-share) listen on 21 and 80 in here; only NetKey's own
        # services run in this namespace
        self.nsrun("sysctl", "-qw", "net.ipv4.ip_forward=1", "net.ipv6.conf.all.disable_ipv6=1",
                   "net.ipv6.conf.default.disable_ipv6=1", "net.ipv4.ip_unprivileged_port_start=0")
        links = self.ns_links()
        for br in (BRIDGE, PROBE_BRIDGE):
            if br not in links:
                self.run("ip", "-n", ns, "link", "add", br, "type", "bridge")
            self.run("ip", "-n", ns, "link", "set", br, "up")
        if role in ("member", "quarantine"):
            # a member is a pure L2 bridge: no gateway, DHCP, NAT or route out (quarantine: LAN port off)
            self.run("ip", "-n", ns, "addr", "flush", "dev", BRIDGE, check=False)
            self.stop_dnsmasq()
            self.run("ip", "link", "del", VETH_MAIN, check=False)
            self.run("nft", "delete", "table", "inet", "netkey_lan", check=False)
        else:
            a = addresses(self.cfg)
            if not os.path.exists(os.path.join(self.sys_net, VETH_MAIN)):
                self.run("ip", "link", "add", VETH_MAIN, "type", "veth", "peer", "name", VETH_NS, "netns", ns)
            self.run("sysctl", "-qw", f"net.ipv6.conf.{VETH_MAIN}.disable_ipv6=1", check=False)
            self.run("ip", "addr", "replace", f"{a['transit_main']}/30", "dev", VETH_MAIN)
            self.run("ip", "link", "set", VETH_MAIN, "up")
            self.run("ip", "-n", ns, "addr", "replace", f"{a['transit_ns']}/30", "dev", VETH_NS)
            self.run("ip", "-n", ns, "link", "set", VETH_NS, "up")
            self.run("ip", "-n", ns, "addr", "replace", f"{a['gateway']}/{a['prefix']}", "dev", BRIDGE)
            self.schedule_garp()                 # the gateway may have been another box until now (a party ended)
            if share:
                self.run("ip", "-n", ns, "route", "replace", "default", "via", a["transit_main"])
            else:
                self.run("ip", "-n", ns, "route", "del", "default", check=False)
        self.built = (role, share)
        self.rules_sig = None

    def ensure_rules(self, role, share, member_ids, ctrl_ids=None):
        sig = (role, share, tuple(member_ids), tuple(ctrl_ids or ()))
        if sig == self.rules_sig:
            return
        if role != "member":
            self.run("nft", "-f", "-", input_text=main_ruleset(self.cfg, share))
        self.nsrun("nft", "-f", "-", input_text=ns_ruleset(self.cfg, share, pt.tunnel_input_rules(
            self.cfg, role, member_ids, ctrl_ids), role))
        self.nsrun("nft", "-f", "-", input_text=pt.guard_ruleset(self.cfg, addresses(self.cfg)["gateway"],
                                                                  member_ids, role))
        self.rules_sig = sig

    # the retro NIC: adoption and leak quarantine
    def find_nic(self, configured, links):
        if configured == "none":
            return None, None, None
        for link in links.values():
            if not self.is_retro_candidate(link):
                continue
            mac = (link.get("address") or "").lower()
            if not configured or configured in (link["ifname"].lower(), mac):
                return link["ifname"], mac, None
        exclude = self.default_ifaces() | self.uplink.exclude()
        main_ifaces = self.all_ifaces()
        last = (read_json(os.path.join(self.state_dir, RETRO_PORT_FILE), {}) or {}).get("mac")
        name, err = choose_nic(configured, candidate_nics(self.sys_net, exclude), main_ifaces, exclude, last)
        return name, next((i["mac"] for i in main_ifaces if i["name"] == name), None), err

    def adopt_nic(self, name, mac, links):
        """Move the retro NIC into the party namespace. It starts in the quarantine bridge until probed."""
        if name not in links:
            if self.reset_if_reused(name):
                self.nic_ok = False
                return                                  # comes back as a fresh device; adopted on the next step
            now = time.monotonic()
            self.adopted_at = [t for t in self.adopted_at if now - t < ADOPT_WINDOW_S]
            if len(self.adopted_at) >= ADOPT_MAX:
                # a rebuild loop: repeated down/up/namespace moves can wedge USB NICs (lan78xx) until a reboot
                raise RuntimeError(f"{name} was moved {len(self.adopted_at)} times in {ADOPT_WINDOW_S} s; "
                                   "waiting before trying again")
            self.adopted_at.append(now)
            self.mark_adopted(name)
            if os.path.exists(os.path.join(self.sys_net, name, "device")):   # real NICs only, not test veths
                self.pin_nm(mac)
            if shutil.which("nmcli"):
                self.run("nmcli", "device", "set", name, "managed", "no", check=False)
            self.run("ip", "link", "set", name, "down")
            self.run("ip", "addr", "flush", "dev", name, check=False)
            self.run("ip", "link", "set", name, "netns", self.ns())
            log.info("lan: retro-LAN port %s (%s) moved into %s (quarantined until probed)", name, mac, self.ns())
            self.nic_ok, self.last_probe = False, 0.0
            self.run("ip", "-n", self.ns(), "link", "set", name, "master", PROBE_BRIDGE)
        elif links[name].get("master") not in (BRIDGE, PROBE_BRIDGE) or \
                (links[name].get("master") == BRIDGE) != self.nic_ok:
            self.run("ip", "-n", self.ns(), "link", "set", name, "master", BRIDGE if self.nic_ok else PROBE_BRIDGE)
        # only when needed: "up" on a link that is already up still makes some drivers (r8152) reprogram their
        # receive filter over USB, every step
        if name not in links or "UP" not in links[name].get("flags", []):
            self.run("ip", "-n", self.ns(), "link", "set", name, "up")
        self.nic = name

    def ns_nic_driver(self, name):
        """The retro NIC's driver (r8152, asix, lan78xx …) while it sits in the namespace (ip netns exec sees its
        sysfs); asked once per NIC."""
        cache = self.__dict__.setdefault("_drivers", {})
        if name not in cache:
            r = self.nsrun("readlink", "-f", f"/sys/class/net/{name}/device/driver", check=False)
            path = (r.stdout or "").strip()
            cache[name] = os.path.basename(path) if r.returncode == 0 and "/drivers/" in path else None
        return cache[name]

    def nic_driver(self, name):
        return os.path.basename(os.path.realpath(os.path.join(self.sys_net, name, "device", "driver")))

    def mark_adopted(self, name):
        idx = read_sys(os.path.join(self.sys_net, name, "ifindex"), "")
        if not idx or self.nic_driver(name) not in RESET_DRIVERS:
            return
        path = os.path.join(self.run_dir, ADOPTED_FILE)
        seen = (read_json(path, {}) or {}).get("ifindex", [])
        if idx not in seen:
            os.makedirs(self.run_dir, exist_ok=True)
            write_json_atomic(path, {"ifindex": (seen + [idx])[-32:]})

    def reset_if_reused(self, name):
        """A RESET_DRIVERS NIC that was in the namespace before (same device since its driver started): reset the
        driver so it is a fresh device again. True when reset."""
        drv = self.nic_driver(name)
        idx = read_sys(os.path.join(self.sys_net, name, "ifindex"), "")
        if drv not in RESET_DRIVERS or not idx or \
                idx not in (read_json(os.path.join(self.run_dir, ADOPTED_FILE), {}) or {}).get("ifindex", []):
            return False
        dev = os.path.basename(os.path.realpath(os.path.join(self.sys_net, name, "device")))
        drv_dir = os.path.realpath(os.path.join(self.sys_net, name, "device", "driver"))
        log.info("lan: %s (%s) was the retro port before; resetting its driver first", name, drv)
        try:
            with open(os.path.join(drv_dir, "unbind"), "w") as f:
                f.write(dev)
        except OSError as e:
            log.warning("lan: could not reset %s: %s", name, e)
            return False
        for attempt in range(5):
            time.sleep(1)
            try:
                with open(os.path.join(drv_dir, "bind"), "w") as f:
                    f.write(dev)
                return True
            except OSError as e:
                err = e
        log.error("lan: %s did not come back after a driver reset: %s", name, err)
        return True

    def move_nic(self, bridge):
        ns = self.ns()
        self.run("ip", "-n", ns, "link", "set", self.nic, "nomaster")    # forgets the MACs learned on it
        self.run("ip", "-n", ns, "link", "set", self.nic, "master", bridge)
        self.run("ip", "-n", ns, "link", "set", self.nic, "up")

    def probe(self, bridge):
        """True when the Pi's own WAN frames, its gateway or the remembered home router appear on the retro NIC:
        cabled into the home LAN."""
        wan = self.wan_info()
        home_gw = (self.uplink.state.get("home") or {}).get("gw_mac")
        if not wan and not home_gw:
            return False
        if wan:
            self.run("arping", "-U", "-q", "-c", "1", "-w", "1", "-I", wan["dev"], wan["ip"], check=False)
            time.sleep(0.4)
        r = self.run("bridge", "-n", self.ns(), "-j", "fdb", "show", "br", bridge, check=False)
        try:
            entries = json.loads(r.stdout or "[]")
        except ValueError:
            return False
        bad = {m for m in ((wan or {}).get("mac"), (wan or {}).get("gw_mac"), home_gw) if m}
        return any(e.get("ifname") == self.nic and (e.get("mac") or "").lower() in bad for e in entries)

    def nic_tick(self, links):
        link = links.get(self.nic, {})
        carrier = "LOWER_UP" in link.get("flags", [])
        now = time.monotonic()
        if not carrier:
            if self.nic_ok:
                self.move_nic(PROBE_BRIDGE)
                self.nic_ok = False
            self.leak, self.last_probe = None, 0.0
            return
        if not self.nic_ok:
            if now - self.last_probe < (1.5 if self.leak is None else 20):
                return
            self.last_probe = now
            leak = self.probe(PROBE_BRIDGE)
            if self.uplink.decide(self.nic, leak):     # wifi uplink: the built-in port is cabled into the home LAN
                return
            if leak:
                if not self.leak:
                    log.warning("lan: %s sees this Pi's home network; keeping it quarantined", self.nic)
                self.leak = LEAK_TEXT
                self.move_nic(PROBE_BRIDGE)       # flush the learned entries before the next probe
                return
            self.leak = None
            self.move_nic(BRIDGE)
            self.nic_ok = True
            log.info("lan: %s passed the leak probe; retro LAN connected", self.nic)
            self.schedule_garp()
            self.remember_retro_port()
        elif now - self.last_probe >= PROBE_EVERY_S:
            self.last_probe = now
            if self.probe(BRIDGE):
                if self.uplink.auto_port() == self.nic:
                    self.move_nic(PROBE_BRIDGE)
                    self.nic_ok = False
                    self.uplink.decide(self.nic, True)
                    return
                log.warning("lan: %s now sees this Pi's home network; quarantined", self.nic)
                self.move_nic(PROBE_BRIDGE)
                self.nic_ok, self.leak = False, LEAK_TEXT

    def pin_nm(self, mac):
        if not mac or not os.path.isdir(os.path.dirname(NM_CONF)):
            return
        macs = set()
        for line in (read_sys(NM_CONF, "") or "").splitlines():
            if line.startswith("unmanaged-devices="):
                macs = {m[4:] for m in line.split("=", 1)[1].split(";") if m.startswith("mac:")}
        if mac in macs:
            return
        macs.add(mac)
        with open(NM_CONF + ".tmp", "w") as f:
            f.write(nm_conf(macs))
        os.replace(NM_CONF + ".tmp", NM_CONF)
        if shutil.which("nmcli"):
            self.run("nmcli", "general", "reload", "conf", check=False)

    def unpin_nm(self, mac):
        if not mac or not os.path.exists(NM_CONF):
            return
        macs = set()
        for line in (read_sys(NM_CONF, "") or "").splitlines():
            if line.startswith("unmanaged-devices="):
                macs = {m[4:] for m in line.split("=", 1)[1].split(";") if m.startswith("mac:")}
        if mac not in macs:
            return
        macs.discard(mac)
        if macs:
            with open(NM_CONF + ".tmp", "w") as f:
                f.write(nm_conf(macs))
            os.replace(NM_CONF + ".tmp", NM_CONF)
        else:
            os.unlink(NM_CONF)
        if shutil.which("nmcli"):
            self.run("nmcli", "general", "reload", "conf", check=False)

    # DHCP/DNS
    # the retro LAN's addresses: who the gateway is, and PCs that share an address
    def schedule_garp(self):
        now = time.monotonic()
        self.garp_due = sorted(set(self.garp_due) | {now + t for t in GARP_AFTER_S})

    def garp_tick(self, role="standalone"):
        """Tell the PCs where 10.99.0.1 is now (gratuitous ARP). After joining a party they still have their own
        box's MAC cached for the gateway; after a party ends, the host's. Windows updates its cache from these."""
        now = time.monotonic()
        if not self.garp_due or self.garp_due[0] > now or not (self.nic_ok or role == "host"):
            return
        self.garp_due = [t for t in self.garp_due if t > now]
        gw = addresses(self.cfg)["gateway"]
        for mode in ("-U", "-A"):                # unsolicited request and reply: old stacks heed one or the other
            self.nsrun("arping", mode, "-c", "1", "-w", "1", "-I", BRIDGE, "-s", gw, gw, check=False)

    def scan_tick(self, role="standalone"):
        """Every minute, in the background: ask the retro LAN who has each address in use (DHCP leases and the
        neighbour table). Two different MACs answering for one address is a clash. A host scans even without a
        working retro port of its own: its party members' PCs are on the same network."""
        if not (self.nic_ok or role == "host"):
            return
        now = time.monotonic()
        if (self.scan_thread and self.scan_thread.is_alive()) or now < self.scan_next:
            return
        self.scan_next = now + SCAN_EVERY_S
        a = addresses(self.cfg)
        net = ipaddress.IPv4Network(a["net"])
        leases = parse_leases(read_sys(os.path.join(self.run_dir, LEASE_FILE), ""))
        ips = {l["ip"] for l in leases}
        r = self.nsrun("ip", "-s", "-j", "neigh", "show", "dev", BRIDGE, check=False)
        try:
            neigh = [n for n in json.loads(r.stdout or "[]") if "FAILED" not in n.get("state", []) and n.get("lladdr")]
        except (ValueError, TypeError):
            neigh = []
        ips |= {n["dst"] for n in neigh}
        self.static_pcs = self.fixed_address_pcs(neigh, leases, a, net)
        ips = sorted((ip for ip in ips if ip != a["gateway"] and ip_in(ip, net)), key=ipaddress.IPv4Address)[:64]
        names = {l["mac"].lower(): l["hostname"] for l in leases}
        self.scan_thread = threading.Thread(target=self.scan, args=(ips, names), daemon=True, name="ip-scan")
        self.scan_thread.start()

    def fixed_address_pcs(self, neigh, leases, a, net):
        """PCs the box hears on the retro LAN without a DHCP lease from it: a fixed address set on the PC (or a
        lease from another box that hasn't run out). Inside the DHCP range they may clash with a lease later."""
        leased = {l["ip"] for l in leases} | {l["mac"].lower() for l in leases}
        lo, hi = ipaddress.IPv4Address(a["dhcp_first"]), ipaddress.IPv4Address(a["dhcp_last"])
        out = []
        for n in neigh:
            ip, mac = n.get("dst", ""), n["lladdr"].lower()
            age = n.get("confirmed", n.get("updated", 0))
            if ip == a["gateway"] or not ip_in(ip, net) or ip in leased or mac in leased or age > STATIC_SEEN_S:
                continue
            out.append({"ip": ip, "mac": mac, "name": (self.pc_names.get(mac) or (None,))[0],
                        "seen": time.time() - age, "in_range": lo <= ipaddress.IPv4Address(ip) <= hi})
        return sorted(out, key=lambda x: ipaddress.IPv4Address(x["ip"]))

    def lookup_names(self):
        """Windows (NetBIOS) names of fixed-address PCs, asked once per PC (again later if it didn't answer)."""
        now = time.time()
        for pc in self.static_pcs:
            name, asked = self.pc_names.get(pc["mac"], (None, 0))
            if name or now - asked < NAME_RETRY_S:
                continue
            r = self.nsrun("nmblookup", "-A", pc["ip"], check=False) if shutil.which("nmblookup") else None
            m = re.search(r"^\s+(\S+)\s+<00>\s+-\s+[BMHP]", (r.stdout if r else "") or "", re.M)
            self.pc_names[pc["mac"]] = (m.group(1) if m else None, now)
            pc["name"] = m.group(1) if m else None

    def scan(self, ips, names):
        found = []
        self.lookup_names()
        for ip in ips:
            if self.stopping:
                return
            r = self.nsrun("arping", "-c", "2", "-w", "2", "-I", BRIDGE, ip, check=False)
            macs = sorted({m.lower() for m in re.findall(r"reply from \S+ \[([0-9A-Fa-f:]{17})\]", r.stdout or "")})
            if len(macs) >= 2:
                found.append({"ip": ip, "pcs": [{"mac": m, "name": names.get(m, "")} for m in macs]})
        prev = {c["ip"]: c for c in self.conflicts}
        for c in found:
            c["since"] = prev[c["ip"]]["since"] if c["ip"] in prev else time.time()
            if c["ip"] not in prev:
                log.warning("lan: two PCs use %s on the retro LAN: %s", c["ip"], ", ".join(
                    f"{p['name'] or '?'} ({p['mac']})" for p in c["pcs"]))
        for ip in set(prev) - {c["ip"] for c in found}:
            log.info("lan: the address clash on %s is gone", ip)
        self.conflicts = found

    # file sharing (fileshare.py): one folder for the retro LAN over Windows sharing, FTP and the web
    def share_paused(self, reason):
        self.stop_fileshare()
        s = self.cfg["share"]
        self.share_status = {"active": False, "paused": reason, "name": fs.netbios_name(), "share": fs.SHARE_NAME,
                             "workgroup": s["workgroup"], "dir": s["dir"],
                             **{k: {"enabled": bool(s[k]), "running": False, "error": None} for k in ("smb", "ftp", "web")}}

    def stop_fileshare(self):
        for name, p in list(self.share_procs.items()):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
            log.info("lan: file sharing: %s stopped", name)
        self.share_procs.clear()
        self.share_sig = None

    def sync_ftp_users(self):
        """The modem users (the GUI's export, read by root) hashed for the FTP server, which never sees passwords."""
        src = os.path.join(self.state_dir, "pap-users.json")
        try:
            st = os.stat(src)
        except OSError:
            st = None
        key = (st.st_mtime, st.st_size) if st else None
        dst = os.path.join(self.run_dir, fs.FTP_USERS_FILE)
        if key == self.ftp_src and os.path.exists(dst):
            return
        old = (read_json(dst, {}) or {}).get("users", {})
        users = (read_json(src, {}) or {}).get("users", []) if st else []
        write_json_atomic(dst, {"users": fs.ftp_users(users, old)}, mode=0o640)
        try:
            import grp
            os.chown(dst, 0, grp.getgrnam(fs.SHARE_USER).gr_gid)
        except (KeyError, PermissionError):
            pass
        self.ftp_src = key

    def share_dir_ready(self, path):
        import grp
        import pwd
        pw = pwd.getpwnam(fs.SHARE_USER)
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            log.info("lan: file sharing: created %s", path)
        st = os.stat(path)
        if (st.st_uid, st.st_gid) != (pw.pw_uid, grp.getgrnam(fs.SHARE_USER).gr_gid) or st.st_mode & 0o7777 != 0o2775:
            os.chown(path, pw.pw_uid, pw.pw_gid)
            os.chmod(path, 0o2775)

    def ensure_fileshare(self, role):
        s, a = self.cfg["share"], addresses(self.cfg)
        if role == "member":
            return self.share_paused("This box is in someone else's party, so its PCs are on the host's network. "
                                     "File sharing is back when the party ends.")
        want, errors = [], {}
        try:
            import pwd
            pwd.getpwnam(fs.SHARE_USER)
        except KeyError:
            return self.share_paused(f"The {fs.SHARE_USER} user is missing: run the NetKey installer again.")
        if s["smb"]:
            if shutil.which("smbd") and shutil.which("nmbd"):
                want += ["smbd", "nmbd"]
                if os.path.exists(os.path.join(fs.SAMBA_LIBEXEC, "samba-dcerpcd")):
                    want.append("dcerpcd")
            else:
                errors["smb"] = "Samba is not installed (sudo apt install samba)"
        ftp, web = bool(s["ftp"]), bool(s["web"])
        if ftp:
            import importlib.util
            if importlib.util.find_spec("pyftpdlib") is None:
                errors["ftp"], ftp = "pyftpdlib is not installed (sudo apt install python3-pyftpdlib)", False
        if ftp or web:
            want.append("files")
        conf = fs.smb_conf(self.cfg, self.run_dir, BRIDGE, a["gateway"])
        sig = (conf, ftp, web, a["gateway"], s["dir"])
        if sig != self.share_sig:
            self.stop_fileshare()
            self.share_sig, self.share_fails, self.share_errors = sig, {}, {}
            smb_dir = os.path.join(self.run_dir, fs.SMB_DIR)
            for sub in ("lock", "state", "cache", "private", "pid", "ncalrpc", "nmbd"):
                os.makedirs(os.path.join(smb_dir, sub), exist_ok=True)
                # nmbd and ncalrpc: Samba insists on 0755. cache: browse.dat, which smbd reads as the guest user
                # when a PC asks for the computer list (Network Neighborhood); 0700 gave every PC an empty list
                os.chmod(os.path.join(smb_dir, sub), 0o755 if sub in ("nmbd", "ncalrpc", "cache") else 0o700)
            with open(os.path.join(smb_dir, "smb.conf.tmp"), "w") as f:
                f.write(conf)
            os.replace(os.path.join(smb_dir, "smb.conf.tmp"), os.path.join(smb_dir, "smb.conf"))
        for name in [n for n in self.share_procs if n not in want]:
            p = self.share_procs.pop(name)
            if p.poll() is None:
                p.terminate()
        if want:
            try:
                self.share_dir_ready(s["dir"])
            except OSError as e:
                return self.share_paused(f"The shared folder {s['dir']} is not usable: {e}")
        if "files" in want and ftp:
            self.sync_ftp_users()
        now = time.monotonic()
        for name in want:
            p = self.share_procs.get(name)
            if p and p.poll() is None:
                continue
            if p:
                self.share_fails.setdefault(name, []).append(now)
                self.share_errors[name] = f"{name} stopped (exit code {p.returncode}); see journalctl -u netkey-lan"
                log.warning("lan: file sharing: %s exited with %s", name, p.returncode)
                self.share_procs.pop(name)
            recent = self.share_fails[name] = [t for t in self.share_fails.get(name, []) if now - t < 120]
            if len(recent) >= 3:
                continue                          # back off: three exits in two minutes
            self.share_procs[name] = subprocess.Popen(self.share_cmd(name, ftp, web), cwd="/", env=self.share_env())
            log.info("lan: file sharing: %s started (pid %d)", name, self.share_procs[name].pid)
        run = {n: bool(self.share_procs.get(n) and self.share_procs[n].poll() is None) for n in ("smbd", "nmbd", "files")}

        def svc(key, running, enabled):
            err = errors.get(key) or (None if running else next(
                (self.share_errors[n] for n in (("smbd", "nmbd", "dcerpcd") if key == "smb" else ("files",))
                 if n in self.share_errors), None))
            return {"enabled": enabled, "running": bool(running and enabled), "error": err if enabled else None}
        self.share_status = {"active": True, "paused": None, "address": a["gateway"], "name": fs.netbios_name(),
                             "share": fs.SHARE_NAME, "workgroup": s["workgroup"], "dir": s["dir"],
                             "writable": bool(s["writable"]),
                             "smb": svc("smb", run["smbd"] and run["nmbd"], bool(s["smb"])),
                             "ftp": svc("ftp", run["files"] and ftp, bool(s["ftp"])),
                             "web": svc("web", run["files"] and web, bool(s["web"]))}

    def share_env(self):
        return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
                "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(fs.__file__)))}

    def share_cmd(self, name, ftp, web):
        ns_exec = ["ip", "netns", "exec", self.ns()]
        conf = os.path.join(self.run_dir, fs.SMB_DIR, "smb.conf")
        if name in ("smbd", "nmbd"):
            return ns_exec + [name, "--foreground", "--no-process-group", "--debug-stdout", f"--configfile={conf}"]
        if name == "dcerpcd":
            return ns_exec + [os.path.join(fs.SAMBA_LIBEXEC, "samba-dcerpcd"), "--foreground", "--no-process-group",
                              "--debug-stdout", f"--configfile={conf}"] + \
                [os.path.join(fs.SAMBA_LIBEXEC, r) for r in fs.RPCDS if os.path.exists(os.path.join(fs.SAMBA_LIBEXEC, r))]
        s, a = self.cfg["share"], addresses(self.cfg)
        return ns_exec + ["setpriv", f"--reuid={fs.SHARE_USER}", f"--regid={fs.SHARE_USER}", "--init-groups",
                          "--no-new-privs", sys.executable, "-m", "netkey.fileshare", "--root", s["dir"],
                          "--address", a["gateway"], "--name", fs.netbios_name(),
                          "--users", os.path.join(self.run_dir, fs.FTP_USERS_FILE)] + \
            (["--ftp"] if ftp else []) + (["--web"] if web else [])

    def stop_dnsmasq(self):
        if self.dnsmasq and self.dnsmasq.poll() is None:
            self.dnsmasq.terminate()
            try:
                self.dnsmasq.wait(5)
            except subprocess.TimeoutExpired:
                self.dnsmasq.kill()
        self.dnsmasq, self.dns_share = None, None

    def ensure_dnsmasq(self, share):
        if self.dnsmasq and self.dnsmasq.poll() is not None:
            log.warning("lan: dnsmasq exited with %s; restarting", self.dnsmasq.returncode)
            self.dnsmasq = None
        if self.dnsmasq and self.dns_share != share:
            self.stop_dnsmasq()
        if self.dnsmasq is None:
            os.makedirs(self.run_dir, exist_ok=True)
            lease = os.path.join(self.run_dir, LEASE_FILE)
            if not os.path.exists(lease):
                open(lease, "a").close()
            try:
                import pwd
                pw = pwd.getpwnam("nobody")
                os.chown(lease, pw.pw_uid, pw.pw_gid)
            except (KeyError, PermissionError):
                pass
            self.dnsmasq = subprocess.Popen(["ip", "netns", "exec", self.ns()] + dnsmasq_cmd(self.cfg, share,
                                                                                               self.run_dir))
            self.dns_share = share
            log.info("lan: dnsmasq started (pid %d), internet sharing %s", self.dnsmasq.pid, "ON" if share else "off")

    # WireGuard and VXLAN
    def ensure_wg(self, private_key, address, peers, listen_port=None):
        ns = self.ns()
        if "wg-party" not in self.ns_links():
            self.run("ip", "link", "del", "wg-party", check=False)            # leftover in the main namespace
            self.run("ip", "link", "add", "wg-party", "type", "wireguard")    # UDP socket lives in the main ns
            self.run("ip", "link", "set", "wg-party", "netns", ns)
            self.wg_text = self.wg_addr = None
        if self.ns_links().get("wg-party", {}).get("mtu") != pt.WG_MTU:     # also fixes tunnels from older versions
            self.run("ip", "-n", ns, "link", "set", "wg-party", "mtu", str(pt.WG_MTU))
        text = pt.wg_conf(private_key, peers, listen_port)
        if text != self.wg_text:
            path = os.path.join(self.run_dir, f".wg-{secrets.token_hex(4)}.conf")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                self.nsrun("wg", "syncconf", "wg-party", path)
            finally:
                os.unlink(path)
            self.wg_text = text
        if address != self.wg_addr:
            self.run("ip", "-n", ns, "addr", "flush", "dev", "wg-party")
            self.run("ip", "-n", ns, "addr", "add", f"{address}/24", "dev", "wg-party")
            self.run("ip", "-n", ns, "link", "set", "wg-party", "up")
            self.wg_addr = address

    def wg_dump(self):
        r = self.nsrun("wg", "show", "wg-party", "dump", check=False)
        peers = {}
        for line in r.stdout.splitlines()[1:]:
            f = line.split("\t")
            if len(f) >= 7:
                peers[f[0]] = {"endpoint": None if f[2] == "(none)" else f[2], "handshake": int(f[4] or 0),
                               "rx": int(f[5] or 0), "tx": int(f[6] or 0)}
        return peers

    def ensure_vx(self, desired):
        """desired: {ifname: (vni, local ip, remote ip)}, VXLAN ports on br-retro, one per tunnel peer."""
        ns = self.ns()
        links = self.ns_links()
        for name, link in list(links.items()):
            if not name.startswith("vx-"):
                continue
            data = link.get("linkinfo", {}).get("info_data", {})
            want = desired.get(name)
            if not want or (data.get("id"), data.get("local"), data.get("remote")) != want:
                self.run("ip", "-n", ns, "link", "del", name, check=False)
                log.info("lan: removed tunnel port %s", name)
                links.pop(name)
        for name, (vni_, local, remote) in desired.items():
            if name in links:
                continue
            self.run("ip", "-n", ns, "link", "add", name, "type", "vxlan", "id", str(vni_), "local", local,
                     "remote", remote, "dstport", str(pt.VX_PORT), "df", "unset")
            self.run("ip", "-n", ns, "link", "set", name, "mtu", "1500")
            self.run("ip", "-n", ns, "link", "set", name, "master", BRIDGE)
            self.run("ip", "-n", ns, "link", "set", name, "up")
            log.info("lan: tunnel port %s up (VNI %d, %s -> %s)", name, vni_, local, remote)

    # host
    def ensure_server(self):
        if self.server and self.server.is_alive():
            return
        try:
            self.server = pt.ControlServer(self.ns(), pt.host_ip(self.cfg), self.handle_control)
            self.server.start()
        except OSError as e:
            log.warning("lan: party control port: %s", e)
            self.server = None

    def stop_server(self):
        if self.server:
            self.server.stop()
            self.server = None

    def host_reconcile(self):
        cfg, now = self.cfg, time.time()
        with self.lock:
            d = self.party.data
            if d.get("name") != box_name(cfg):          # renamed in the GUI: members see it with their next hello
                d["name"] = box_name(cfg)
                self.party.save()
            for mid, m in list(d["members"].items()):
                if m.get("kicked_at") and now - m["kicked_at"] > pt.KICK_GRACE_S:
                    d["members"].pop(mid)
                    d.setdefault("revoked", []).append(m["pub"])
                    d["revoked"] = d["revoked"][-50:]
                    self.member_rt.pop(mid, None)
                    self.party.save()
            if d.get("closing_at") and now - d["closing_at"] > pt.KICK_GRACE_S:
                self.party.data = pt.empty_state()
                self.party.save()
                self.note("Party closed")
                return None
            active = sorted(int(mid) for mid, m in d["members"].items()
                            if not m.get("kicked_at") and not d.get("closing_at"))
            self.ctrl_ids = sorted(int(mid) for mid in d["members"])
            members = sorted(d["members"].items(), key=lambda kv: int(kv[0]))
            if d.get("via") == "hub":
                # every member is reached through its relay port pair on the hub; no listening port needed
                peers = [{"pub": m["pub"], "allowed": f"{pt.member_ip(cfg, mid)}/32",
                          "endpoint": self.relay_endpoint(m["relay_port"], m.get("relay_host")), "keepalive": 15}
                         for mid, m in members if m.get("relay_port")]
                listen = None
            else:
                peers = [{"pub": d["join_pub"], "allowed": f"{pt.join_ip(cfg)}/32"}]
                peers += [{"pub": m["pub"], "allowed": f"{pt.member_ip(cfg, mid)}/32"} for mid, m in members]
                listen = cfg["lan"]["party_port"]
            priv = d["priv"]
        self.ensure_wg(priv, pt.host_ip(cfg), peers, listen)
        self.ensure_vx({pt.vx_name(m): (pt.vni(m), pt.host_ip(cfg), pt.member_ip(cfg, m)) for m in active})
        self.ensure_server()
        return active

    def handle_control(self, peer_ip, msg):
        """Runs in the control server's threads."""
        cfg = self.cfg
        with self.lock:
            d = self.party.data
            if d.get("role") != "host":
                return {"status": "closed"}
            if msg.get("proto") != pt.PROTO:
                return {"status": "rejected", "reason": "different NetKey version; update both boxes"}
            if peer_ip == pt.join_ip(cfg):
                return self.handle_join(d, msg)
            mid = next((k for k in d["members"] if pt.member_ip(cfg, k) == peer_ip), None)
            m = d["members"].get(mid) if mid else None
            if not m or m["pub"] != msg.get("pub"):
                return {"status": "unknown"}
            if m.get("kicked_at"):
                return {"status": "kicked"}
            if d.get("closing_at"):
                return {"status": "closed"}
            if msg.get("op") == "leave":
                d["members"].pop(mid)
                self.member_rt.pop(mid, None)
                self.party.save()
                log.info("lan: member %s (%s) left the party", mid, m["name"])
                return {"status": "left"}
            if msg.get("op") == "hello":
                name = pt.clean_name(msg.get("name")) if msg.get("name") else m["name"]
                if name != m["name"]:                   # the member renamed its box
                    m["name"] = name
                    self.party.save()
                rtt = msg.get("rtt")
                self.member_rt[mid] = {"rtt": rtt if isinstance(rtt, (int, float)) else None, "last": time.time()}
                return {"status": "ok", "party": self.party_view(d)}
            return {"status": "error", "error": "unknown op"}

    def handle_join(self, d, msg):
        pub, party_id = msg.get("pub"), msg.get("party")
        if msg.get("op") != "join" or party_id != d["party_id"]:
            return {"status": "rejected", "reason": "this invite code belongs to another party"}
        try:
            pt.key_bytes(str(pub))
        except ValueError:
            return {"status": "rejected", "reason": "bad key"}
        name = pt.clean_name(msg.get("name"))
        for mid, m in d["members"].items():
            if m["pub"] == pub:
                if m.get("kicked_at"):
                    return {"status": "rejected", "reason": "removed by the host"}
                return self.approved_reply(d, mid)
        if pub in d.get("revoked", []):
            return {"status": "rejected", "reason": "removed by the host"}
        if d.get("closing_at"):
            return {"status": "rejected", "reason": "the party is closing"}
        if len(d["members"]) + 1 >= int(self.cfg["lan"]["party_max"]):
            return {"status": "full"}
        now = time.time()
        if pub not in d["pending"]:
            if len(d["pending"]) >= pt.MAX_PENDING:
                return {"status": "rejected", "reason": "too many pending requests; try again later"}
            d["pending"][pub] = {"name": name, "since": now}
            log.info("lan: join request from %s", name)
        d["pending"][pub].update(last=now, name=name)
        if self.cfg["lan"]["party_auto_approve"]:
            mid = self.approve(d, pub)
            return self.approved_reply(d, mid)
        self.party.save()
        return {"status": "pending"}

    def approved_reply(self, d, mid):
        return {"status": "approved", "id": int(mid), "party_name": d["name"], "max": int(self.cfg["lan"]["party_max"])}

    def approve(self, d, pub):
        used = {int(k) for k in d["members"]}
        free = next((i for i in range(1, int(self.cfg["lan"]["party_max"])) if i not in used), None)
        if free is None:
            raise ValueError("the party is full")
        pt.key_bytes(pub)
        p = d["pending"].pop(pub)
        member = {"pub": pub, "name": p["name"], "joined": time.time()}
        if d.get("via") == "hub":
            try:
                r = self.hub_client().call("POST", "/v1/party/approve", {"box": p["box"], "member_id": free})
            except hc.HubError as e:
                d["pending"][pub] = p
                raise ValueError(f"Hub: {e}") from None
            member.update(box=p["box"], relay_port=int(r["relay_port"]), relay_host=relay_host(r.get("relay_host")),
                          relay_name=clean_relay(r.get("relay")))
        d["members"][str(free)] = member
        self.party.save()
        log.info("lan: %s joined as member %d", p["name"], free)
        return str(free)

    def party_view(self, d):
        return {"name": d["name"], "share_internet": bool(self.cfg["lan"]["share_internet"]),
                "members": [{"id": int(k), "name": m["name"]} for k, m in sorted(d["members"].items(),
                                                                                 key=lambda kv: int(kv[0]))
                            if not m.get("kicked_at")]}

    # member
    def resolve_endpoint(self, d, force=False):
        """The host's endpoint as ip:port. Names are resolved here, in the main namespace: `wg` would resolve them
        inside the party namespace, which has no network on purpose. Re-resolved every 5 minutes (dynamic DNS)."""
        host, port = d["endpoint"], d["port"]
        try:
            ipaddress.ip_address(host)
            return pt.endpoint_str(host, port)
        except ValueError:
            pass
        rt, now = self.host_rt, time.monotonic()
        cached = rt.get("resolved")
        if cached and not force and now - cached[1] < 300:
            return pt.endpoint_str(cached[0], port)
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
            infos.sort(key=lambda i: i[0] != socket.AF_INET)             # prefer IPv4
            ip = infos[0][4][0]
            if not cached or cached[0] != ip:
                log.info("lan: party host %s is %s", host, ip)
            rt["resolved"] = (ip, now)
            return pt.endpoint_str(ip, port)
        except (OSError, IndexError):
            if cached:
                return pt.endpoint_str(cached[0], port)
            raise RuntimeError(f"cannot resolve the party host {host}") from None

    def member_reconcile(self):
        cfg = self.cfg
        with self.lock:
            d = dict(self.party.data)
        if d.get("via") == "hub":
            if d["status"] != "approved":
                self.ensure_vx({})                 # waiting for the host's approval (an event from the hub)
                return
            mid = d["id"]
            host = {"pub": d["host_pub"], "allowed": f"{pt.host_ip(cfg)}/32",
                    "endpoint": self.relay_endpoint(d["relay_port"], d.get("relay_host")), "keepalive": 15}
            self.ensure_wg(d["priv"], pt.member_ip(cfg, mid), [host])
            self.ensure_vx({"vx-host": (pt.vni(mid), pt.member_ip(cfg, mid), pt.host_ip(cfg))})
            self.member_tick(d)
            return
        host = {"pub": d["host_pub"], "allowed": f"{pt.host_ip(cfg)}/32",
                "endpoint": self.resolve_endpoint(d), "keepalive": 15}
        if d["status"] == "joining":
            self.ensure_wg(d["join_priv"], pt.join_ip(cfg), [host])
            self.ensure_vx({})
        else:
            mid = d["id"]
            self.ensure_wg(d["priv"], pt.member_ip(cfg, mid), [host])
            self.ensure_vx({"vx-host": (pt.vni(mid), pt.member_ip(cfg, mid), pt.host_ip(cfg))})
        self.member_tick(d)

    def member_tick(self, d):
        rt, now = self.host_rt, time.monotonic()
        if now - rt.get("last_try", -99) < (3.0 if d["status"] == "joining" else pt.HELLO_S):
            return
        rt["last_try"] = now
        name = box_name(self.cfg)
        if d["status"] == "joining":
            msg = {"op": "join", "proto": pt.PROTO, "party": d["party_id"], "pub": d["pub"], "name": name}
        else:
            msg = {"op": "hello", "proto": pt.PROTO, "pub": d["pub"], "rtt": rt.get("rtt"), "name": name}
        try:
            reply = pt.request(self.ns(), pt.host_ip(self.cfg), msg)
        except OSError:
            rt.setdefault("unreachable_since", time.time())
            rt["error"] = "Cannot reach the host"
            if d.get("via") != "hub":
                self.refresh_endpoint(d)
            return
        rt.pop("unreachable_since", None)
        rt.update(error=None, rtt=reply.get("_rtt_ms"), last_ok=time.time())
        st = reply.get("status")
        if d["status"] == "joining":
            if st == "approved":
                with self.lock:
                    self.party.data.update(status="approved", id=int(reply["id"]), party_name=reply.get("party_name"),
                                           joined_at=time.time())
                    self.party.save()
                self.note(f"Joined {reply.get('party_name')}'s LAN party as member {reply['id']}", ok=True)
            elif st == "pending":
                rt["pending"] = True
            elif st == "full":
                self.leave_party("The party is full")
            elif st in ("rejected", "unknown", "closed", "error"):
                self.leave_party(f"Join refused: {reply.get('reason') or st}")
        elif st == "ok":
            rt["view"] = reply.get("party")
            host_name = (rt["view"] or {}).get("name")
            if host_name and host_name != d.get("party_name"):     # the host renamed its box
                with self.lock:
                    self.party.data["party_name"] = pt.clean_name(host_name)
                    self.party.save()
        elif st == "kicked":
            self.leave_party("You were removed from the party by the host")
        elif st == "closed":
            self.leave_party("The host closed the party")
        elif st == "unknown":
            self.leave_party("The host no longer knows this box")

    def refresh_endpoint(self, d):
        """DNS names and changed home IPs: re-set the host endpoint when the handshake is stale."""
        rt, now = self.host_rt, time.monotonic()
        if now - rt.get("endpoint_refresh", -999) < 60:
            return
        rt["endpoint_refresh"] = now
        hs = self.wg_dump().get(d["host_pub"], {}).get("handshake", 0)
        if not hs or time.time() - hs > 150:
            try:
                ep = self.resolve_endpoint(d, force=True)
            except RuntimeError:
                return
            self.nsrun("wg", "set", "wg-party", "peer", d["host_pub"], "endpoint", ep, check=False)

    def leave_party(self, message):
        with self.lock:
            self.party.data = pt.empty_state()
            self.party.save()
        self.host_rt = {}
        self.note(message, ok=False)
        log.info("lan: left the party: %s", message)

    def note(self, message, ok=True, cid=None, cmd="notice"):
        self.results.append({"id": cid, "cmd": cmd, "ok": ok, "message": message, "at": time.time()})

    # cloud hub
    def hub_client(self):
        lan = self.cfg["lan"]
        key = (lan["hub_url"], lan["hub_fingerprint"])
        if self.hub is None or self.hub_key != key:
            self.hub = hc.HubClient(lan["hub_url"], lan["hub_fingerprint"], self.state_dir, box_name(self.cfg),
                                    identity=ident.try_load(self.state_dir))
            self.hub_key = key
        if self.party.role == "standalone":
            self.hub.adopt_identity()       # an older box's token moves to the permanent box ID between parties
        return self.hub

    def hub_call(self, method, path, body=None):
        try:
            return self.hub_client().call(method, path, body)
        except hc.HubError as e:
            raise ValueError(f"Hub: {e}") from None

    def relay_endpoint(self, port, relay=None):
        """Where a pair's packets go: the relay server the hub chose, or the hub itself (its own relay)."""
        if relay:
            return f"{relay}:{int(port)}"
        host = urlsplit(self.cfg["lan"]["hub_url"]).hostname
        try:
            return f"{hc.resolve(host, self.dns_cache)}:{int(port)}"
        except OSError:
            raise RuntimeError(f"cannot resolve the hub {host}") from None

    def start_probe(self, force=False):
        """Measures the relays in the background (STUN round-trip times, reported to the hub)."""
        if self.prober and self.prober.is_alive():
            return
        if not force and self.relay_probe and time.time() - self.relay_probe["at"] < hc.PROBE_MAX_AGE_S:
            return
        if not self.cfg["lan"]["hub_fingerprint"]:
            return

        def done(result, error):
            if result:
                self.relay_probe, self.relay_probe_error = result, None
            else:
                self.relay_probe_error = error
        self.prober = hc.RelayProbe(self.hub_client(), self.dns_cache, done)
        self.prober.start()

    def probe_now(self, timeout=6):
        """Before creating a party: fresh times at the hub so it picks the relay nearest this box. Always measured
        again, even if the last times are recent: a relay may have been added since (a running probe is fresh too)."""
        self.start_probe(force=True)
        if self.prober and self.prober.is_alive():
            self.prober.join(timeout)

    # planned public events this box RSVP'd to with "join automatically" (public parties, phase 5)
    def watches(self, now=None):
        """{listing: {"starts", "title"}} still within their start window; older ones are dropped."""
        now = now or time.time()
        path = os.path.join(self.state_dir, WATCH_FILE)
        w = read_json(path) or {}
        keep = {k: v for k, v in w.items() if isinstance(v, dict) and float(v.get("starts", 0)) + WATCH_LATE_S > now}
        if keep != w:
            write_json_atomic(path, keep, mode=0o600)
        return keep

    def watching_now(self):
        now = time.time()
        return any(float(v["starts"]) - WATCH_EARLY_S <= now for v in self.watches(now).values())

    def public_auto(self, d):
        """Auto-approve while hosting a public party: the listing's own switch (host form), until the host card's
        switch (lan.party_auto_approve) is flipped; from then on that switch decides."""
        cfg_auto = bool(self.cfg["lan"]["party_auto_approve"])
        if d.get("cfg_auto_seen") is None:
            d["cfg_auto_seen"] = cfg_auto
        elif d["cfg_auto_seen"] != cfg_auto:
            d["cfg_auto_seen"], d["public_auto"] = cfg_auto, cfg_auto
        return cfg_auto or bool(d.get("public_auto"))

    def sync_public(self, share):
        """Tell the hub what this host really does (auto-approve, internet sharing), so joiners see it."""
        with self.lock:
            d = self.party.data
            if not (d.get("role") == "host" and d.get("via") == "hub" and d.get("public") and d.get("listing")):
                return
            want = {"auto": self.public_auto(d), "share": bool(share)}
            if d.get("synced") == want or time.time() < d.get("sync_retry", 0):
                return
            try:
                self.hub_client().call("POST", f"/v1/listing/{d['listing']}/settings", want)
                d["synced"] = want
                d.pop("sync_retry", None)
            except hc.HubError as e:
                log.warning("lan: could not update the public listing: %s", e)
                d["sync_retry"] = time.time() + 30
            self.party.save()

    def ensure_agent(self):
        """The long-poll event channel runs while this box is in a party through the hub, and while a planned
        event it wants to join automatically is about to start (to hear "listing_started")."""
        d = self.party.data
        want = d.get("via") == "hub" and d.get("role") in ("host", "member")
        if not want and d.get("role", "standalone") == "standalone" and self.cfg["lan"]["hub_fingerprint"]:
            want = self.watching_now()
        if want and (self.agent is None or not self.agent.is_alive()):
            self.agent = hc.HubAgent(self.hub_client(), int(d.get("hub_seq", 0)))
            self.agent.start()
        elif not want and self.agent:
            self.agent.stop()
            self.agent = None

    def process_hub_events(self):
        while self.agent:
            try:
                e = self.agent.events.get_nowait()
            except queue.Empty:
                return
            try:
                self.hub_event(e)
            except Exception:
                log.exception("lan: hub event %s", e.get("type"))

    def hub_event(self, e):
        t = e.get("type")
        with self.lock:
            d = self.party.data
            if t == "listing_started":
                lid = str(e.get("listing") or "")
                if self.party.role == "standalone" and lid in self.watches():
                    w = self.watches()
                    w.pop(lid)
                    write_json_atomic(os.path.join(self.state_dir, WATCH_FILE), w, mode=0o600)
                    try:
                        msg = self.command({"cmd": "join", "listing": lid})
                        self.note(f"{pt.clean_name(e.get('title')) or 'The event'} started: {msg}")
                    except (ValueError, RuntimeError, OSError) as err:
                        self.note(f"Could not join {pt.clean_name(e.get('title')) or 'the event'}: {err}", ok=False)
                return
            if d.get("via") != "hub" or e.get("party") != d.get("hub_party"):
                return                                    # an old event for a previous party
            d["hub_seq"] = max(int(d.get("hub_seq", 0)), int(e.get("seq", 0)))
            if d["role"] == "host":
                if t == "join_request":
                    pub, name, box = str(e.get("pub")), pt.clean_name(e.get("name")), str(e.get("box", ""))[:32]
                    try:
                        pt.key_bytes(pub)
                    except ValueError:
                        log.warning("lan: hub sent a join request with a bad key; ignored")
                        return
                    if len(d["pending"]) >= pt.MAX_PENDING and pub not in d["pending"]:
                        return
                    if pub in d.get("revoked", []) or any(m["pub"] == pub for m in d["members"].values()):
                        return
                    d["pending"][pub] = {"name": name, "since": time.time(), "box": box}
                    log.info("lan: join request from %s (via the hub)", name)
                    # auto-approve: the box setting, the public listing's own switch, or an RSVP the host pre-approved
                    auto = self.public_auto(d) if d.get("public") else self.cfg["lan"]["party_auto_approve"]
                    if auto or e.get("pre_approved"):
                        try:
                            self.note(f"{name} joined as member {self.approve(d, pub)}")
                        except ValueError as err:
                            self.note(str(err), ok=False)
                elif t == "relay_moved":
                    for mid, m in d["members"].items():
                        if m.get("box") and m.get("box") == e.get("box"):
                            port = int(e["relay_port"])
                            if not 1 <= port <= 65535:
                                raise ValueError("bad relay move from the hub")
                            m.update(relay_port=port, relay_host=relay_host(e.get("relay_host")),
                                     relay_name=clean_relay(e.get("relay")))
                            log.info("lan: %s now relayed via %s", m["name"], m["relay_name"] or "the hub")
                            d["relay_name"] = m["relay_name"]   # the hub moved the party to this relay too
                elif t == "left":
                    for mid, m in list(d["members"].items()):
                        if m.get("box") == e.get("box"):
                            d["members"].pop(mid)
                            self.member_rt.pop(mid, None)
                            self.note(f"{m['name']} left the party")
                    for pub, p in list(d["pending"].items()):
                        if p.get("box") == e.get("box"):
                            d["pending"].pop(pub)
                self.party.save()
            elif d["role"] == "member":
                if t == "approved":
                    if e.get("host_pub") != d["host_pub"]:
                        self.leave_party("The hub sent a different host key than the code promised; refused")
                        return
                    mid, port = int(e["member_id"]), int(e["relay_port"])
                    if not (1 <= mid <= pt.MAX_MEMBERS and 1 <= port <= 65535):
                        raise ValueError("bad approval from the hub")
                    d.update(status="approved", id=mid, relay_port=port, joined_at=time.time(),
                             relay_host=relay_host(e.get("relay_host")), relay_name=clean_relay(e.get("relay")),
                             party_name=pt.clean_name(e.get("party_name") or d.get("party_name")))
                    self.party.save()
                    self.host_rt = {}
                    self.note(f"Joined {d['party_name']}'s LAN party as member {d['id']}", ok=True)
                elif t == "rejected":
                    reason = "".join(ch for ch in str(e.get("reason") or "") if ch.isprintable())[:120]
                    self.leave_party(f"Join refused: {reason}" if reason else "Join refused by the host")
                elif t == "relay_moved":
                    port = int(e["relay_port"])
                    if not 1 <= port <= 65535:
                        raise ValueError("bad relay move from the hub")
                    d.update(relay_port=port, relay_host=relay_host(e.get("relay_host")),
                             relay_name=clean_relay(e.get("relay")))
                    self.party.save()
                    log.info("lan: the party is now relayed via %s", d["relay_name"] or "the hub")
                elif t == "kicked":
                    self.leave_party("You were removed from the party by the host")
                elif t == "closed":
                    self.leave_party("The host closed the party")
                else:
                    self.party.save()

    # commands (GUI / CLI)
    def process_commands(self):
        for c in pt.take_commands(self.state_dir):
            try:
                msg, ok = self.command(c), True
            except (ValueError, KeyError, RuntimeError, OSError, subprocess.CalledProcessError) as e:
                msg, ok = str(e) or e.__class__.__name__, False
            self.note(msg, ok, c.get("id"), c["cmd"])
            log.info("lan: party %s: %s", c["cmd"], msg)

    def command(self, c):
        if str(c.get("cmd", "")).startswith("wifi_"):
            return self.uplink.command(c)
        cmd, role = c["cmd"], self.party.role
        need = {"create": "standalone", "join": "standalone", "approve": "host", "reject": "host", "kick": "host",
                "regenerate": "host", "close": "host", "endpoint": "host", "leave": "member", "relays": None,
                "start": "standalone", "watch": None}
        if cmd not in need:
            raise ValueError(f"unknown command {cmd}")
        if cmd == "relays":
            self.start_probe(force=True)
            return "Measuring the distance to the relays"
        if cmd == "watch":
            lid = str(c.get("listing") or "")
            if not re.fullmatch(r"[0-9a-f]{12}", lid):
                raise ValueError("bad listing")
            with self.lock:
                w = self.watches()
                if c.get("on", True):
                    w[lid] = {"starts": float(c.get("starts") or 0), "title": pt.clean_name(c.get("title"))}
                else:
                    w.pop(lid, None)
                write_json_atomic(os.path.join(self.state_dir, WATCH_FILE), w, mode=0o600)
            return "This box joins automatically when the event starts" if c.get("on", True) else "Auto-join off"
        if role != need[cmd]:
            raise ValueError({"standalone": "Leave or close the current party first",
                              "host": "Only the party host can do that",
                              "member": "This box is not a party member"}[need[cmd]])
        name = box_name(self.cfg)
        with self.lock:
            d = self.party.data
            if cmd in ("create", "start") and (c.get("via") or "hub") == "hub":
                pin = str(self.cfg["lan"]["hub_relay"] or "").strip().lower()
                if not RELAY_RE.match(pin):
                    raise ValueError("lan.hub_relay is not a relay name")
                public = c.get("public") if cmd == "create" else None
                if public is not None and not isinstance(public, dict):
                    raise ValueError("bad public listing")
                self.probe_now()
                priv, pub = pt.wg_genkey()
                if cmd == "start":                       # a planned public event: its host presses Start
                    lid = str(c.get("listing") or "")
                    if not re.fullmatch(r"[0-9a-f]{12}", lid):
                        raise ValueError("bad listing")
                    r = self.hub_call("POST", f"/v1/listing/{lid}/start", {"host_pub": pub, "relay": pin})
                else:
                    body = {"host_pub": pub, "name": name, "relay": pin, "max": int(self.cfg["lan"]["party_max"])}
                    if public:
                        body["public"] = public
                    r = self.hub_call("POST", "/v1/party", body)
                self.party.data = {"role": "host", "via": "hub", "hub_party": r["party"], "code": r["code"],
                                   "party_id": 0, "name": name, "created": time.time(), "priv": priv, "pub": pub,
                                   "members": {}, "pending": {}, "revoked": [], "hub_seq": 0,
                                   "relay_name": clean_relay(r.get("relay")),
                                   "max": min(max(int(r.get("max") or self.cfg["lan"]["party_max"]), 2), 16)}
                if r.get("listing"):
                    self.party.data.update(public=True, listing=str(r["listing"])[:12],
                                           title="".join(ch for ch in str(r.get("title") or "") if ch.isprintable())[:40],
                                           event_code=str(r.get("event_code") or "")[:8], public_auto=bool(r.get("auto")))
                self.party.save()
                via = f" (relay {clean_relay(r.get('relay'))})" if r.get("relay") else ""
                if r.get("listing"):
                    return f"Public party started via the cloud hub{via}; it is listed under Parties"
                return f"Party created via the cloud hub{via}; share the code {r['code']}"
            if cmd == "start":
                raise ValueError("Planned events always run through the cloud hub")
            if cmd == "create" and c.get("public"):
                raise ValueError("A public party always runs through the cloud hub (a direct one would show your IP)")
            if cmd == "create":
                endpoint = (c.get("endpoint") or self.cfg["lan"]["party_endpoint"] or
                            (self.wan_info() or {}).get("ip") or "").strip()
                if not endpoint:
                    raise ValueError("No address for members to reach this box; set one")
                priv, pub = pt.wg_genkey()
                jpriv, jpub = pt.wg_genkey()
                self.party.data = {"role": "host", "party_id": secrets.randbits(31), "name": name,
                                   "created": time.time(), "priv": priv, "pub": pub, "join_priv": jpriv,
                                   "join_pub": jpub, "endpoint": endpoint, "port": int(self.cfg["lan"]["party_port"]),
                                   "members": {}, "pending": {}, "revoked": []}
                self.party.save()
                return f"Party created; members connect to {endpoint}:{self.cfg['lan']['party_port']}"
            if cmd == "join" and c.get("listing"):
                # public party from the directory: no code; the host key comes from the hub (the key check shows)
                priv, pub = pt.wg_genkey()
                r = self.hub_call("POST", "/v1/join", {"listing": str(c["listing"])[:16], "pub": pub, "name": name})
                pt.key_bytes(r["host_pub"])
                if r.get("status") != "pending":
                    raise ValueError("This box is already in that party")
                self.party.data = {"role": "member", "via": "hub", "status": "joining", "hub_party": r["party"],
                                   "party_name": pt.clean_name(r.get("party_name")), "host_pub": r["host_pub"],
                                   "party_id": 0, "priv": priv, "pub": pub, "name": name, "hub_seq": 0,
                                   "public": True, "listing": str(r.get("listing") or "")[:12]}
                self.party.save()
                self.host_rt = {}
                return (f"Asked {pt.clean_name(r.get('party_name'))} to let this box join; "
                        f"the host sees the key check {hc.verify_words(pub)}")
            if cmd == "join" and hc.is_hub_code(c.get("code", "")):
                code_id, fp = hc.parse_hub_code(c["code"])
                priv, pub = pt.wg_genkey()
                r = self.hub_call("POST", "/v1/join", {"code": c["code"], "pub": pub, "name": name})
                if hc.key_fingerprint(r["host_pub"]) != fp:
                    raise ValueError("The hub returned a host key that doesn't match the code's fingerprint; refused")
                if r.get("status") != "pending":
                    raise ValueError("This box is already in that party")
                self.party.data = {"role": "member", "via": "hub", "status": "joining", "hub_party": r["party"],
                                   "party_name": pt.clean_name(r.get("party_name")), "host_pub": r["host_pub"],
                                   "party_id": 0,
                                   "priv": priv, "pub": pub, "name": name, "hub_seq": 0}
                self.party.save()
                self.host_rt = {}
                return (f"Asked {pt.clean_name(r.get('party_name'))} to let this box join (via the cloud hub); "
                        f"the host should see the key check {hc.verify_words(pub)}")
            if cmd == "join":
                info = pt.decode_code(c.get("code", ""))
                priv, pub = pt.wg_genkey()
                self.party.data = dict(info, role="member", status="joining", priv=priv, pub=pub, name=name)
                self.party.save()
                self.host_rt = {}
                return f"Asking the host at {info['endpoint']} to let this box join"
            if cmd == "leave":
                if d.get("via") == "hub":
                    try:
                        self.hub_client().call("POST", "/v1/leave", {})
                    except hc.HubError as e:
                        log.warning("lan: hub leave: %s", e)
                if d.get("status") == "approved":
                    try:
                        pt.request(self.ns(), pt.host_ip(self.cfg), {"op": "leave", "proto": pt.PROTO,
                                                                     "pub": d["pub"]}, timeout=3)
                    except OSError:
                        pass
                self.party.data = pt.empty_state()
                self.party.save()
                self.host_rt = {}
                return "Left the party"
            target = str(c.get("member", "")).strip()
            if cmd in ("approve", "reject"):
                pub = next((p for p, v in d["pending"].items() if target in (p, v["name"])), None)
                if target == "all" and cmd == "approve":
                    names = [d["pending"][p]["name"] for p in list(d["pending"])]
                    for p in list(d["pending"]):
                        self.approve(d, p)
                    return f"Approved {', '.join(names) or 'nobody (no pending requests)'}"
                if not pub:
                    raise ValueError(f"No pending join request from {target}")
                if cmd == "approve":
                    mid = self.approve(d, pub)
                    return f"{d['members'][mid]['name']} joined as member {mid}"
                p = d["pending"].pop(pub)
                d.setdefault("revoked", []).append(pub)
                self.party.save()
                if d.get("via") == "hub":
                    self.hub_call("POST", "/v1/party/reject", {"box": p["box"]})
                return f"Rejected {p['name']}"
            if cmd == "kick":
                mid = next((k for k, m in d["members"].items() if target in (k, m["name"])), None)
                if not mid:
                    raise ValueError(f"No member {target}")
                d["members"][mid]["kicked_at"] = time.time()
                self.party.save()
                if d.get("via") == "hub" and d["members"][mid].get("box"):
                    try:                                 # the host already enforces it; this only informs the hub
                        self.hub_client().call("POST", "/v1/party/kick", {"box": d["members"][mid]["box"]})
                    except hc.HubError as e:
                        log.warning("lan: hub kick: %s", e)
                return f"Removed {d['members'][mid]['name']} (member {mid})"
            if cmd == "regenerate" and d.get("via") == "hub":
                d["code"] = self.hub_call("POST", "/v1/party/regenerate", {})["code"]
                self.party.save()
                return "New invite code created; the old code no longer works"
            if cmd == "regenerate":
                d["join_priv"], d["join_pub"] = pt.wg_genkey()
                self.party.save()
                return "New invite code created; the old code no longer works"
            if cmd == "endpoint" and d.get("via") == "hub":
                raise ValueError("This party runs through the cloud hub; members need no address")
            if cmd == "endpoint":
                ep = (str(c.get("endpoint", "")).strip() or self.cfg["lan"]["party_endpoint"] or
                      (self.wan_info() or {}).get("ip") or "")
                if not ep or len(ep) > 253 or any(ch.isspace() for ch in ep):
                    raise ValueError("Give a host name or IP address")
                d["endpoint"] = ep
                self.party.save()
                return f"Members now connect to {ep}; share the new invite code"
            if cmd == "close":
                d["closing_at"] = time.time()
                self.party.save()
                if d.get("via") == "hub":
                    try:
                        self.hub_client().call("POST", "/v1/party/close", {})
                    except hc.HubError as e:
                        log.warning("lan: hub close: %s", e)
                return "Closing the party; members are told within a few seconds"
        raise ValueError(f"unknown command {cmd}")

    # teardown
    def ensure_stats(self):
        """Protocol counters (protostats.py): loaded once per namespace, never reloaded, so counts survive."""
        if self.stats_since is not None:
            return
        if self.nsrun("nft", "list", "table", "bridge", ps.TABLE, check=False).returncode != 0:
            self.nsrun("nft", "-f", "-", input_text=ps.ruleset())
        self.stats_since = time.time()

    def proto_counters(self):
        if self.stats_since is None:
            return None
        r = self.nsrun("nft", "-j", "list", "counters", "table", "bridge", ps.TABLE, check=False)
        return ps.parse_counters(r.stdout) if r.returncode == 0 else None

    # retro port while the LAN party port is off
    def remember_retro_port(self):
        """Only a NIC that passed the leak probe (so it isn't cabled into the home network) is ever kept off."""
        mac = (read_sys(os.path.join(self.sys_net, self.nic, "address")) or "").lower() if self.nic else ""
        if not mac:                         # the NIC is inside the namespace: ask there
            r = self.nsrun("ip", "-j", "link", "show", self.nic, check=False) if self.nic else None
            try:
                mac = (json.loads(r.stdout)[0].get("address") or "").lower() if r else ""
            except (ValueError, IndexError):
                mac = ""
        path = os.path.join(self.state_dir, RETRO_PORT_FILE)
        if mac and (read_json(path, {}) or {}).get("mac") != mac:
            write_json_atomic(path, {"mac": mac, "name": self.nic, "since": time.time()}, mode=0o640)

    def park_extra_nics(self, keep, park=True):
        """Wired adapters beyond the retro port (keep): down and away from NetworkManager. Otherwise NetworkManager
        runs DHCP on them in the Pi's own network, and one cabled into the retro switch gets a lease from the party's
        dnsmasq: retro PCs could then reach the web GUI and ssh, and the Pi could pick up a route via the party.
        One that becomes the uplink or management port is handed back. park=False: only that hand-back."""
        path = os.path.join(self.state_dir, EXTRA_PORTS_FILE)
        pinned = set((read_json(path, {}) or {}).get("macs", []))
        before = set(pinned)
        exclude = self.default_ifaces() | self.uplink.exclude()
        for i in self.all_ifaces():                   # handed back: it carries the uplink or the default route now
            if i["mac"] in pinned and i["name"] in exclude:
                pinned.discard(i["mac"])
                self.unpin_nm(i["mac"])
                self.run("ip", "link", "set", i["name"], "up", check=False)
                log.info("lan: %s is the uplink now; no longer switched off", i["name"])
        extra = {}
        if park:
            builtin = {self.uplink.eth} if self.uplink.eth else set()   # the built-in port: the uplink's business
            extra = {c["name"]: c["mac"] for c in candidate_nics(self.sys_net, exclude | builtin) if c["name"] != keep}
        for name, mac in extra.items():
            flags = read_sys(os.path.join(self.sys_net, name, "flags"), "0x0")
            if name in self.extra and not int(flags, 16) & 0x1:
                continue                              # already off
            if os.path.exists(os.path.join(self.sys_net, name, "device")):
                self.pin_nm(mac)
            if shutil.which("nmcli"):
                self.run("nmcli", "device", "set", name, "managed", "no", check=False)
            self.run("ip", "addr", "flush", "dev", name, check=False)
            self.run("ip", "link", "set", name, "down", check=False)
            if name not in self.extra:
                log.warning("lan: extra Ethernet adapter %s (%s) switched off; only one retro-LAN port is used "
                            "(%s). For more PCs, use a switch on the retro port", name, mac, keep or "none yet")
            pinned.add(mac)
        if park:
            self.extra = extra
        if pinned != before:
            write_json_atomic(path, {"macs": sorted(pinned)}, mode=0o640)

    def park_retro_port(self):
        """LAN party port off: keep the last retro NIC down and away from NetworkManager, so retro PCs on it can't
        reach the Pi or (via IP forwarding) the home network. Never touches the uplink or the management port."""
        mac = (read_json(os.path.join(self.state_dir, RETRO_PORT_FILE), {}) or {}).get("mac")
        name = next((i["name"] for i in self.all_ifaces() if mac and i["mac"] == mac), None)
        if not name:
            return
        if name in self.uplink.exclude() or name in self.default_ifaces():
            if name in self.parked:          # it became the uplink or management port (e.g. uplink set to Ethernet)
                self.parked.discard(name)
                self.unpin_nm(mac)
                self.run("ip", "link", "set", name, "up", check=False)
                log.info("lan: %s is the uplink now; no longer kept off", name)
            return
        flags = read_sys(os.path.join(self.sys_net, name, "flags"), "0x0")
        if name in self.parked and not int(flags, 16) & 0x1:
            return
        if os.path.exists(os.path.join(self.sys_net, name, "device")):
            self.pin_nm(mac)
        if shutil.which("nmcli"):
            self.run("nmcli", "device", "set", name, "managed", "no", check=False)
        self.run("ip", "addr", "flush", "dev", name, check=False)
        self.run("ip", "link", "set", name, "down", check=False)
        if name not in self.parked:
            log.info("lan: LAN party port is off; retro port %s switched off", name)
        self.parked.add(name)

    def watch_auto_port(self):
        """LAN party port off, Wi-Fi uplink: the built-in port stays in quarantine (isolated, no address) and keeps
        deciding: cabled into the home network -> management port (rescue access when the Wi-Fi is down); cabled
        to retro PCs -> stays isolated, never joined to a retro LAN. False when there is no such port."""
        port = self.uplink.auto_port()
        if not port:
            return False
        self.uplink.port_tick(self.ns_links())
        if self.uplink.mgmt == port:                    # NetworkManager runs it as management port
            if self.ns_exists():
                self.teardown()
            return True
        try:
            self.ensure_layout("quarantine", False)
            links = self.ns_links()
            mac = next((i["mac"] for i in self.all_ifaces() if i["name"] == port), None) or \
                (links.get(port, {}).get("address") or "").lower()
            if port not in links and not os.path.exists(os.path.join(self.sys_net, port)):
                return True                             # unplugged adapter: nothing to watch
            self.nic_ok = False
            self.adopt_nic(port, mac, links)
            self.quarantine_tick(self.ns_links())
        except RuntimeError as e:
            log.error("lan: %s", e)
            self.built = None
        return True

    def quarantine_tick(self, links):
        carrier = "LOWER_UP" in links.get(self.nic, {}).get("flags", [])
        now = time.monotonic()
        if not carrier:
            self.last_probe = 0.0
            return
        if now - self.last_probe < (1.5 if not self.last_probe else 60):
            return
        self.last_probe = now
        if not self.uplink.decide(self.nic, self.probe(PROBE_BRIDGE)):
            self.move_nic(PROBE_BRIDGE)                 # flush what it learned; stays isolated

    def teardown(self, release_nm=False):
        self.stats_since = None
        self.stop_fileshare()
        self.stop_server()
        self.stop_dnsmasq()
        if self.ns_exists():
            for link in self.ns_links().values():
                if self.is_retro_candidate(link):
                    name = link["ifname"]
                    self.run("ip", "-n", self.ns(), "link", "set", name, "nomaster", check=False)
                    self.run("ip", "-n", self.ns(), "link", "set", name, "netns", str(os.getpid()), check=False)
                    log.info("lan: %s returned to the main namespace", name)
            self.run("ip", "netns", "del", self.ns(), check=False)
        self.run("ip", "link", "del", VETH_MAIN, check=False)
        self.run("ip", "link", "del", "wg-party", check=False)
        self.run("nft", "delete", "table", "inet", "netkey_lan", check=False)
        if release_nm and os.path.exists(NM_CONF):
            os.unlink(NM_CONF)
            if shutil.which("nmcli"):
                self.run("nmcli", "general", "reload", "conf", check=False)
        self.built = self.rules_sig = self.wg_text = self.wg_addr = None
        self.nic, self.nic_ok, self.leak = None, False, None

    # status for the GUI
    def party_status(self, links):
        with self.lock:
            d = json.loads(json.dumps(self.party.data))
        role = d.get("role", "standalone")
        via = d.get("via", "direct") if role != "standalone" else None
        out = {"role": role, "via": via, "results": list(self.results),
               "watches": self.watches() if role == "standalone" else {}}
        if via == "hub":
            out.update(hub_party=d.get("hub_party"), public=bool(d.get("public")), listing=d.get("listing"),
                       title=d.get("title"), event_code=d.get("event_code"))
            if role == "host" and d.get("public"):
                out["auto_approve"] = bool(self.cfg["lan"]["party_auto_approve"]) or bool(d.get("public_auto"))
            a = self.agent
            out["hub"] = {"url": self.cfg["lan"]["hub_url"], "online": bool(a and a.online),
                          "error": a.error if a else None, "last_ok": a.last_ok if a else None}
        out["relay_probe"] = dict(self.relay_probe or {}, error=self.relay_probe_error,
                                  running=bool(self.prober and self.prober.is_alive()))
        cfg, now = self.cfg, time.time()
        dump = self.wg_dump() if "wg-party" in links else {}
        if role == "host":
            members = []
            for mid, m in sorted(d["members"].items(), key=lambda kv: int(kv[0])):
                w, rt = dump.get(m["pub"], {}), self.member_rt.get(mid, {})
                hs = w.get("handshake", 0)
                st = links.get(pt.vx_name(mid), {}).get("stats64", {})
                members.append({"id": int(mid), "name": m["name"], "joined": m.get("joined"),
                                "online": bool(hs and now - hs < pt.ONLINE_S) and not m.get("kicked_at"),
                                "handshake_age": int(now - hs) if hs else None, "endpoint": w.get("endpoint"),
                                "rtt_ms": rt.get("rtt"), "last_hello": rt.get("last"),
                                "connection": "relay" if via == "hub" else "direct",
                                "relay": m.get("relay_name") if via == "hub" else None,
                                "kicked": bool(m.get("kicked_at")), "verify": hc.verify_words(m["pub"]),
                                "rx_bytes": st.get("rx", {}).get("bytes", 0),
                                "tx_bytes": st.get("tx", {}).get("bytes", 0)})
            out.update(party_id=d["party_id"], name=d["name"], endpoint=d.get("endpoint"), port=d.get("port"),
                       relay=d.get("relay_name"),
                       max=int(d.get("max") or cfg["lan"]["party_max"]), members=members, closing=bool(d.get("closing_at")),
                       pending=[{"pub": p, "name": v["name"], "since": v["since"], "verify": hc.verify_words(p)}
                                for p, v in d["pending"].items()],
                       code=d["code"] if via == "hub" else
                       pt.encode_code(d["party_id"], d["endpoint"], d["port"], d["pub"], d["join_priv"]))
        elif role == "member":
            hs = dump.get(d["host_pub"], {}).get("handshake", 0)
            rt = self.host_rt
            waiting = d["status"] == "joining" and (rt.get("pending") or via == "hub")
            out.update(status="pending" if waiting else d["status"],
                       party_id=d["party_id"], host_endpoint="via the cloud hub" if via == "hub" else
                       pt.endpoint_str(d["endpoint"], d["port"]),
                       connection="relay" if via == "hub" else "direct",
                       relay=d.get("relay_name") if via == "hub" else None,
                       member_id=d.get("id"), party_name=d.get("party_name"), error=rt.get("error"),
                       unreachable_since=rt.get("unreachable_since"), rtt_ms=rt.get("rtt"),
                       handshake_age=int(now - hs) if hs else None, view=rt.get("view"),
                       verify=hc.verify_words(d["pub"]) if d.get("pub") else None)
        return out

    def write_status(self, state, detail="", share=False, links=None):
        a = addresses(self.cfg)
        links = links if links is not None else {}
        nic_info = None
        if self.nic and self.nic in links:
            link = links[self.nic]
            r = self.nsrun("cat", f"/sys/class/net/{self.nic}/speed", check=False)
            speed = int(r.stdout) if r.stdout.strip().lstrip("-").isdigit() and int(r.stdout) > 0 else None
            nic_info = {"name": self.nic, "mac": (link.get("address") or "").lower(),
                        "carrier": "LOWER_UP" in link.get("flags", []), "speed": speed, "duplex": None,
                        "quarantined": not self.nic_ok, "driver": self.ns_nic_driver(self.nic)}
        br = links.get(BRIDGE, {}).get("stats64", {})
        # the retro port's own counters: the bridge device only counts frames addressed to the Pi itself
        io = (links.get(self.nic) or {}).get("stats64", {}) if nic_info else br
        role = self.party.role
        leases = parse_leases(read_sys(os.path.join(self.run_dir, LEASE_FILE), "")) \
            if state == "up" and role != "member" else []
        party = self.party_status(links) if links else {"role": role, "results": list(self.results)}
        self.status = {
            "state": state, "detail": detail, "nic": nic_info, "share_internet": share, "role": role,
            "enabled": bool(self.cfg["lan"]["enabled"]), "subnet": a["net"], "gateway": a["gateway"],
            "dhcp_range": [a["dhcp_first"], a["dhcp_last"]], "leases": leases,
            "rx_bytes": io.get("rx", {}).get("bytes", 0), "tx_bytes": io.get("tx", {}).get("bytes", 0),
            "party": {k: v for k, v in party.items() if k != "code"},
            "net": self.uplink.status, "extra_nics": sorted(self.extra),
            "ip_conflicts": self.conflicts if state == "up" else [],
            "static_pcs": self.static_pcs if state == "up" and role != "member" else [],
            "fileshare": self.share_status if state in ("up", "error") else dict(
                self.share_status, active=False, paused=detail or "The retro-LAN port is not active"),
            "proto": self.proto_counters() if state == "up" else None, "proto_since": self.stats_since,
            "updated": time.time(), "pid": os.getpid(),
        }
        os.makedirs(self.run_dir, exist_ok=True)
        os.chmod(self.run_dir, 0o755)
        write_json_atomic(os.path.join(self.run_dir, STATUS_FILE), self.status, mode=0o644)
        party["updated"] = time.time()
        path = os.path.join(self.run_dir, pt.PARTY_FILE)
        write_json_atomic(path, party, mode=0o640)       # contains the invite code: root + group netkey only
        net = os.path.join(self.run_dir, NET_FILE)
        write_json_atomic(net, dict(self.uplink.private_status(), updated=time.time()), mode=0o640)  # hotspot pw
        try:
            import grp
            gid = grp.getgrnam("netkey").gr_gid
            os.chown(path, 0, gid)
            os.chown(net, 0, gid)
        except (KeyError, PermissionError):
            pass

    # main loop
    def step(self):
        self.load_cfg()
        try:
            self.uplink.tick()
        except Exception:
            log.exception("lan: uplink")
        self.process_commands()
        try:
            self.ensure_agent()
            if self.party.data.get("via") == "hub" and self.party.role in ("host", "member"):
                self.start_probe()                 # every 10 min: keeps the hub's relay choice current
        except (ValueError, OSError) as e:
            log.warning("lan: hub: %s", e)
        self.process_hub_events()
        lan = self.cfg["lan"]
        share = bool(lan["share_internet"]) if self.share_override is None else self.share_override
        role = self.party.role
        self.sync_public(share)
        if not lan["enabled"]:
            if self.built and self.built[0] != "quarantine":
                log.info("lan: disabled; tearing down")
                self.teardown()
            if not self.watch_auto_port() and (self.built or self.ns_exists()):
                self.teardown()
            self.park_retro_port()                 # a USB retro adapter (the built-in port is handled above)
            self.park_extra_nics(None, park=False)
            self.write_status("disabled", "LAN party port is switched off", share)
            return
        self.parked.clear()                        # adopt_nic takes the port from here
        configured = (self.override_iface if self.override_iface is not None else lan["interface"]).strip().lower()
        self.uplink.port_tick(self.ns_links())
        name, mac, err = self.find_nic(configured, self.ns_links())
        self.park_extra_nics(name, park=configured != "none")
        if err:
            if err != self.last_error:
                log.warning("lan: %s", err)
            self.last_error = err
            if role == "standalone":
                self.write_status("error", err, share, self.ns_links())
                return
            name = mac = None                      # a party keeps running without a retro port
        else:
            self.last_error = None
        if name is None and role == "standalone":
            if self.built:
                self.teardown()
            self.write_status("no_nic", "Plug in a USB Ethernet adapter for the retro LAN", share)
            return
        try:
            self.ensure_layout(role, share)
            self.ensure_stats()
            member_ids = []
            if role == "host":
                member_ids = self.host_reconcile()
            elif role == "member":
                self.member_reconcile()
            if member_ids is None or self.party.role != role:
                return                                         # role changed: rebuild on the next step
            if frozenset(member_ids) - self.last_members:
                self.schedule_garp()                           # a member's PCs still know their own box as gateway
            self.last_members = frozenset(member_ids)
            self.ensure_rules(role, share, member_ids, self.ctrl_ids if role == "host" else None)
            if role != "member":
                self.ensure_dnsmasq(share)
            links = self.ns_links()
            if name:
                self.adopt_nic(name, mac, links)
                links = self.ns_links()
                self.nic_tick(links)
                links = self.ns_links()
            if not name:
                self.share_paused("No retro-LAN port: plug in a USB Ethernet adapter")
            elif self.leak:
                self.share_paused("The retro-LAN port is quarantined (it is cabled into your home network)")
            else:
                self.ensure_fileshare(role)
            if name and not self.leak and role != "member":
                self.garp_tick(role)
                self.scan_tick(role)
            else:
                self.conflicts = []
            if self.leak:
                self.write_status("error", self.leak, share, links)
            else:
                self.write_status("up", "" if name else (self.last_error or "No retro-LAN port: party traffic only"),
                                  share, links)
        except RuntimeError as e:
            log.error("lan: %s", e)
            self.write_status("error", str(e), share, self.ns_links())
            self.built = None

    def loop(self):
        log.info("lan: starting (namespace %s, subnet %s, party role %s)", self.ns(), self.cfg["lan"]["subnet"],
                 self.party.role)
        while not self.stopping:
            try:
                self.step()
            except Exception:
                log.exception("lan: reconcile failed")
            deadline = time.monotonic() + LOOP_S
            while not self.stopping and time.monotonic() < deadline:
                time.sleep(0.2)
        if self.agent:
            self.agent.stop()
        self.teardown()
        self.write_status("stopped", "LAN service stopped")
        log.info("lan: stopped")


def run(base_cfg, state_dir, interface=None, once=False, share=None, netns=None, run_dir=RUN_DIR):
    d = LanDaemon(base_cfg, state_dir, interface, run_dir=run_dir, share=share, netns=netns)
    signal.signal(signal.SIGTERM, d.stop)
    signal.signal(signal.SIGINT, d.stop)
    if once:
        d.step()
        return 0
    d.loop()
    return 0


def teardown(base_cfg, state_dir, release_nm=False, netns=None, run_dir=RUN_DIR):
    """Safety net after the service stops (crash or kill): NIC back to the main namespace, rules removed."""
    d = LanDaemon(base_cfg, state_dir, run_dir=run_dir, netns=netns)
    d.load_cfg()
    d.teardown(release_nm=release_nm)
    return 0


def read_status(run_dir=RUN_DIR, now=None):
    """For the web GUI (netkey user): None when the LAN service isn't running."""
    s = read_json(os.path.join(run_dir, STATUS_FILE))
    if not s:
        return None
    s["stale"] = (now or time.time()) - s.get("updated", 0) > 15
    return s


def read_net(run_dir=RUN_DIR):
    """Uplink status incl. the hotspot password (root and the netkey group)."""
    return read_json(os.path.join(run_dir, NET_FILE))


def read_party(run_dir=RUN_DIR):
    """Full party status including the invite code (readable by root and the netkey group)."""
    return read_json(os.path.join(run_dir, pt.PARTY_FILE))


__all__ = ["addresses", "candidate_nics", "choose_nic", "main_ruleset", "ns_ruleset", "dnsmasq_cmd",
           "parse_leases", "nm_conf", "LanDaemon", "run", "teardown", "read_status", "read_party"]
