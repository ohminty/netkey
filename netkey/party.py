"""LAN party (direct backend): one box hosts, up to 15 members join over WireGuard.

Topology (star): member retro LAN -- VXLAN (VNI 1000+id) over WireGuard --> host br-retro (DHCP, gateway, NAT).
The WireGuard interface is created in the box's main namespace (its UDP socket stays there) and moved into the
party namespace, so nothing party-related is reachable from the main namespace.

Joining needs only the host's UDP port: the invite code carries the host's endpoint and public key plus a shared
*join key*. With it a member reaches only the host's control port (172.30.98.1:7999) and asks to join; once the
host approves, the member switches to its own key and gets its VXLAN port. The host is the only enforcement point:
kicking removes the member's VXLAN port at once, tells the member on its next hello, then removes its key.
"""
import base64
import binascii
import ipaddress
import json
import os
import secrets
import socket
import struct
import subprocess
import threading
import time

from .store import read_json, write_json_atomic

CODE_PREFIX = "netkey1-"
CODE_VERSION = 1
CTRL_PORT = 7999
VX_PORT = 4789
HOST_OCTET, JOIN_OCTET, MEMBER_BASE, VNI_BASE = 1, 254, 10, 1000
PROTO = 1
# Full 1500-byte retro frames + VXLAN (50) don't fit this MTU on purpose: the kernel splits such VXLAN packets
# *before* encryption (VXLAN df unset), so every WireGuard packet on the internet stays <= 1440 bytes and is never
# IP-fragmented (fragments get dropped by mobile/CGNAT/DS-Lite paths). 1380 also fits PPPoE and DS-Lite.
WG_MTU = 1380
# Largest IPv4 packet the tunnel passes whole with DF set (VXLAN adds 50 bytes), minus IP and TCP headers. TCP
# handshakes crossing a tunnel port get their MSS lowered to this, so no TCP packet ever needs PMTU discovery.
TUNNEL_TCP_MSS = WG_MTU - 50 - 40
HELLO_S = 5.0
ONLINE_S = 180          # WireGuard handshake age that still counts as online
KICK_GRACE_S = 20       # a kicked member's key stays this long so its next hello hears "kicked"
MAX_PENDING = 8
MAX_MEMBERS = 15        # tunnel addresses .11-.25
STATE_FILE = "party-state.json"
CMD_DIR = "party-cmd"
PARTY_FILE = "party.json"     # run dir, 0640 root:netkey: includes the invite code


def clean_name(name):
    return "".join(ch for ch in str(name or "") if ch.isalnum() or ch in "-_. ")[:32].strip() or "NetKey"


# keys and invite codes
def wg_genkey():
    priv = subprocess.run(["wg", "genkey"], capture_output=True, text=True, check=True).stdout.strip()
    return priv, wg_pubkey(priv)


def wg_pubkey(priv):
    return subprocess.run(["wg", "pubkey"], input=priv + "\n", capture_output=True, text=True,
                          check=True).stdout.strip()


def key_bytes(key):
    raw = base64.b64decode(key, validate=True)
    if len(raw) != 32:
        raise ValueError("WireGuard key must be 32 bytes")
    return raw


def encode_code(party_id, endpoint, port, host_pub, join_priv):
    """netkey1-<base32>: version, party id, port, host public key, join private key, endpoint, CRC16."""
    host = endpoint.encode("idna" if any(c.isalpha() for c in endpoint) else "ascii")
    if not 1 <= len(host) <= 253:
        raise ValueError("endpoint must be 1-253 characters")
    body = struct.pack("!BIH", CODE_VERSION, party_id, port) + key_bytes(host_pub) + key_bytes(join_priv) + host
    body += struct.pack("!H", binascii.crc_hqx(body, 0xFFFF))
    text = base64.b32encode(body).decode().rstrip("=").lower()
    return CODE_PREFIX + "-".join(text[i:i + 6] for i in range(0, len(text), 6))


def decode_code(code):
    """Returns dict(party_id, endpoint, port, host_pub, join_priv); ValueError on a bad or mistyped code."""
    text = "".join(ch for ch in str(code).strip().lower() if ch.isalnum())
    prefix = CODE_PREFIX.replace("-", "")
    if not text.startswith(prefix):
        raise ValueError("not a NetKey LAN party code (it starts with netkey1-)")
    text = text[len(prefix):].upper()
    try:
        body = base64.b32decode(text + "=" * (-len(text) % 8))
    except (binascii.Error, ValueError):
        raise ValueError("the code contains invalid characters") from None
    if len(body) < 1 + 4 + 2 + 64 + 1 + 2 or binascii.crc_hqx(body[:-2], 0xFFFF) != struct.unpack("!H", body[-2:])[0]:
        raise ValueError("the code is incomplete or mistyped")
    version, party_id, port = struct.unpack("!BIH", body[:7])
    if version != CODE_VERSION:
        raise ValueError("this code is for a different NetKey version")
    host_pub = base64.b64encode(body[7:39]).decode()
    join_priv = base64.b64encode(body[39:71]).decode()
    endpoint = body[71:-2].decode("ascii", "replace")
    return {"party_id": party_id, "endpoint": endpoint, "port": port, "host_pub": host_pub, "join_priv": join_priv}


# addressing and generated config
def tunnel_net(cfg):
    return ipaddress.IPv4Network(cfg["lan"]["tunnel"])


def tunnel_ip(cfg, octet):
    return str(tunnel_net(cfg).network_address + octet)


def member_ip(cfg, member_id):
    return tunnel_ip(cfg, MEMBER_BASE + int(member_id))


def host_ip(cfg):
    return tunnel_ip(cfg, HOST_OCTET)


def join_ip(cfg):
    return tunnel_ip(cfg, JOIN_OCTET)


def vni(member_id):
    return VNI_BASE + int(member_id)


def vx_name(member_id):
    return f"vx-m{int(member_id)}"


def wg_conf(private_key, peers, listen_port=None):
    """peers: [dict(pub, allowed, endpoint=None, keepalive=None)]"""
    for v in [private_key] + [str(x) for p in peers for x in p.values() if x is not None]:
        if any(ch in str(v) for ch in "\r\n\0"):
            raise ValueError("line break in a WireGuard config value")
    out = ["[Interface]", f"PrivateKey = {private_key}"]
    if listen_port:
        out.append(f"ListenPort = {int(listen_port)}")
    for p in peers:
        out += ["", "[Peer]", f"PublicKey = {p['pub']}", f"AllowedIPs = {p['allowed']}"]
        if p.get("endpoint"):
            out.append(f"Endpoint = {p['endpoint']}")
        if p.get("keepalive"):
            out.append(f"PersistentKeepalive = {int(p['keepalive'])}")
    return "\n".join(out) + "\n"


def endpoint_str(host, port):
    try:
        if ipaddress.ip_address(host).version == 6:
            return f"[{host}]:{port}"
    except ValueError:
        pass
    return f"{host}:{port}"


def tunnel_input_rules(cfg, role, member_ids, ctrl_ids=None):
    """Lines for the party namespace's inet input chain: what may arrive over wg-party.
    ctrl_ids: members that may still talk to the control port (a kicked member, to hear that it was kicked)."""
    if role == "host":
        ips = [member_ip(cfg, m) for m in member_ids]
        ctrl = [member_ip(cfg, m) for m in (member_ids if ctrl_ids is None else ctrl_ids)]
        rules = []
        if ips:
            rules.append(f'iifname "wg-party" ip saddr {{ {", ".join(ips)} }} udp dport {VX_PORT} accept')
        if ctrl:
            rules.append(f'iifname "wg-party" ip saddr {{ {", ".join(ctrl)} }} tcp dport {CTRL_PORT} accept')
        rules.append(f'iifname "wg-party" ip saddr {join_ip(cfg)} tcp dport {CTRL_PORT} accept')
        rules.append('iifname "wg-party" icmp type echo-request limit rate 5/second accept')
        return rules
    if role == "member":
        return [f'iifname "wg-party" ip saddr {host_ip(cfg)} udp dport {VX_PORT} accept',
                f'iifname "wg-party" ip saddr {host_ip(cfg)} icmp type echo-request limit rate 5/second accept']
    return []


def guard_ruleset(cfg, gateway, member_ids, role):
    """Bridge-family guards inside the party namespace. Host: everything arriving from member ports."""
    lan = cfg["lan"]
    lines = ["table bridge netkey_guard", "delete table bridge netkey_guard", "table bridge netkey_guard {"]
    if role == "host":
        for m in member_ids:
            lines.append(f"    set macs_m{m} {{ type ether_addr; size {int(lan['party_mac_limit'])}; "
                         f"flags dynamic,timeout; timeout 30m; }}")
    lines += ["    chain guard {"]
    lines.append("        ether type ip6 counter drop")
    for d in ("iifname", "oifname"):
        lines.append(f'        {d} "vx-*" tcp flags syn / syn,rst tcp option maxseg size > {TUNNEL_TCP_MSS} '
                     f'tcp option maxseg size set {TUNNEL_TCP_MSS}')
    if role == "host" and member_ids:
        pps = int(lan["party_bcast_pps"])
        lines += [
            '        iifname "vx-m*" udp sport 67 udp dport 68 counter drop',
            f'        iifname "vx-m*" arp saddr ip {gateway} counter drop',
            f'        iifname "vx-m*" ip saddr {gateway} counter drop',
            f'        iifname "vx-m*" meta pkttype {{ broadcast, multicast }} meter bcast {{ iifname limit rate over '
            f'{pps}/second burst {pps} packets }} counter drop',
        ]
        for m in member_ids:
            lines += [f'        iifname "{vx_name(m)}" ether saddr @macs_m{m} return',
                      f'        iifname "{vx_name(m)}" add @macs_m{m} {{ ether saddr }} return',
                      f'        iifname "{vx_name(m)}" counter drop']
    lines += ["    }",
              "    chain forward {", "        type filter hook forward priority filter; policy accept;",
              "        jump guard", "    }",
              "    chain input {", "        type filter hook input priority filter; policy accept;",
              "        jump guard", "    }", "}"]
    return "\n".join(lines) + "\n"


# persistent party state (root only)
def empty_state():
    return {"role": "standalone"}


class PartyState:
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, STATE_FILE)
        self.lock = threading.RLock()
        self.data = read_json(self.path, None) or empty_state()

    def save(self):
        with self.lock:
            write_json_atomic(self.path, self.data, mode=0o600)

    @property
    def role(self):
        return self.data.get("role", "standalone")


# sockets inside a namespace
def ns_socket(netns, kind=socket.SOCK_STREAM):
    """A socket created inside the named network namespace (the calling thread switches there and back)."""
    with open(f"/run/netns/{netns}") as target, open("/proc/thread-self/ns/net") as home:
        os.setns(target.fileno(), os.CLONE_NEWNET)
        try:
            return socket.socket(socket.AF_INET, kind)
        finally:
            os.setns(home.fileno(), os.CLONE_NEWNET)


def request(netns, host, msg, timeout=4.0):
    """One JSON line to host:CTRL_PORT from inside the namespace; returns the decoded reply (or raises OSError).

    reply["_rtt_ms"] is the network round trip through the tunnel, like a ping: the time connect() takes. The host's
    kernel answers the TCP handshake, so the host daemon being busy doesn't count, and neither does the request."""
    s = ns_socket(netns)
    try:
        s.settimeout(timeout)
        t0 = time.monotonic()
        s.connect((host, CTRL_PORT))
        rtt = round((time.monotonic() - t0) * 1000, 1)
        s.sendall((json.dumps(msg) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n") and len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        reply = json.loads(buf or b"{}")
        reply["_rtt_ms"] = rtt
        return reply
    except ValueError:
        raise OSError("bad reply from the host") from None
    finally:
        s.close()


class ControlServer(threading.Thread):
    """Host side: JSON-line requests on <tunnel host ip>:7999 inside the party namespace."""

    def __init__(self, netns, bind_ip, handler):
        super().__init__(daemon=True, name="party-control")
        self.handler = handler
        self.sock = ns_socket(netns)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind_ip, CTRL_PORT))
        self.sock.listen(16)
        self.sock.settimeout(1.0)
        self.stopping = False

    def run(self):
        while not self.stopping:
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.serve, args=(conn, addr[0]), daemon=True).start()

    def serve(self, conn, peer_ip):
        try:
            conn.settimeout(5.0)
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 8192:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            try:
                msg = json.loads(buf or b"{}")
                if not isinstance(msg, dict):
                    raise ValueError
                reply = self.handler(peer_ip, msg)
            except ValueError:
                reply = {"status": "error", "error": "bad request"}
            conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass
        finally:
            conn.close()

    def stop(self):
        self.stopping = True
        try:
            self.sock.close()
        except OSError:
            pass


# commands from the web GUI / CLI
def queue_command(state_dir, cmd, **args):
    """Drop a command for the root daemon (state_dir/party-cmd, writable by the netkey user). Returns its id."""
    d = os.path.join(state_dir, CMD_DIR)
    os.makedirs(d, exist_ok=True)
    cid = f"{time.time():.6f}-{secrets.token_hex(3)}"
    write_json_atomic(os.path.join(d, f"{cid}.json"), dict(args, cmd=cmd, id=cid), mode=0o660)
    return cid


def take_commands(state_dir):
    d = os.path.join(state_dir, CMD_DIR)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return []
    out = []
    for n in names:
        path = os.path.join(d, n)
        data = read_json(path)
        try:
            os.unlink(path)
        except OSError:
            pass
        if isinstance(data, dict) and data.get("cmd"):
            out.append(data)
    return out


def wait_result(run_dir, cid, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        p = read_json(os.path.join(run_dir, PARTY_FILE)) or {}
        for r in p.get("results", []):
            if r.get("id") == cid:
                return r
        time.sleep(0.3)
    return None
