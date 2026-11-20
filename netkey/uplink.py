"""Uplink management: Wi-Fi as the upstream, the built-in Ethernet port as an automatic port, and a rescue hotspot.

network.uplink = "ethernet": the built-in Ethernet port is the uplink (as before); a USB adapter is the retro port.
network.uplink = "wifi":     Wi-Fi is the uplink and the built-in Ethernet port decides by itself what it is:
  - cabled into the home network (a DHCP server answers, or the leak probe sees this Pi's own traffic):
    a management port, handed to NetworkManager (DHCP address, GUI, SSH). Never connected to the party.
  - cabled to retro PCs (nobody answers DHCP): the retro-LAN port, as with a USB adapter.
  Every link drop sends the port back to quarantine, so moving the cable re-decides.
When the Wi-Fi is up, a DHCP answer only counts if it matches the remembered home network (gateway and subnet),
so a retro PC running a DHCP server stays on the retro side. With no uplink at all, any DHCP answer counts.

Rescue hotspot (network.hotspot, any box with Wi-Fi): after HOTSPOT_AFTER_S without any uplink, the Pi opens its
own WPA2 network (NetKey-XXXX, password HOTSPOT_PASSWORD on every box) with the GUI at http://192.168.4.1. Without clients it
retries the real uplink every HOTSPOT_RETRY_S.
"""
import ipaddress
import json
import logging
import os
import secrets
import select
import shutil
import socket
import struct
import subprocess
import sys
import time

from .store import read_json, write_json_atomic

log = logging.getLogger("netkey")

STATE_FILE = "net-state.json"           # state_dir, root 0600: home fingerprint, hotspot password, Wi-Fi country
NET_FILE = "net.json"                   # run dir, 0640 root:netkey: full status incl. the hotspot password
WIFI_PROFILE = "netkey-wifi"             # saved networks are netkey-wifi-1, -2, ... (this bare name: before 0.10.4)
WIFI_TRIAL = WIFI_PROFILE + "-new"       # a network being joined, until it works
MAX_KNOWN = 10                           # saved Wi-Fi networks; the least recently used goes when an 11th is added
PRIO_PREFERRED, PRIO_KNOWN = 20, 10      # NetworkManager joins the preferred network, else any saved one in range
HOTSPOT_PROFILE = "netkey-hotspot"
HOTSPOT_ADDR = "192.168.4.1/24"
HOTSPOT_PASSWORD = "netkeyrescue"       # same on every box (documented); the hotspot only serves the rescue GUI
HOTSPOT_AFTER_S = 300                   # no uplink this long -> hotspot (60 s when no Wi-Fi is configured yet)
HOTSPOT_AFTER_UNCONFIGURED_S = 60
HOTSPOT_RETRY_S = 300                   # hotspot without clients: try the real uplink again this often
RETRY_WINDOW_S = 60                     # how long a retry waits for the uplink before the hotspot returns
MGMT_CARRIER_LOSS_S = 3                 # management port without link this long goes back to quarantine
MGMT_NO_IP_S = 90                       # management port with link but no address this long: decide again
                                        # (catches a cable moved faster than the carrier poll)
WIFI_POLL_S = 5
BUILTIN_DRIVERS = ("smsc95xx", "lan78xx", "bcmgenet", "macb")   # Pi 1-3, 3B+, 4, 5 on-board Ethernet
DHCP_PROBE_S = 3.0
ETH_P_ALL = 0x0003
PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# DHCP probe (pure parts are unit-tested)
def _csum(data):
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def build_discover(mac, xid):
    """Broadcast DHCPDISCOVER Ethernet frame from `mac` (6 bytes)."""
    bootp = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 1, 1, 6, 0, xid, 0, 0x8000, b"\0" * 4, b"\0" * 4,
                        b"\0" * 4, b"\0" * 4, mac + b"\0" * 10, b"", b"")
    opts = b"\x63\x82\x53\x63" + bytes([53, 1, 1, 55, 4, 1, 3, 6, 54, 255])
    payload = (bootp + opts).ljust(300, b"\0")
    udp = struct.pack("!HHHH", 68, 67, 8 + len(payload), 0) + payload          # UDP checksum 0 = none (IPv4)
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0x10, 20 + len(udp), 0, 0, 64, 17, 0, b"\0" * 4, b"\xff" * 4)
    ip = ip[:10] + struct.pack("!H", _csum(ip)) + ip[12:]
    return b"\xff" * 6 + mac + b"\x08\x00" + ip + udp


def parse_offer(frame, xid):
    """dict(server_mac, server, router, subnet, offered) for a DHCPOFFER answering `xid`, else None."""
    try:
        if len(frame) < 14 + 20 + 8 + 240 or frame[12:14] != b"\x08\x00":
            return None
        ihl = (frame[14] & 0x0F) * 4
        if frame[14 + 9] != 17:
            return None
        udp = 14 + ihl
        sport, dport = struct.unpack("!HH", frame[udp:udp + 4])
        if (sport, dport) != (67, 68):
            return None
        b = frame[udp + 8:]
        if b[0] != 2 or struct.unpack("!I", b[4:8])[0] != xid or b[236:240] != b"\x63\x82\x53\x63":
            return None
        opts, i = {}, 240
        while i < len(b) and b[i] != 255:
            if b[i] == 0:
                i += 1
                continue
            code, n = b[i], b[i + 1]
            opts[code] = b[i + 2:i + 2 + n]
            i += 2 + n
        if opts.get(53) != b"\x02":
            return None
        ip4 = lambda raw: str(ipaddress.IPv4Address(raw[:4])) if raw and len(raw) >= 4 else None  # noqa: E731
        offered, mask = ip4(b[16:20]), ip4(opts.get(1))
        subnet = str(ipaddress.IPv4Network(f"{offered}/{mask}", strict=False)) if offered and mask else None
        return {"server_mac": frame[6:12].hex(":"), "server": ip4(opts.get(54)), "router": ip4(opts.get(3)),
                "subnet": subnet, "offered": offered}
    except (IndexError, struct.error, ValueError):
        return None


def dhcp_probe(ifname, timeout=DHCP_PROBE_S):
    """Send one DHCPDISCOVER out of `ifname` (may be a bridge port) and return the first offer, or None.
    ETH_P_ALL: on a bridge port, protocol-specific packet sockets never see the frames (the bridge takes them)."""
    with open(f"/sys/class/net/{ifname}/address") as f:
        mac = bytes.fromhex(f.read().strip().replace(":", ""))
    xid = secrets.randbits(32)
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    try:
        s.bind((ifname, 0))
        s.send(build_discover(mac, xid))
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            if not select.select([s], [], [], left)[0]:
                break
            offer = parse_offer(s.recv(4096), xid)
            if offer:
                return offer
        return None
    finally:
        s.close()


def home_match(offer, home):
    """The offer comes from the remembered home network (same gateway and subnet)."""
    if not offer or not home:
        return False
    return bool(offer.get("router")) and offer["router"] == home.get("gateway") and \
        offer.get("subnet") == home.get("subnet")


def builtin_ethernet(sys_net="/sys/class/net"):
    """Name of the board's own Ethernet port: by driver, else eth0."""
    try:
        names = sorted(os.listdir(sys_net))
    except OSError:
        return None
    for name in names:
        drv = os.path.basename(os.path.realpath(os.path.join(sys_net, name, "device", "driver")))
        if drv in BUILTIN_DRIVERS:
            return name
    return "eth0" if "eth0" in names else None


def wifi_iface(sys_net="/sys/class/net"):
    try:
        names = sorted(os.listdir(sys_net))
    except OSError:
        return None
    for name in names:
        if os.path.exists(os.path.join(sys_net, name, "wireless")) or \
                os.path.exists(os.path.join(sys_net, name, "phy80211")):
            return name
    return None


def nm_split(line):
    """nmcli -t fields: ':' separated, with '\\:' escaped."""
    out, cur, i = [], "", 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if line[i] == ":":
            out.append(cur)
            cur = ""
        else:
            cur += line[i]
        i += 1
    out.append(cur)
    return out


def is_known_name(name):
    """A saved Wi-Fi network of this box (not the hotspot, not a network still being joined). Besides the box's own
    netkey-wifi-<n>, this is any other saved one, such as the network Raspberry Pi Imager set up ("preconfigured",
    "netplan-wlan0-...")."""
    return bool(name) and name not in (HOTSPOT_PROFILE, WIFI_TRIAL)


def parse_scan(text):
    best = {}
    for line in text.splitlines():
        f = nm_split(line)
        if len(f) < 5 or not f[1]:
            continue
        inuse, ssid, signal, security, freq = f[:5]
        sig = int(signal) if signal.isdigit() else 0
        if ssid not in best or sig > best[ssid]["signal"]:
            best[ssid] = {"ssid": ssid, "signal": sig, "secure": bool(security and security != "--"),
                          "security": security if security != "--" else "", "band": "5 GHz" if freq.startswith("5")
                          else "2.4 GHz", "in_use": inuse == "*"}
    return sorted(best.values(), key=lambda x: (-x["in_use"], -x["signal"]))


def dhcp_probe_cli():
    """python3 -m netkey.uplink --dhcp-probe IFACE (run inside the party namespace): prints the offer as JSON."""
    print(json.dumps(dhcp_probe(sys.argv[2])))


# the uplink manager (inside the root LAN daemon)
class Uplink:
    def __init__(self, daemon):
        self.d = daemon
        self.state = read_json(os.path.join(daemon.state_dir, STATE_FILE), {}) or {}
        self.wifi = None
        self.eth = None
        self.mgmt = None                 # name of the auto port while it serves as management port
        self.carrier_lost = None
        self.no_ip_since = None
        self.last_radio = 0.0
        self.lost_since = None           # no uplink since (monotonic)
        self.hotspot = "off"             # off | on | retry
        self.hotspot_since = 0.0
        self.last_client = 0.0
        self.last_poll = 0.0
        self.wifi_info = {}
        self.scan = {"at": None, "networks": []}
        self.status = {}

    # helpers
    def run(self, *cmd, check=False, timeout=60):
        try:
            return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return subprocess.CompletedProcess(cmd, 1, "", str(e))

    def nm(self, *args, timeout=60):
        if not shutil.which("nmcli"):
            return subprocess.CompletedProcess(args, 1, "", "NetworkManager (nmcli) is not installed")
        return self.run("nmcli", *args, timeout=timeout)

    def save(self):
        write_json_atomic(os.path.join(self.d.state_dir, STATE_FILE), self.state, mode=0o600)

    def chosen(self):
        """True when someone picked the uplink (in the GUI, or uplink = "wifi" in config.toml)."""
        return bool(getattr(self.d, "uplink_chosen", False)) or self.d.cfg["network"]["uplink"] != "ethernet"

    def configured(self):
        """The uplink setting; until someone picks one, what the box was using when it first came online."""
        return self.d.cfg["network"]["uplink"] if self.chosen() else self.state.get("auto_mode", "ethernet")

    def mode(self):
        return "wifi" if self.configured() == "wifi" and self.wifi else "ethernet"

    def learn_mode(self):
        """Nobody picked the uplink yet: take the one the box came online through (set up with Raspberry Pi Imager
        on Wi-Fi, say), once, so the GUI shows it and the built-in port is free to decide by itself."""
        if self.chosen() or "auto_mode" in self.state:
            return
        routes = sorted(self.uplink_routes(), key=lambda r: r.get("metric", 0))
        if not routes:
            return
        self.state["auto_mode"] = "wifi" if self.wifi and routes[0].get("dev") == self.wifi else "ethernet"
        self.save()
        log.info("lan: uplink not chosen yet; the box came online via %s, so that is the uplink",
                 "Wi-Fi" if self.state["auto_mode"] == "wifi" else "Ethernet")

    def hotspot_password(self):
        return HOTSPOT_PASSWORD

    def hotspot_ssid(self):
        mac = (read_sys(os.path.join(self.d.sys_net, self.wifi or "", "address")) or "000000").replace(":", "")
        return f"NetKey-{mac[-4:].upper()}"

    def carrier(self, name):
        return read_sys(os.path.join(self.d.sys_net, name, "carrier")) == "1"

    def eth_carrier(self):
        """Cable in the built-in port, also while it sits in the party namespace (not visible in our /sys)."""
        if os.path.exists(os.path.join(self.d.sys_net, self.eth)):
            return self.carrier(self.eth)
        return "LOWER_UP" in self.d.ns_links().get(self.eth, {}).get("flags", [])

    def uplink_routes(self):
        return [r for r in self.d.default_route() if r.get("dev") != VETH_MAIN]

    # which ports exist
    def refresh_ports(self):
        self.wifi = wifi_iface(self.d.sys_net)
        eth = builtin_ethernet(self.d.sys_net)
        if not eth and self.eth and self.d.ns_exists() and self.eth in self.d.ns_links():
            eth = self.eth                             # moved into the party namespace: still the built-in port
        self.eth = eth

    def exclude(self):
        """Interfaces find_nic must never take as the retro port."""
        out = set()
        if self.eth and (self.mode() == "ethernet" or self.mgmt == self.eth):
            out.add(self.eth)
        return out

    def auto_port(self):
        return self.eth if self.mode() == "wifi" else None

    # the automatic port
    def port_tick(self, ns_links):
        """Before find_nic: keep the management port with NetworkManager, give the port back in ethernet mode."""
        if not self.eth:
            return
        if self.mode() == "ethernet":
            self.mgmt = None
            if self.eth in ns_links:                   # was the retro port in wifi mode: back to NetworkManager
                self.release(self.eth, "uplink is Ethernet again")
            return
        in_main = os.path.exists(os.path.join(self.d.sys_net, self.eth))
        if self.mgmt is None and in_main and self.eth not in ns_links and self.carrier(self.eth) and \
                self.has_ipv4(self.eth):
            self.mgmt = self.eth                       # NetworkManager already runs it (boot, or just switched)
            log.info("lan: %s is connected to the home network; kept as management port", self.eth)
        if self.mgmt == self.eth:
            if self.carrier(self.eth):
                self.carrier_lost = None
                if self.has_ipv4(self.eth):
                    self.no_ip_since = None
                else:
                    self.no_ip_since = self.no_ip_since or time.monotonic()
                    if time.monotonic() - self.no_ip_since >= MGMT_NO_IP_S:
                        log.info("lan: %s has no address from the home network; deciding again", self.eth)
                        self.mgmt, self.no_ip_since = None, None
            else:
                self.carrier_lost = self.carrier_lost or time.monotonic()
                if time.monotonic() - self.carrier_lost >= MGMT_CARRIER_LOSS_S:
                    log.info("lan: %s unplugged; it decides again when plugged in", self.eth)
                    self.mgmt, self.carrier_lost = None, None

    def has_ipv4(self, name):
        r = self.run("ip", "-j", "-4", "addr", "show", "dev", name)
        try:
            return any(a.get("scope") == "global" for x in json.loads(r.stdout or "[]") for a in x.get("addr_info", []))
        except ValueError:
            return False

    def decide(self, name, leak):
        """Quarantined auto port with carrier: True when it was handed over as management port."""
        if name != self.auto_port():
            return False
        offer = None
        if not leak:
            r = self.d.run("ip", "netns", "exec", self.d.ns(), sys.executable, "-c",
                           "import sys; sys.path.insert(0, sys.argv[1]); sys.argv[1:] = ['--dhcp-probe', sys.argv[2]];"
                           " from netkey.uplink import dhcp_probe_cli; dhcp_probe_cli()", PKG_ROOT, name, check=False)
            try:
                offer = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None
            except (ValueError, IndexError):
                offer = None
        uplink_ok = bool(self.uplink_routes())
        home = self.state.get("home")
        if leak or (offer and (not uplink_ok or not home or home_match(offer, home))):
            why = "sees this Pi's own traffic" if leak else \
                f"DHCP from {offer.get('server') or offer.get('server_mac')} ({offer.get('subnet')})"
            log.info("lan: %s is cabled into the home network (%s); using it as management port", name, why)
            self.release(name, why)
            self.mgmt = name
            return True
        if offer:
            log.warning("lan: %s sees a DHCP server that is not the home router (%s); kept as retro port",
                        name, offer.get("server"))
        return False

    def release(self, name, why):
        """Party namespace -> main namespace, handed to NetworkManager for DHCP."""
        d = self.d
        mac = None
        if d.ns_exists():
            r = d.run("ip", "-n", d.ns(), "-j", "link", "show", name, check=False)
            try:
                mac = (json.loads(r.stdout)[0].get("address") or "").lower()
            except (ValueError, IndexError):
                mac = None
            d.run("ip", "-n", d.ns(), "link", "set", name, "nomaster", check=False)
            d.run("ip", "-n", d.ns(), "link", "set", name, "netns", "1", check=False)
        if d.nic == name:
            d.nic, d.nic_ok, d.leak = None, False, None
        d.unpin_nm(mac or (read_sys(os.path.join(d.sys_net, name, "address")) or "").lower())
        self.nm("device", "set", name, "managed", "yes", timeout=15)
        self.nm("device", "connect", name, timeout=30)
        log.info("lan: %s handed to NetworkManager (%s)", name, why)

    # learning the home network
    def learn_home(self):
        for r in self.uplink_routes():
            gw, dev = r.get("gateway"), r.get("dev")
            if not gw or dev == self.wifi and self.hotspot == "on":
                continue
            out = self.run("ip", "-j", "-4", "route", "show", "dev", dev, "scope", "link")
            try:
                nets = [x["dst"] for x in json.loads(out.stdout or "[]") if "/" in x.get("dst", "")]
            except ValueError:
                nets = []
            subnet = next((n for n in nets if ipaddress.IPv4Address(gw) in ipaddress.IPv4Network(n)), None)
            out = self.run("ip", "-j", "neigh", "show", gw, "dev", dev)
            try:
                gw_mac = (json.loads(out.stdout or "[]")[0].get("lladdr") or "").lower() or None
            except (ValueError, IndexError):
                gw_mac = None
            home = {"gateway": gw, "subnet": subnet, "gw_mac": gw_mac or (self.state.get("home") or {}).get("gw_mac")}
            if subnet and self.state.get("home") != home:
                self.state["home"] = home
                self.save()
                log.info("lan: home network is %s via %s", subnet, gw)
            return

    # Wi-Fi
    def poll_wifi(self):
        if not self.wifi:
            self.wifi_info = {}
            return
        known = self.migrate(self.known_networks())            # first, so the device shows the new name
        r = self.nm("-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", self.wifi,
                    timeout=10)
        info = {}
        for line in r.stdout.splitlines():
            k, _, v = line.partition(":")
            if k == "GENERAL.STATE":
                info["state"] = v.split("(")[-1].rstrip(")") if "(" in v else v
            elif k == "GENERAL.CONNECTION":
                info["connection"] = v or None
            elif k.startswith("IP4.ADDRESS") and v:
                info.setdefault("ip", v.split("/")[0])
        # networks NetworkManager saw in its last scan (no new scan: that would disturb the connection)
        r = self.nm("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list", "ifname", self.wifi,
                    "--rescan", "no", timeout=10)
        seen = parse_scan(r.stdout) if r.returncode == 0 else []
        visible = {n["ssid"] for n in seen}
        if is_known_name(info.get("connection") or ""):
            cur = next((n for n in seen if n["in_use"]), None)
            if cur:
                info.update(ssid=cur["ssid"], signal=cur["signal"], band=cur["band"])
            else:
                info["ssid"] = next((k["ssid"] for k in known if k["name"] == info["connection"]), None)
        info["configured"] = bool(known)
        info["known"] = [{"ssid": k["ssid"], "preferred": i == 0, "connected": k["name"] == info.get("connection"),
                          "in_range": k["ssid"] in visible} for i, k in enumerate(known)]
        if known and not info.get("ssid"):
            info["ssid"] = known[0]["ssid"]                         # the one it will join first
        self.wifi_info = info

    def known_networks(self):
        """The saved Wi-Fi networks, preferred first: [{name, ssid, priority, used}]."""
        r = self.nm("-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY,TIMESTAMP", "connection", "show", timeout=10)
        cache = getattr(self, "ssid_cache", None)
        if cache is None:
            cache = self.ssid_cache = {}
        out = []
        for line in r.stdout.splitlines():
            f = nm_split(line)
            if len(f) < 4 or not is_known_name(f[0]) or "wireless" not in f[1]:
                continue
            name = f[0]
            if name not in cache:
                cache[name] = self.nm("-t", "-g", "802-11-wireless.ssid", "connection", "show", name,
                                      timeout=10).stdout.strip()
            out.append({"name": name, "ssid": cache[name],
                        "priority": int(f[2]) if f[2].lstrip("-").isdigit() else 0,
                        "used": int(f[3]) if f[3].isdigit() else 0})
        out.sort(key=lambda k: (-k["priority"], -k["used"]))
        return out

    def forget_cache(self):
        self.ssid_cache = {}

    @staticmethod
    def free_name(known):
        used = {k["name"] for k in known}
        return next(f"{WIFI_PROFILE}-{n}" for n in range(1, MAX_KNOWN + 2) if f"{WIFI_PROFILE}-{n}" not in used)

    def migrate(self, known):
        """A box from before 0.10.4 has one network called netkey-wifi: it becomes netkey-wifi-<n>, preferred.
        Renaming doesn't drop the connection."""
        old = next((k for k in known if k["name"] == WIFI_PROFILE), None)
        if not old:
            return known
        name = self.free_name(known)
        r = self.nm("connection", "modify", WIFI_PROFILE, "connection.id", name,
                    "connection.autoconnect-priority", str(PRIO_PREFERRED), timeout=15)
        if r.returncode != 0:
            return known
        log.info("lan: saved Wi-Fi network %s is now %s", old["ssid"], name)
        self.forget_cache()
        return self.known_networks()

    def prefer(self, name, known):
        """Make one saved network the preferred one (joined first); the others stay as fallbacks."""
        for k in known:
            prio = PRIO_PREFERRED if k["name"] == name else PRIO_KNOWN
            if k["priority"] != prio:
                self.nm("connection", "modify", k["name"], "connection.autoconnect-priority", str(prio), timeout=15)

    def reconnect(self, before, known):
        """After a failed attempt: back to the network the box was on, else the preferred saved one."""
        target = before or (known[0]["name"] if known else None)
        if target:
            self.nm("--wait", "30", "connection", "up", target, "ifname", self.wifi, timeout=40)

    def wifi_connected(self):
        return is_known_name(self.wifi_info.get("connection") or "") and bool(self.wifi_info.get("ip"))

    def set_country(self, cc):
        cc = (cc or self.state.get("country") or "").upper()
        if not cc:
            return
        if len(cc) != 2 or not cc.isalpha():
            raise ValueError("Wi-Fi country must be a 2-letter code, e.g. NO")
        if shutil.which("raspi-config"):
            self.run("raspi-config", "nonint", "do_wifi_country", cc, timeout=30)
        elif shutil.which("iw"):
            self.run("iw", "reg", "set", cc)
        if self.state.get("country") != cc:
            self.state["country"] = cc
            self.save()

    def radio_on(self):
        if shutil.which("rfkill"):
            self.run("rfkill", "unblock", "wifi")
        self.nm("radio", "wifi", "on", timeout=10)

    # hotspot
    def clients(self):
        if not shutil.which("iw") or not self.wifi:
            return 0
        r = self.run("iw", "dev", self.wifi, "station", "dump", timeout=5)
        return r.stdout.count("Station ")

    def start_hotspot(self):
        self.radio_on()
        self.set_country(None)
        self.nm("connection", "delete", HOTSPOT_PROFILE, timeout=15)
        r = self.nm("connection", "add", "type", "wifi", "ifname", self.wifi, "con-name", HOTSPOT_PROFILE,
                    "autoconnect", "no", "ssid", self.hotspot_ssid(), "802-11-wireless.mode", "ap",
                    "802-11-wireless.band", "bg", "ipv4.method", "shared", "ipv4.addresses", HOTSPOT_ADDR,
                    "ipv6.method", "disabled", "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn",
                    "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.psk", self.hotspot_password(),
                    timeout=20)
        if r.returncode == 0:
            r = self.nm("--wait", "30", "connection", "up", HOTSPOT_PROFILE, timeout=40)
        if r.returncode != 0:
            log.warning("lan: rescue hotspot failed: %s", (r.stderr or r.stdout).strip())
            return False
        self.hotspot, self.hotspot_since, self.last_client = "on", time.monotonic(), time.monotonic()
        log.warning("lan: no uplink; rescue hotspot %s is on (GUI at http://%s)", self.hotspot_ssid(),
                    HOTSPOT_ADDR.split("/")[0])
        return True

    def stop_hotspot(self, retry=False):
        self.nm("connection", "down", HOTSPOT_PROFILE, timeout=20)
        self.hotspot, self.hotspot_since = ("retry" if retry else "off"), time.monotonic()
        known = self.known_networks() if retry else []
        if known:
            # the preferred network first; if it isn't there, NetworkManager's autoconnect tries the other saved ones
            visible = {k["ssid"] for k in self.wifi_info.get("known", []) if k.get("in_range")}
            target = next((k for k in known if k["ssid"] in visible), known[0])
            self.nm("--wait", "40", "connection", "up", target["name"], "ifname", self.wifi, timeout=50)
        log.info("lan: rescue hotspot off%s", " (trying the uplink again)" if retry else "")

    def hotspot_tick(self, now):
        if not self.wifi or not self.d.cfg["network"]["hotspot"]:
            if self.hotspot == "on":
                self.stop_hotspot()
            self.hotspot = "off"
            return
        up = bool(self.uplink_routes())
        if self.hotspot == "off":
            if up:
                self.lost_since = None
                return
            self.lost_since = self.lost_since or now
            wait = HOTSPOT_AFTER_S if self.wifi_info.get("configured") else HOTSPOT_AFTER_UNCONFIGURED_S
            if now - self.lost_since >= wait:
                if not self.start_hotspot():
                    self.lost_since = now                  # try again after another wait
        elif self.hotspot == "on":
            if self.clients():
                self.last_client = now
            idle = now - self.last_client
            if up and idle > 10:
                self.stop_hotspot()
                self.lost_since = None
            elif now - self.hotspot_since >= HOTSPOT_RETRY_S and idle >= 60:
                self.stop_hotspot(retry=True)
        elif self.hotspot == "retry":
            if up:
                self.hotspot, self.lost_since = "off", None
                log.info("lan: uplink is back")
            elif now - self.hotspot_since >= RETRY_WINDOW_S:
                self.start_hotspot()

    # every daemon step (before the LAN logic, also when the LAN port is switched off)
    def tick(self):
        self.refresh_ports()
        self.learn_mode()
        now = time.monotonic()
        if self.wifi and self.mode() == "wifi" and now - self.last_radio >= 30 and \
                self.wifi_info.get("state") in (None, "unavailable"):
            self.last_radio = now
            self.radio_on()
        if now - self.last_poll >= WIFI_POLL_S:
            self.last_poll = now
            self.poll_wifi()
            if self.hotspot != "on":
                self.learn_home()
        self.hotspot_tick(now)
        self.build_status(now)

    def build_status(self, now):
        routes = self.uplink_routes()
        eth_role, eth_carrier = None, False
        if self.eth:
            eth_carrier = self.eth_carrier()
            if self.mode() == "ethernet":
                eth_role = "uplink"
            elif self.mgmt == self.eth:
                eth_role = "management"
            elif not eth_carrier:
                eth_role = "unplugged"
            elif self.d.nic == self.eth:
                eth_role = "retro" if self.d.nic_ok else "checking"
            else:
                eth_role = "checking"
        eth_ip = None
        if self.eth and eth_role in ("uplink", "management"):
            r = self.run("ip", "-j", "-4", "addr", "show", "dev", self.eth)
            try:
                eth_ip = next((a["local"] for x in json.loads(r.stdout or "[]") for a in x.get("addr_info", [])
                               if a.get("scope") == "global"), None)
            except ValueError:
                pass
        self.status = {
            "mode": self.mode(), "configured_mode": self.configured(), "mode_chosen": self.chosen(),
            "wifi_available": bool(self.wifi), "uplink_ok": bool(routes),
            "uplink_dev": routes[0]["dev"] if routes else None,
            "uplink_lost_for": int(now - self.lost_since) if self.lost_since else None,
            "wifi": dict(self.wifi_info, iface=self.wifi, country=self.state.get("country")) if self.wifi else None,
            "ethernet": {"name": self.eth, "role": eth_role, "ip": eth_ip,
                         "carrier": eth_carrier} if self.eth else None,
            "hotspot": {"enabled": bool(self.d.cfg["network"]["hotspot"]) and bool(self.wifi), "state": self.hotspot,
                        "ssid": self.hotspot_ssid() if self.wifi else None, "address": HOTSPOT_ADDR.split("/")[0],
                        "clients": self.clients() if self.hotspot == "on" else 0,
                        "after_s": HOTSPOT_AFTER_S if self.wifi_info.get("configured") else
                        HOTSPOT_AFTER_UNCONFIGURED_S},
            "home": self.state.get("home"), "scan": self.scan,
        }

    def private_status(self):
        out = json.loads(json.dumps(self.status))
        if out.get("hotspot") and self.wifi:
            out["hotspot"]["password"] = self.hotspot_password()
        return out

    # commands from the GUI / CLI
    def command(self, c):
        cmd = c["cmd"]
        if not self.wifi:
            raise ValueError("This box has no Wi-Fi")
        if cmd == "wifi_scan":
            self.radio_on()
            nets = []
            for attempt in range(4):              # a radio that was just switched on needs a few seconds
                r = self.nm("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list", "ifname",
                            self.wifi, "--rescan", "yes", timeout=30)
                nets = parse_scan(r.stdout) if r.returncode == 0 else []
                if nets:
                    break
                time.sleep(3)
            if r.returncode != 0 and not nets:
                raise ValueError((r.stderr or r.stdout).strip() or "Wi-Fi scan failed")
            self.scan = {"at": time.time(), "networks": nets[:40]}
            return f"Found {len(self.scan['networks'])} Wi-Fi networks"
        if cmd == "wifi_connect":
            ssid, psk = str(c.get("ssid") or ""), str(c.get("password") or "")
            if not 1 <= len(ssid.encode()) <= 32:
                raise ValueError("Network name must be 1-32 bytes")
            if psk and not 8 <= len(psk) <= 63:
                raise ValueError("Wi-Fi password must be 8-63 characters")
            self.set_country(c.get("country"))
            if not self.state.get("country"):
                raise ValueError("Choose the Wi-Fi country first")
            self.radio_on()
            if self.hotspot == "on":
                self.stop_hotspot()
            self.forget_cache()
            known = self.known_networks()
            cur = self.wifi_info.get("connection") or ""
            before = cur if is_known_name(cur) else None
            match = next((k for k in known if k["ssid"] == ssid), None)
            if match and not psk:                    # a saved network: switch to it, it becomes the preferred one
                r = self.nm("--wait", "45", "connection", "up", match["name"], "ifname", self.wifi, timeout=60)
                if r.returncode != 0:
                    self.reconnect(before, known)
                    err = (r.stderr or r.stdout).strip().replace("Error: ", "")
                    raise ValueError(f"Could not join {ssid}: {err or 'no answer'}")
                self.prefer(match["name"], known)
            else:                                    # a new network, or a new password for a saved one
                self.nm("connection", "delete", WIFI_TRIAL, timeout=15)
                args = ["--wait", "45", "device", "wifi", "connect", ssid, "ifname", self.wifi, "name", WIFI_TRIAL]
                if psk:
                    args[6:6] = ["password", psk]
                r = self.nm(*args, timeout=60)
                if r.returncode != 0:
                    self.nm("connection", "delete", WIFI_TRIAL, timeout=15)
                    self.reconnect(before, known)
                    err = (r.stderr or r.stdout).strip().replace("Error: ", "")
                    raise ValueError(f"Could not join {ssid}: {err or 'no answer'}")
                if match:
                    self.nm("connection", "delete", match["name"], timeout=15)
                    known = [k for k in known if k is not match]
                for old in sorted(known, key=lambda k: k["used"])[:max(0, len(known) + 1 - MAX_KNOWN)]:
                    self.nm("connection", "delete", old["name"], timeout=15)     # keep at most MAX_KNOWN
                    known.remove(old)
                    log.info("lan: forgot the least recently used Wi-Fi network %s", old["ssid"])
                name = self.free_name(known)
                self.nm("connection", "modify", WIFI_TRIAL, "connection.id", name, "connection.autoconnect", "yes",
                        "connection.autoconnect-priority", str(PRIO_PREFERRED), timeout=15)
                self.prefer(name, known + [{"name": name, "priority": PRIO_PREFERRED}])
            self.forget_cache()
            self.last_poll = 0.0
            self.poll_wifi()
            return f"Connected to {ssid}" + (f" as {self.wifi_info['ip']}" if self.wifi_info.get("ip") else "")
        if cmd == "wifi_forget":
            self.forget_cache()
            known = self.known_networks()
            ssid = str(c.get("ssid") or "")
            cur = self.wifi_info.get("connection")
            k = next((k for k in known if k["ssid"] == ssid), None) if ssid else \
                next((k for k in known if k["name"] == cur), known[0] if known else None)
            if not k:
                raise ValueError(f"{ssid or 'That network'} is not saved on this box")
            self.nm("connection", "delete", k["name"], timeout=15)
            rest = [x for x in known if x is not k]
            if rest and k is known[0]:
                self.prefer(rest[0]["name"], rest)              # the next one up becomes the preferred network
            self.forget_cache()
            self.last_poll = 0.0
            self.poll_wifi()
            return f"Forgot {k['ssid']}" + (": the box joins another saved network in range" if rest and k["name"] == cur
                                            else "")
        raise ValueError(f"unknown command {cmd}")


def read_sys(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


VETH_MAIN = "pm-lan0"

if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "--dhcp-probe":
    dhcp_probe_cli()
