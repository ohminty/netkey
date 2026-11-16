"""Client for the netkey cloud hub: box registration, the long-poll event channel and party calls.

The hub only introduces boxes and relays their encrypted WireGuard packets; it has no keys. TLS is pinned to the
hub certificate's SHA-256 fingerprint (lan.hub_fingerprint), so no certificate authority is involved.
Protocol 2: the hub may run a party's packets through one of several relay servers. The box measures its round-trip
time to each with STUN (RFC 5389) and reports it, so the hub can pick the relay nearest the host.
Invite codes: KEY- and 8 groups of 4: a 40-bit party id, then a 120-bit SHA-256 fingerprint of the host's
WireGuard key (same scheme as netkey_hub/codes.py in netkey-hub). 120 bits means the hub can't forge a matching key.
"""
import base64
import binascii
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import queue
import socket
import ssl
import statistics
import struct
import threading
import time
from urllib.parse import urlsplit

from .store import read_json, write_json_atomic

log = logging.getLogger("netkey")

CREDS_FILE = "hub-box.json"   # the LAN service's token; the GUI process keeps its own (WEB_CREDS_FILE)
WEB_CREDS_FILE = "hub-web.json"
PROTO = 3                     # 2: relay_host in approvals, relay_moved, /v1/relays, /v1/rtt; 3: box identity, /v1/me
PROBE_MAX_AGE_S = 600         # during a party relays are measured again after this long (always before creating one)
MAX_RELAYS = 128              # relays measured per probe (the hub accepts up to 256 times per report)
STUN_MAGIC = 0x2112A442
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_PREFIX = "KEY"
ID_CHARS, FP_CHARS = 8, 24


# codes
def b32(value, chars):
    out = ""
    for _ in range(chars):
        out = ALPHABET[value & 31] + out
        value >>= 5
    return out


def key_fingerprint(pub, chars=FP_CHARS):
    try:
        raw = base64.b64decode(pub, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("bad WireGuard key") from None
    if len(raw) != 32:
        raise ValueError("bad WireGuard key")
    return b32(int.from_bytes(hashlib.sha256(raw).digest()[:15], "big") >> (5 * (FP_CHARS - chars)), chars)


def verify_words(pub):
    """Short key fingerprint people compare by eye or phone (like Signal's safety number): 60 bits."""
    fp = key_fingerprint(pub, 12)
    return "-".join(fp[i:i + 4] for i in range(0, 12, 4))


def is_hub_code(code):
    return "".join(ch for ch in str(code).upper() if ch.isalnum()).startswith(CODE_PREFIX)


def parse_hub_code(code):
    """(code_id, fingerprint), normalised; ValueError if it can't be a KEY-... code."""
    text = "".join(ch for ch in str(code).upper() if ch.isalnum())
    if not text.startswith(CODE_PREFIX) or len(text) != len(CODE_PREFIX) + ID_CHARS + FP_CHARS:
        raise ValueError("not a complete LAN party code (KEY- and 8 groups of 4 characters)")
    body = "".join({"O": "0", "I": "1", "L": "1"}.get(ch, ch) for ch in text[len(CODE_PREFIX):])
    if any(ch not in ALPHABET for ch in body):
        raise ValueError("the code contains invalid characters")
    return body[:ID_CHARS], body[ID_CHARS:]


# HTTPS with a pinned certificate
class HubError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class HubClient:
    def __init__(self, url, fingerprint, state_dir, name, identity=None, creds_file=CREDS_FILE):
        """identity: {"secret", "box_id"} from identity.load(), or None to register as a box without one (then the
        hub hands out a new box ID each time it has forgotten the box, and public parties are unavailable)."""
        u = urlsplit(url)
        if u.scheme != "https" or not u.hostname:
            raise ValueError("lan.hub_url must be https://host:port")
        self.url, self.host, self.port = url, u.hostname, u.port or 443
        self.fingerprint = fingerprint.lower().replace(":", "")
        self.creds_path = os.path.join(state_dir, creds_file)
        self.name = name
        self.identity = identity
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE       # verified below by fingerprint (pinning)
        self.creds = read_json(self.creds_path) or {}
        if self.creds.get("url") != url:
            self.creds = {}

    def adopt_identity(self):
        """Switch a token from before the identity existed (or from a copied identity) to the permanent box ID.
        Call it only while the box is in no hub party: the party belongs to the box ID the token stands for."""
        if self.identity and self.creds.get("token") and self.creds.get("identity") != self.identity["box_id"]:
            log.info("lan: switching to the permanent box ID %s", self.identity["box_id"])
            self.creds = {}
            write_json_atomic(self.creds_path, {}, mode=0o600)

    def call(self, method, path, body=None, timeout=15, auth=True):
        if not self.fingerprint:
            raise HubError(0, "no hub configured (lan.hub_fingerprint is empty)")
        conn = http.client.HTTPSConnection(self.host, self.port, context=self.ctx, timeout=timeout)
        try:
            conn.connect()
            der = conn.sock.getpeercert(binary_form=True)
            if hashlib.sha256(der).hexdigest() != self.fingerprint:
                raise HubError(0, "the hub's certificate does not match lan.hub_fingerprint")
            headers = {"Content-Type": "application/json", "X-NetKey-Proto": str(PROTO)}
            if auth:
                headers["Authorization"] = f"Bearer {self.token()}"
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            r = conn.getresponse()
            data = json.loads(r.read() or b"{}")
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise HubError(0, f"cannot reach the hub: {e}") from None
        finally:
            conn.close()
        if r.status == 401 and auth:
            self.creds = {}                      # hub forgot us (or new hub): register again next time
            write_json_atomic(self.creds_path, {}, mode=0o600)
        if r.status != 200:
            raise HubError(r.status, data.get("error") or f"hub error {r.status}")
        return data

    def token(self):
        if not self.creds.get("token"):
            body = {"name": self.name}
            if self.identity:
                body["identity"] = self.identity["secret"]
            d = self.call("POST", "/v1/register", body, auth=False)
            self.creds = {"url": self.url, "box": d["box"], "token": d["token"]}
            if self.identity:
                self.creds["identity"] = self.identity["box_id"]
            write_json_atomic(self.creds_path, self.creds, mode=0o600)
            log.info("lan: registered with the hub as box %s", d["box"])
        return self.creds["token"]

    @property
    def box(self):
        return self.creds.get("box")


class HubAgent(threading.Thread):
    """Keeps the long-poll event channel open; events go to a queue the LAN daemon drains on its own thread."""

    def __init__(self, client, after=0):
        super().__init__(daemon=True, name="hub-events")
        self.client = client
        self.after = after
        self.events = queue.Queue()
        self.online = False
        self.error = None
        self.last_ok = None
        self.stopping = threading.Event()

    def run(self):
        backoff = 2
        while not self.stopping.is_set():
            try:
                d = self.client.call("GET", f"/v1/events?after={self.after}&wait=25", timeout=40)
                for e in d.get("events", []):
                    self.events.put(e)
                    self.after = max(self.after, e["seq"])
                self.online, self.error, self.last_ok, backoff = True, None, time.time(), 2
            except HubError as e:
                if self.online or self.error != str(e):
                    log.warning("lan: hub: %s", e)
                self.online, self.error = False, str(e)
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 60)

    def stop(self):
        self.stopping.set()


# relays: STUN round-trip times
def stun_rtts(targets, tries=3, timeout=1.0, gap=0.2):
    """Median STUN Binding round-trip time in ms per target {key: (ip, port)}, or None if none was answered.

    All targets are measured at once from one socket: `tries` rounds of requests `gap` s apart, then up to `timeout`
    s for the answers. It takes about gap * (tries - 1) + the slowest round trip, at most about 1.4 s, however many
    relays there are."""
    samples = {k: [] for k in targets}
    pending = {}                                   # transaction id -> (target, monotonic send time)
    magic = struct.pack("!I", STUN_MAGIC)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        start, sent = time.monotonic(), 0
        end = start + gap * (tries - 1) + timeout
        while True:
            now = time.monotonic()
            while sent < tries and now >= start + sent * gap:
                for k, addr in targets.items():
                    txid = os.urandom(12)
                    try:
                        s.sendto(struct.pack("!HH", 1, 0) + magic + txid, addr)
                        pending[txid] = (k, time.monotonic())
                    except OSError:
                        pass
                sent += 1
                now = time.monotonic()
            if sent == tries and not pending:
                break
            left = (end if sent == tries else min(end, start + sent * gap)) - now
            if left <= 0:
                if sent == tries:
                    break
                continue
            s.settimeout(left)
            try:
                data, _ = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) >= 20 and data[:2] == b"\x01\x01" and data[4:8] == magic:
                hit = pending.pop(data[8:20], None)
                if hit:
                    samples[hit[0]].append((time.monotonic() - hit[1]) * 1000)
    finally:
        s.close()
    return {k: round(statistics.median(v), 1) if v else None for k, v in samples.items()}


def stun_rtt(ip, port, tries=3, timeout=1.0):
    """Median round-trip time in ms of STUN Binding requests to ip:port, or None if none was answered."""
    return stun_rtts({0: (ip, port)}, tries, timeout)[0]


def relay_ip(host, cache):
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        return resolve(host, cache)


def probe_relays(client, cache):
    """Measures every relay the hub lists (all at once) and reports the times; returns what the GUI shows."""
    d = client.call("GET", "/v1/relays")
    relays = [r for r in d.get("relays", []) if isinstance(r, dict)][:MAX_RELAYS]
    targets = {}
    for r in relays:
        try:
            port = int(r.get("stun_port") or 0)
            if 0 < port < 65536:
                targets[str(r["id"])] = (relay_ip(str(r["host"]), cache), port)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    rtt = {k: v for k, v in stun_rtts(targets).items() if v is not None}
    if rtt:
        client.call("POST", "/v1/rtt", {"rtt": rtt})
    clean = lambda v: "".join(ch for ch in str(v or "") if ch.isalnum() or ch in "-._")[:32]
    return {"at": time.time(),
            "relays": [{"id": clean(r.get("id")), "name": clean(r.get("name")),
                        "rtt_ms": rtt.get(str(r.get("id")))} for r in relays]}


class RelayProbe(threading.Thread):
    def __init__(self, client, cache, done):
        super().__init__(daemon=True, name="relay-probe")
        self.client, self.cache, self.done = client, cache, done

    def run(self):
        try:
            self.done(probe_relays(self.client, self.cache), None)
        except HubError as e:
            self.done(None, str(e))
        except Exception as e:                 # never take the daemon down for a measurement
            log.exception("lan: relay probe")
            self.done(None, str(e))


def resolve(host, cache, ttl=300):
    """IPv4 address for a host name (cached); keeps the last good answer if DNS fails."""
    now = time.monotonic()
    hit = cache.get(host)
    if hit and now - hit[1] < ttl:
        return hit[0]
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
        ip = infos[0][4][0]
        cache[host] = (ip, now)
        return ip
    except (OSError, IndexError):
        if hit:
            return hit[0]
        raise
