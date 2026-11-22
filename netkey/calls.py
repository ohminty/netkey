"""Modem calls: extensions between lines on this box, and calls between boxes (netkey-dev/docs/phone-numbers.md).

  dial plan     which dial strings are extensions (555-0100 + line), NetKey numbers (555-1000..9999) or dial-up
  frames        type | length | payload: between the modem sessions and the LAN service, and between two boxes
  CallClient    the modem session's side (user netkey): a control connection to the LAN service, JSON lines
  CallService   the LAN service's side (root): line readiness, ringing, extension calls, and calls through the hub:
                each call gets its own network namespace (nkc0...) with a WireGuard interface through a relay pair,
                and the LAN service holds the call's TCP connection inside it (opened there with setns)

No caller ID: the callee's box is never told who calls. The hub knows (for "phone book only" and abuse limits).
"""
import collections
import ctypes
import json
import logging
import os
import secrets
import select
import socket
import struct
import subprocess
import threading
import time

log = logging.getLogger("netkey")

LAN_RUN_DIR = "/run/netkey-lan"
SOCK_NAME = "calls.sock"
PHONE_FILE = "phone.json"             # state dir: this box's number (as the hub last said)
BOOK_FILE = "phone-book.json"         # state dir: the phone book, written by the web GUI
EXT_BASE = 5550100                    # extensions: 555-0100 + line number
MAX_EXT = 16
SERVICE_FIRST, NETWORK_FIRST, NETWORK_LAST = 5550000, 5551000, 5559999
# made-up numbers people already use for dial-up: never assigned, always dial-up
DIALUP_KEEP = {"5551234", "5551212", "5550000"} | {"555" + d * 4 for d in "123456789"}
MAX_PREFIX = 4                        # 9, / 1 / an area code in front of the seven digits

# frames
HELLO, RINGING, ANSWER, CONNECTED, DATA, KEEPALIVE, HANGUP = range(1, 8)
FRAME_NAMES = {HELLO: "hello", RINGING: "ringing", ANSWER: "answer", CONNECTED: "connected", DATA: "data",
               KEEPALIVE: "keepalive", HANGUP: "hangup"}
MAX_FRAME = 4096
DATA_CHUNK = 1024
PROTO = 1

# timing
RING_EVERY_S = 6.0
RING_MAX_S = 60.0                     # the callee stops ringing by itself
ANSWER_CONFIRM_S = 3.0                # callee waits this long for CONNECTED after ANSWER
SETUP_S = 10.0                        # tunnel up and HELLO exchanged
KEEPALIVE_S = 2.0
LOST_S = 15.0
IDLE_LIMIT_S = 2 * 3600
CALL_LIMIT_S = 12 * 3600
PRESENCE_EVERY_S = 300
NUMBER_REFRESH_S = 600
MAX_SLOTS = 8                         # calls between boxes at once
MAX_PENDING = 64                      # frames kept for a line that hasn't attached yet (then the call ends)
MAX_CONTROL_LINE = 4096               # a control message longer than this closes the connection
REASONS = {"hangup", "cancel", "no_answer", "busy", "lost", "limit", "no_carrier", "no_call", "version"}
CALL_PORT = 7998
CALLER_IP, CALLEE_IP = "172.30.99.1", "172.30.99.2"
WG_MTU = 1380


# ---- dial plan
def digits(dial):
    return "".join(ch for ch in str(dial or "") if ch.isdigit())


def classify(dial):
    """('ppp', None) | ('ext', line) | ('reserved', number) | ('number', number) for a dial string."""
    d = digits(dial)
    if len(d) < 7 or len(d) - 7 > MAX_PREFIX or not d[-7:].startswith("555"):
        return "ppp", None
    n = d[-7:]
    if n in DIALUP_KEEP:
        return "ppp", None
    v = int(n)
    if EXT_BASE <= v < EXT_BASE + MAX_EXT:
        return "ext", v - EXT_BASE
    if v < NETWORK_FIRST:
        return "reserved", n
    return "number", n


def fmt_number(n):
    n = digits(n)
    return f"{n[:3]}-{n[3:]}" if len(n) == 7 else n


def extension(line):
    return fmt_number(str(EXT_BASE + int(line)))


# ---- frames
def frame(kind, payload=b""):
    if isinstance(payload, str):
        payload = payload.encode()
    if len(payload) > MAX_FRAME:
        raise ValueError("frame too long")
    return struct.pack(">BH", kind, len(payload)) + payload


def jframe(kind, **data):
    return frame(kind, json.dumps(data, separators=(",", ":")))


class FrameError(Exception):
    pass


class FrameReader:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 3:
            kind, n = struct.unpack(">BH", self.buf[:3])
            if kind not in FRAME_NAMES or n > MAX_FRAME:
                raise FrameError(f"bad frame (type {kind}, {n} bytes)")
            if len(self.buf) < 3 + n:
                break
            out.append((kind, bytes(self.buf[3:3 + n])))
            del self.buf[:3 + n]
        return out


def wg_key_ok(key):
    """A WireGuard public key as the hub passes it on: 32 bytes in base64."""
    import base64
    import binascii
    try:
        return isinstance(key, str) and len(key) == 44 and len(base64.b64decode(key, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


def port_ok(port):
    try:
        return 1 <= int(port) <= 65535
    except (TypeError, ValueError):
        return False


def reason_of(payload):
    """A hang-up reason from the other end, limited to the ones NetKey sends."""
    r = payload_json(payload).get("reason")
    return r if r in REASONS else "hangup"


def payload_json(payload):
    try:
        d = json.loads(payload.decode() or "{}")
        return d if isinstance(d, dict) else {}
    except (UnicodeDecodeError, ValueError):
        return {}


# ---- the modem session's side
class CallClient:
    """Control connection from one modem line to the LAN service. Single-threaded: the session polls it."""

    def __init__(self, line, path=None):
        self.line = int(line)
        self.path = path or os.path.join(LAN_RUN_DIR, SOCK_NAME)
        self.sock = None
        self.buf = b""
        self.pushes = collections.deque()
        self.replies = collections.deque()
        self.sent_state = None
        self.sent_at = 0.0
        self.retry_at = 0.0

    def connected(self):
        return self.sock is not None

    def connect(self):
        if self.sock is not None:
            return True
        if time.monotonic() < self.retry_at:
            return False
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(2)
            s.connect(self.path)
            s.setblocking(False)
        except OSError:
            s.close()
            self.retry_at = time.monotonic() + 5
            return False
        self.sock, self.buf, self.sent_state = s, b"", None
        return True

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def send(self, **msg):
        if not self.connect():
            return False
        try:
            self.sock.setblocking(True)
            self.sock.sendall((json.dumps(msg) + "\n").encode())
            self.sock.setblocking(False)
            return True
        except OSError:
            self.close()
            return False

    def poll(self):
        """Read what arrived; returns the pushed messages (ring, stop_ring) in order."""
        if self.sock is None:
            self.connect()
            return []
        while True:
            try:
                chunk = self.sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self.close()
                break
            if not chunk:
                self.close()
                break
            self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            (self.replies if str(msg.get("op", "")).endswith("_result") else self.pushes).append(msg)
        out = list(self.pushes)
        self.pushes.clear()
        return out

    def update(self, ready, baud, force=False):
        """Tell the service whether this line can take a call (sent on change and every 30 s)."""
        state = (bool(ready), int(baud))
        if force or state != self.sent_state or time.monotonic() - self.sent_at > 30:
            if self.send(op="hello", line=self.line, ready=state[0], baud=state[1]):
                self.sent_state, self.sent_at = state, time.monotonic()

    def request(self, op, want, timeout, abort=None, **kw):
        """Send a request and wait for its reply. abort() is checked every 50 ms; True cancels (returns 'abort').
        Returns the reply dict, None when the service can't be reached, or {'op': want, 'result': 'abort'}."""
        self.replies.clear()
        if not self.send(op=op, line=self.line, **kw):
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.sock is None:
                return None
            r, _, _ = select.select([self.sock], [], [], 0.05)
            if r:
                self.pushes.extend(self.poll())
            while self.replies:
                msg = self.replies.popleft()
                if msg.get("op") == want:
                    return msg
            if abort and abort():
                return {"op": want, "result": "abort"}
        return {"op": want, "result": "timeout"}

    def attach(self, call):
        """A data connection for a call: frames both ways."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(self.path)
        s.sendall((json.dumps({"op": "attach", "call": call, "line": self.line}) + "\n").encode())
        s.setblocking(False)
        return s


# ---- the LAN service's side
def _setns(fd):
    if hasattr(os, "setns"):
        os.setns(fd, os.CLONE_NEWNET)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.setns(fd, 0x40000000) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))


def ns_socket(ns, kind=socket.SOCK_STREAM):
    """A socket created inside network namespace `ns` (it stays there). A short-lived thread does the setns."""
    out = {}

    def work():
        try:
            fd = os.open(f"/run/netns/{ns}", os.O_RDONLY)
            try:
                _setns(fd)
            finally:
                os.close(fd)
            out["sock"] = socket.socket(socket.AF_INET, kind)
        except OSError as e:
            out["err"] = e
    t = threading.Thread(target=work, name="setns")
    t.start()
    t.join()
    if "err" in out:
        raise out["err"]
    return out["sock"]


class Leg:
    """One end of a call inside the LAN service: a modem session's data connection, or the TCP connection through
    the call's tunnel. A thread per leg reads frames and hands them to the service."""

    def __init__(self, sock, name):
        self.sock = sock
        self.name = name
        self.lock = threading.Lock()
        self.closed = False
        self.last_rx = self.last_tx = time.monotonic()

    def send(self, kind, payload=b""):
        if self.closed:
            return False
        try:
            with self.lock:
                self.sock.sendall(frame(kind, payload))
            self.last_tx = time.monotonic()
            return True
        except OSError:
            self.close()
            return False

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass


class Call:
    def __init__(self, cid, kind, caller_line=None):
        self.id = cid
        self.kind = kind                    # ext | out | in
        self.caller_line = caller_line      # ext, out
        self.ring_lines = set()             # lines ringing (ext: the one line; in: every ready line)
        self.answered_line = None
        self.state = "setup"                # setup | ringing | answered | connected | ended
        self.legs = {}                      # line -> Leg (modem session data connections)
        self.pending = collections.defaultdict(list)   # line -> frames waiting for that line to attach
        self.remote = None                  # Leg through the tunnel (out, in)
        self.slot = None
        self.number = None                  # out: the number dialed
        self.created = time.monotonic()
        self.ring_started = None
        self.last_data = time.monotonic()
        self.cancelled = False
        self.ended_reason = None
        self.reported = False
        self.lock = threading.RLock()

    def local_peer(self):
        """The line on this box that talks to the remote end (or the other line of an extension call)."""
        return self.caller_line if self.kind == "out" else self.answered_line


class CallService:
    """Runs inside the LAN service. `daemon` gives: cfg, party role, hub client, relay endpoint, state/run dirs."""

    def __init__(self, daemon, run_dir, state_dir):
        self.d = daemon
        self.run_dir = run_dir
        self.state_dir = state_dir
        self.path = os.path.join(run_dir, SOCK_NAME)
        self.lock = threading.RLock()
        self.lines = {}                     # line -> {"conn", "ready", "baud", "seen"}
        self.conns = {}                     # control socket -> line
        self.calls = {}
        self.slots = [None] * MAX_SLOTS
        self.server = None
        self.stopping = False
        self.phone = self.load_phone()
        self.synced = None                  # (incoming, allow) last sent to the hub
        self.presence_sent = ("unsent", 0.0)
        self.number_checked = 0.0
        self.hub_ok = None
        self.history = collections.deque(maxlen=20)
        self.cancelled = set()              # lines that gave up while their dial was still being set up

    # ---- lifecycle
    def start(self):
        self.cleanup_tunnels()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path)
        os.chmod(self.path, 0o660)
        try:
            import grp
            os.chown(self.path, 0, grp.getgrnam("netkey").gr_gid)
        except (KeyError, PermissionError, ImportError):
            pass
        s.listen(16)
        self.server = s
        threading.Thread(target=self.accept_loop, daemon=True, name="calls-accept").start()
        threading.Thread(target=self.tick_loop, daemon=True, name="calls-tick").start()
        log.info("calls: listening on %s%s", self.path,
                 f"; this box's number is {fmt_number(self.phone['number'])}" if self.phone.get("number") else "")

    def stop(self):
        self.stopping = True
        for c in list(self.calls.values()):
            self.end(c, "service stopping")
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
        self.cleanup_tunnels()

    def run_cmd(self, *cmd, check=True, input_text=None):
        r = subprocess.run(cmd, capture_output=True, text=True, input=input_text)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:4])}: {(r.stderr or r.stdout).strip()}")
        return r

    def cleanup_tunnels(self):
        """Leftovers of calls from before a crash or restart."""
        try:
            names = self.run_cmd("ip", "netns", "list", check=False).stdout.split()
            for n in names:
                if n.startswith("nkc") and n[3:].isdigit():
                    self.run_cmd("ip", "netns", "del", n, check=False)
            links = self.run_cmd("ip", "-o", "link", "show", check=False).stdout
            for i in range(MAX_SLOTS):
                if f" wgc{i}:" in links:
                    self.run_cmd("ip", "link", "del", f"wgc{i}", check=False)
        except OSError:
            pass                                 # no iproute2 (tests on another OS)

    # ---- settings and number
    def settings(self):
        p = self.d.cfg.get("phone") or {}
        incoming = p.get("incoming", "off")
        return {"incoming": incoming if incoming in ("off", "book", "anyone") else "off",
                "busy_in_party": bool(p.get("busy_in_party", True)), "dnd": bool(p.get("dnd", False))}

    def load_phone(self):
        try:
            with open(os.path.join(self.state_dir, PHONE_FILE)) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_phone(self, d):
        self.phone = d
        from .store import write_atomic
        write_atomic(os.path.join(self.state_dir, PHONE_FILE), json.dumps(d), mode=0o644)

    def book_numbers(self):
        try:
            with open(os.path.join(self.state_dir, BOOK_FILE)) as f:
                rows = json.load(f)
        except (OSError, ValueError):
            return []
        out = []
        for r in rows if isinstance(rows, list) else []:
            n = digits(r.get("number") if isinstance(r, dict) else "")
            if len(n) == 7 and classify(n)[0] == "number" and n not in out:
                out.append(n)
        return sorted(out)[:500]

    def number(self):
        return self.phone.get("number")

    def wants_events(self):
        return bool(self.number() and self.settings()["incoming"] != "off") or \
            any(c.kind in ("out", "in") for c in self.calls.values())

    def hub(self, method, path, body=None, timeout=15):
        from . import hubclient as hc
        try:
            r = self.d.hub_client().call(method, path, body, timeout=timeout)
            self.hub_ok = True
            return r
        except hc.HubError as e:
            if e.status == 0:
                self.hub_ok = False
            raise

    def command(self, cmd, **kw):
        """GUI commands (through the LAN service's command queue). Returns a message; raises ValueError."""
        from . import hubclient as hc
        try:
            if cmd == "phone_get":
                d = self.hub("POST", "/v1/number", {})
                self.save_phone({"number": d["number"], "since": time.time()})
                self.synced, self.presence_sent = None, ("unsent", 0.0)
                log.info("calls: this box's number is now %s", fmt_number(d["number"]))
                return f"Your number is {fmt_number(d['number'])}"
            if cmd == "phone_release":
                self.hub("POST", "/v1/number/release", {})
                self.save_phone({})
                log.info("calls: number given up")
                return "Number given up"
            if cmd == "phone_refresh":
                self.refresh_number(force=True)
                return "ok"
        except hc.HubError as e:
            raise ValueError(str(e)) from None
        if cmd == "phone_hangup":
            line = int(kw.get("line", -1))
            for c in list(self.calls.values()):
                if line in (c.caller_line, c.answered_line) or line in c.ring_lines:
                    self.hangup_from(c, line, "gui")
                    return "Hung up"
            raise ValueError("no call on that line")
        raise ValueError(f"unknown command {cmd}")

    def refresh_number(self, force=False):
        """What the hub has for this box (a number can be revoked by the operator)."""
        if not force and time.time() - self.number_checked < NUMBER_REFRESH_S:
            return
        self.number_checked = time.time()
        from . import hubclient as hc
        try:
            d = self.hub("GET", "/v1/number")
        except hc.HubError as e:
            if e.status not in (0, 404):
                log.info("calls: number check: %s", e)
            return
        n = d.get("number")
        if n != self.number():
            log.info("calls: the hub has %s for this box", fmt_number(n) if n else "no number")
            self.save_phone({"number": n, "since": time.time()} if n else {})
            self.synced = None

    def presence_reason(self):
        """None when a call can ring here now, else why not: off | dnd | party | busy (lines in calls) | no_line."""
        s = self.settings()
        if not self.number() or s["incoming"] == "off":
            return "off"
        if s["dnd"]:
            return "dnd"
        if s["busy_in_party"] and self.d.party.role in ("host", "member"):
            return "party"
        if self.ready_lines():
            return None
        return "busy" if any(self.line_busy(ln) for ln in self.lines) else "no_line"

    def presence(self):
        return self.presence_reason() is None

    def sync_hub(self):
        """Settings and presence to the hub, when they change (presence also every 5 minutes)."""
        if not self.number() or not self.d.cfg["lan"].get("hub_fingerprint"):
            return
        from . import hubclient as hc
        s = self.settings()
        want = (s["incoming"], tuple(self.book_numbers()) if s["incoming"] == "book" else ())
        try:
            if want != self.synced:
                self.hub("POST", "/v1/number/settings", {"incoming": want[0], "allow": list(want[1])})
                self.synced = want
            reason = self.presence_reason()
            last, at = self.presence_sent
            if s["incoming"] != "off" and (reason != last or time.time() - at > PRESENCE_EVERY_S):
                self.hub("POST", "/v1/presence", {"available": reason is None, "reason": reason})
                self.presence_sent = (reason, time.time())
        except hc.HubError as e:
            if e.status == 404:
                self.save_phone({})              # the number is gone (revoked or released elsewhere)
            log.info("calls: hub sync: %s", e)

    # ---- control connections from the modem sessions
    def accept_loop(self):
        while not self.stopping:
            try:
                conn, _ = self.server.accept()
            except OSError:
                if self.stopping:
                    return
                time.sleep(0.5)
                continue
            threading.Thread(target=self.serve_conn, args=(conn,), daemon=True, name="calls-conn").start()

    def serve_conn(self, conn):
        buf = b""
        conn.settimeout(None)
        line = None
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    op = msg.get("op")
                    if op == "attach":
                        self.attach(conn, msg, buf)
                        return                   # the connection is a call leg now
                    got = self.control(conn, msg)
                    line = got if got is not None else line
                if len(buf) > MAX_CONTROL_LINE:
                    log.warning("calls: control message too long; closing the connection")
                    break
        except OSError:
            pass
        finally:
            with self.lock:
                ln = self.conns.pop(conn, None)
                if ln is not None and self.lines.get(ln, {}).get("conn") is conn:
                    del self.lines[ln]
            try:
                conn.close()
            except OSError:
                pass

    def reply(self, conn, **msg):
        try:
            conn.sendall((json.dumps(msg) + "\n").encode())
        except OSError:
            pass

    def control(self, conn, msg):
        op = msg.get("op")
        try:
            line = int(msg.get("line"))
        except (TypeError, ValueError):
            return None
        if not 0 <= line < MAX_EXT:
            return None
        if op == "hello":
            with self.lock:
                old = self.lines.get(line)
                if old and old["conn"] is not conn:
                    self.conns.pop(old["conn"], None)
                try:
                    baud = int(msg.get("baud") or 0)
                except (TypeError, ValueError):
                    baud = 0
                self.lines[line] = {"conn": conn, "ready": bool(msg.get("ready")), "baud": baud,
                                    "seen": time.monotonic()}
                self.conns[conn] = line
            return line
        if op == "dial":
            threading.Thread(target=self.dial, args=(conn, line, str(msg.get("number", ""))), daemon=True,
                             name="calls-dial").start()
        elif op == "cancel":
            c = self.calls.get(str(msg.get("call")))
            if c is None:
                c = next((x for x in self.calls.values() if x.caller_line == line), None)
            if c and c.caller_line == line:
                c.cancelled = True
                self.hangup_from(c, line, "cancel")
            else:
                self.cancelled.add(line)
        elif op == "answer":
            self.answer(conn, line, str(msg.get("call")))
        return line

    def push(self, line, **msg):
        info = self.lines.get(line)
        if info:
            self.reply(info["conn"], **msg)

    def ready_lines(self, exclude=()):
        busy = set()
        for c in self.calls.values():
            busy |= c.ring_lines | {c.caller_line, c.answered_line}
        return sorted(ln for ln, i in self.lines.items() if i["ready"] and ln not in busy and ln not in exclude)

    def line_busy(self, line):
        return any(line in (c.caller_line, c.answered_line) or line in c.ring_lines for c in self.calls.values())

    def new_call(self, kind, caller_line=None, cid=None):
        c = Call(cid or secrets.token_hex(8), kind, caller_line)
        with self.lock:
            self.calls[c.id] = c
        return c

    # ---- dialing
    def dial(self, conn, line, number):
        kind, value = classify(number)
        self.cancelled.discard(line)
        try:
            if kind == "ext":
                result, cid = self.dial_ext(line, value)
            elif kind == "number":
                if not self.number():
                    result, cid = "ppp", None    # no number: 555 numbers are dial-up on this box, as before
                else:
                    result, cid = self.dial_out(line, value)
            elif kind == "reserved":
                result, cid = "no_carrier", None
            else:
                result, cid = "ppp", None
        except Exception:
            log.exception("calls: dial")
            result, cid = "no_carrier", None
        if line in self.cancelled and cid:
            self.cancelled.discard(line)
            c = self.calls.get(cid)
            if c:
                self.end(c, "cancelled while setting up")
            result, cid = "no_carrier", None
        self.reply(conn, op="dial_result", result=result, call=cid)

    def dial_ext(self, line, target):
        if target == line:
            return "busy", None
        info = self.lines.get(target)
        if info is None:
            return "no_carrier", None
        if self.line_busy(target) or not info["ready"]:
            return "busy", None
        c = self.new_call("ext", caller_line=line)
        c.number = extension(target)
        self.start_ringing(c, [target])
        self.queue(c, line, RINGING)
        log.info("calls: line %d calls line %d", line, target)
        return "ringing", c.id

    def start_ringing(self, c, lines):
        with c.lock:
            c.state = "ringing"
            c.ring_started = time.monotonic()
            c.ring_lines = set(lines)
        for ln in lines:
            self.push(ln, op="ring", call=c.id)

    def stop_ringing(self, c, missed=False, keep=None):
        with c.lock:
            lines, c.ring_lines = set(c.ring_lines), set()
        first = min(lines) if lines else None
        for ln in lines:
            if ln != keep:
                self.push(ln, op="stop_ring", call=c.id, missed=missed and ln == first)   # logged once

    # ---- answering and call legs
    def answer(self, conn, line, cid):
        c = self.calls.get(cid)
        ok = False
        if c:
            with c.lock:
                if c.state == "ringing" and line in c.ring_lines and c.answered_line is None:
                    c.answered_line, c.state, ok = line, "answered", True
            if ok:
                self.stop_ringing(c, keep=line)
                log.info("calls: line %d answered call %s", line, cid)
        self.reply(conn, op="answer_result", ok=ok, call=cid)

    def attach(self, conn, msg, rest):
        c = self.calls.get(str(msg.get("call")))
        try:
            line = int(msg.get("line"))
        except (TypeError, ValueError):
            line = -1
        if not c or line not in (c.caller_line, c.answered_line):
            try:
                conn.sendall(frame(HANGUP, json.dumps({"reason": "no_call"})))
                conn.close()
            except OSError:
                pass
            return
        leg = Leg(conn, f"line{line}")
        with c.lock:
            c.legs[line] = leg
            waiting, c.pending[line] = c.pending[line], []
        for kind, payload in waiting:
            leg.send(kind, payload)
        reader = FrameReader()
        try:
            frames = reader.feed(rest) if rest else []
            for kind, payload in frames:
                self.from_line(c, line, kind, payload)
            conn.settimeout(None)
            while not leg.closed:
                chunk = conn.recv(8192)
                if not chunk:
                    break
                for kind, payload in reader.feed(chunk):
                    self.from_line(c, line, kind, payload)
        except (OSError, FrameError):
            pass
        if c.state != "ended":
            self.hangup_from(c, line, "line gone")

    def queue(self, c, line, kind, payload=b""):
        """Send to a line's leg, or keep it until the line attaches."""
        with c.lock:
            leg = c.legs.get(line)
            if leg is None:
                if len(c.pending[line]) >= MAX_PENDING:
                    overflow = True
                else:
                    c.pending[line].append((kind, payload))
                    return
            else:
                overflow = False
        if overflow:
            log.warning("calls: call %s: line %d does not take its data; ending the call", c.id, line)
            self.end(c, "line not attached", line_reason="lost")
            return
        leg.send(kind, payload)

    def from_line(self, c, line, kind, payload):
        if kind == DATA:
            c.last_data = time.monotonic()
        if kind == HANGUP:
            self.hangup_from(c, line, reason_of(payload))
            return
        if kind == CONNECTED:
            c.state = "connected"
        if kind not in (ANSWER, CONNECTED, DATA):
            return
        if c.kind == "ext":
            other = c.answered_line if line == c.caller_line else c.caller_line
            if other is not None:
                self.queue(c, other, kind, payload)
        elif c.remote:
            c.remote.send(kind, payload)

    def from_remote(self, c, kind, payload):
        c.remote.last_rx = time.monotonic()
        if kind == KEEPALIVE or kind == HELLO:
            return
        if kind == HANGUP:
            reason = reason_of(payload)
            self.end(c, f"other side: {reason}", notify_remote=False,
                     line_reason="no_answer" if reason == "no_answer" else reason)
            return
        if c.kind == "in" and c.answered_line is None:
            return                                 # nothing but HANG UP counts until a line here answered
        if kind == ANSWER and c.kind == "in":
            return                                 # only the called side answers
        if kind == DATA:
            c.last_data = time.monotonic()
        if kind == CONNECTED:
            c.state = "connected"
        target = c.local_peer()
        if target is not None:
            self.queue(c, target, kind, payload)

    def hangup_from(self, c, line, reason):
        """A line (or the GUI) ended the call."""
        if c.state == "ended":
            return
        if c.kind == "ext":
            other = c.answered_line if line == c.caller_line else c.caller_line
            if line == c.caller_line and c.state == "ringing":
                self.stop_ringing(c, missed=True)
            if other is not None and other != line:
                self.queue(c, other, HANGUP, json.dumps({"reason": reason}).encode())
            self.end(c, f"line {line}: {reason}", notify_lines=False)
            return
        self.end(c, f"line {line}: {reason}")

    def end(self, c, why, notify_remote=True, notify_lines=True, line_reason="hangup"):
        with c.lock:
            if c.state == "ended":
                return
            was_ringing = c.state in ("setup", "ringing")
            c.state, c.ended_reason = "ended", why
        if c.ring_lines:
            self.stop_ringing(c, missed=was_ringing and c.kind == "in")
        if notify_lines:
            for line, leg in list(c.legs.items()):
                leg.send(HANGUP, json.dumps({"reason": line_reason}).encode())
        if c.remote and notify_remote:
            c.remote.send(HANGUP, json.dumps({"reason": line_reason if line_reason != "hangup" else "hangup"}).encode())
        time.sleep(0.05)
        for leg in list(c.legs.values()):
            leg.close()
        if c.remote:
            c.remote.close()
        if c.slot is not None:
            self.tunnel_down(c.slot)
            self.slots[c.slot] = None
        with self.lock:
            self.calls.pop(c.id, None)
        self.history.appendleft({"kind": c.kind, "number": c.number if c.kind != "in" else None,
                                 "ended": time.time(), "why": why})
        log.info("calls: call %s (%s) ended: %s", c.id, c.kind, why)
        if c.kind in ("out", "in") and not c.reported:
            c.reported = True
            secs = int(time.monotonic() - c.created)
            threading.Thread(target=self.report_end, args=(c.id, why, secs), daemon=True).start()

    def report_end(self, cid, why, secs):
        try:
            self.hub("POST", "/v1/call/end", {"call": cid, "reason": why[:60], "seconds": secs})
        except Exception:  # noqa: BLE001 - the hub frees the pair on its own when this doesn't arrive
            pass

    # ---- calls between boxes
    def free_slot(self, c):
        with self.lock:
            for i in range(MAX_SLOTS):
                if self.slots[i] is None:
                    self.slots[i] = c.id
                    c.slot = i
                    return i
        return None

    def tunnel_up(self, slot, caller, priv, peer_pub, endpoint):
        ns, dev = f"nkc{slot}", f"wgc{slot}"
        me, peer = (CALLER_IP, CALLEE_IP) if caller else (CALLEE_IP, CALLER_IP)
        self.run_cmd("ip", "netns", "del", ns, check=False)
        self.run_cmd("ip", "link", "del", dev, check=False)
        self.run_cmd("ip", "netns", "add", ns)
        self.run_cmd("ip", "-n", ns, "link", "set", "lo", "up")
        self.run_cmd("ip", "link", "add", dev, "type", "wireguard")     # its UDP socket stays in the main namespace
        self.run_cmd("ip", "link", "set", dev, "netns", ns)
        key = os.path.join(self.run_dir, f".wgc-{secrets.token_hex(4)}")
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(priv + "\n")
            self.run_cmd("ip", "netns", "exec", ns, "wg", "set", dev, "private-key", key, "peer", peer_pub,
                         "endpoint", endpoint, "allowed-ips", f"{peer}/32", "persistent-keepalive", "5")
        finally:
            os.unlink(key)
        self.run_cmd("ip", "-n", ns, "addr", "add", f"{me}/30", "dev", dev)
        self.run_cmd("ip", "-n", ns, "link", "set", dev, "mtu", str(WG_MTU), "up")
        rules = f"""table inet netkey_call {{
    chain input {{
        type filter hook input priority filter; policy drop;
        iif "lo" accept
        ct state established,related accept
        iifname "{dev}" ip saddr {peer} tcp dport {CALL_PORT} accept
        counter drop
    }}
    chain forward {{
        type filter hook forward priority filter; policy drop;
    }}
}}
"""
        self.run_cmd("ip", "netns", "exec", ns, "nft", "-f", "-", input_text=rules)

    def tunnel_down(self, slot):
        self.run_cmd("ip", "netns", "del", f"nkc{slot}", check=False)

    def set_endpoint(self, c, relay_host, relay_port, relay=None):
        if c.slot is None:
            return
        try:
            ep = self.d.relay_endpoint(relay_port, relay_host)
            peer = self.run_cmd("ip", "netns", "exec", f"nkc{c.slot}", "wg", "show", f"wgc{c.slot}", "peers").stdout.split()
            if peer:
                self.run_cmd("ip", "netns", "exec", f"nkc{c.slot}", "wg", "set", f"wgc{c.slot}", "peer", peer[0],
                             "endpoint", ep)
                log.info("calls: call %s moved to relay %s", c.id, relay or relay_host or "hub")
        except (RuntimeError, OSError) as e:
            log.warning("calls: cannot move call %s: %s", c.id, e)

    def remote_reader(self, c):
        reader = getattr(c.remote, "reader", None) or FrameReader()
        try:
            for kind, payload in getattr(c.remote, "early", []):
                self.from_remote(c, kind, payload)
            while not c.remote.closed:
                chunk = c.remote.sock.recv(8192)
                if not chunk:
                    break
                for kind, payload in reader.feed(chunk):
                    self.from_remote(c, kind, payload)
        except (OSError, FrameError) as e:
            log.info("calls: call %s: tunnel connection: %s", c.id, e)
        if c.state != "ended":
            self.end(c, "connection lost", notify_remote=False, line_reason="lost")

    def hello(self, c, sock):
        """Exchange HELLO on a fresh tunnel connection; returns the Leg or raises."""
        leg = Leg(sock, "remote")
        sock.settimeout(SETUP_S)
        leg.send(HELLO, json.dumps({"call": c.id, "v": PROTO}).encode())
        reader = FrameReader()
        deadline = time.monotonic() + SETUP_S
        while time.monotonic() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("closed during hello")
            frames = reader.feed(chunk)
            if frames:
                kind, payload = frames[0]
                d = payload_json(payload)
                if kind != HELLO or d.get("call") != c.id or d.get("v") != PROTO:
                    leg.send(HANGUP, json.dumps({"reason": "version"}).encode())
                    raise OSError("bad hello")
                sock.settimeout(None)            # blocking from now on; Leg.close() shuts it down to end a recv
                leg.reader, leg.early = reader, frames[1:]
                return leg
        raise OSError("no hello")

    def dial_out(self, line, number):
        from . import hubclient as hc
        from .party import wg_genkey
        if number == self.number():
            return "busy", None
        c = self.new_call("out", caller_line=line)
        c.number = fmt_number(number)
        priv, pub = wg_genkey()
        try:
            r = self.hub("POST", "/v1/call", {"to": number, "pub": pub, "line": line}, timeout=20)
        except hc.HubError as e:
            self.drop(c)
            log.info("calls: dialing %s: %s", c.number, e)
            return ("no_dialtone" if e.status == 0 else "no_carrier"), None
        result = r.get("result")
        if result != "ringing":
            self.drop(c)
            log.info("calls: %s: %s", c.number, result)
            return (result if result in ("busy", "no_carrier") else "no_carrier"), None
        if not (r.get("call") and wg_key_ok(r.get("peer_pub")) and port_ok(r.get("relay_port"))):
            self.drop(c)
            log.warning("calls: dialing %s: the hub's answer is incomplete", c.number)
            return "no_carrier", None
        old = c.id
        with self.lock:
            self.calls.pop(old, None)
            c.id = str(r["call"])
            self.calls[c.id] = c
        if self.free_slot(c) is None:
            self.end(c, "too many calls at once")
            return "busy", None
        try:
            self.tunnel_up(c.slot, True, priv, str(r["peer_pub"]),
                           self.d.relay_endpoint(r["relay_port"], r.get("relay_host")))
            sock = None
            deadline = time.monotonic() + SETUP_S
            while time.monotonic() < deadline and not c.cancelled:
                s = ns_socket(f"nkc{c.slot}")
                s.settimeout(2)
                try:
                    s.connect((CALLEE_IP, CALL_PORT))
                    sock = s
                    break
                except OSError:
                    s.close()
                    time.sleep(0.3)
            if sock is None:
                raise OSError("the other box could not be reached")
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            c.remote = self.hello(c, sock)
        except Exception as e:  # noqa: BLE001 - whatever went wrong, the call (slot, tunnel) must be torn down
            log.info("calls: call %s to %s: %s", c.id, c.number, e)
            self.end(c, f"setup failed: {e}", line_reason="no_carrier")
            return "no_carrier", None
        if c.cancelled:
            self.end(c, "cancelled")
            return "no_carrier", None
        c.state = "ringing"
        threading.Thread(target=self.remote_reader, args=(c,), daemon=True, name="calls-remote").start()
        log.info("calls: line %d calls %s (call %s, relay %s)", line, c.number, c.id, r.get("relay") or "hub")
        return "ringing", c.id

    def drop(self, c):
        with self.lock:
            self.calls.pop(c.id, None)

    def incoming(self, e):
        """Hub event incoming_call: accept or refuse within 5 s, then set up the tunnel and ring."""
        from . import hubclient as hc
        from .party import wg_genkey
        cid = str(e.get("call") or "")
        if not cid or len(cid) > 64 or cid in self.calls:
            return
        if not wg_key_ok(e.get("peer_pub")):
            log.warning("calls: incoming call %s without a valid key; ignored", cid)
            return
        s = self.settings()
        refuse = None
        if not self.number() or s["incoming"] == "off":
            refuse = "off"
        elif s["dnd"]:
            refuse = "dnd"
        elif s["busy_in_party"] and self.d.party.role in ("host", "member"):
            refuse = "party"
        elif not self.ready_lines():
            refuse = "no ready line"
        elif all(x is not None for x in self.slots):
            refuse = "too many calls"
        if refuse:
            log.info("calls: incoming call refused (%s)", refuse)
            try:
                self.hub("POST", "/v1/call/reject", {"call": cid})
            except hc.HubError:
                pass
            return
        c = self.new_call("in", cid=cid)
        lines = self.ready_lines(exclude=())
        c.ring_lines = set(lines)              # reserved from now on (no RING yet)
        priv, pub = wg_genkey()
        try:
            r = self.hub("POST", "/v1/call/accept", {"call": cid, "pub": pub})
        except hc.HubError as ex:
            log.info("calls: incoming call %s: %s", cid, ex)
            c.ring_lines = set()
            self.drop(c)
            return
        if self.free_slot(c) is None:
            self.end(c, "too many calls at once")
            return
        try:
            self.tunnel_up(c.slot, False, priv, str(e["peer_pub"]),
                           self.d.relay_endpoint(r.get("relay_port", e.get("relay_port")),
                                                 r.get("relay_host", e.get("relay_host"))))
            lst = None
            deadline = time.monotonic() + SETUP_S
            while lst is None and time.monotonic() < deadline:
                try:
                    s2 = ns_socket(f"nkc{c.slot}")
                    s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s2.bind((CALLEE_IP, CALL_PORT))
                    s2.listen(1)
                    lst = s2
                except OSError:
                    time.sleep(0.2)              # the address may need a moment (DAD is off for IPv4, but be safe)
            if lst is None:
                raise OSError("cannot listen in the call namespace")
            lst.settimeout(max(deadline - time.monotonic(), 1))
            try:
                sock, addr = lst.accept()
            finally:
                lst.close()
            if addr[0] != CALLER_IP:
                sock.close()
                raise OSError(f"unexpected connection from {addr[0]}")
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            c.remote = self.hello(c, sock)
        except Exception as ex:  # noqa: BLE001 - whatever went wrong, the call (slot, tunnel) must be torn down
            log.info("calls: incoming call %s: %s", cid, ex)
            self.end(c, f"setup failed: {ex}", line_reason="no_carrier")
            return
        threading.Thread(target=self.remote_reader, args=(c,), daemon=True, name="calls-remote").start()
        lines = [ln for ln in c.ring_lines if ln in self.lines]
        if not lines:
            self.end(c, "no ready line")
            return
        self.start_ringing(c, lines)
        c.remote.send(RINGING)
        log.info("calls: incoming call %s rings line(s) %s", cid, ", ".join(map(str, lines)))

    def hub_event(self, e):
        """Called from the hub event thread. True when the event was a call event (handled here)."""
        t = e.get("type")
        if t == "incoming_call":
            threading.Thread(target=self.incoming, args=(e,), daemon=True, name="calls-in").start()
            return True
        if t == "call_ended":
            c = self.calls.get(str(e.get("call")))
            if c:
                self.end(c, f"other side: {e.get('reason') or 'ended'}", notify_remote=False,
                         line_reason="no_answer" if e.get("reason") == "no_answer" else "hangup")
            return True
        if t == "call_moved":
            c = self.calls.get(str(e.get("call")))
            if c:
                self.set_endpoint(c, e.get("relay_host"), e.get("relay_port"), e.get("relay"))
            return True
        if t == "number_revoked":
            log.info("calls: the hub took this box's number back")
            self.save_phone({})
            return True
        return False

    # ---- housekeeping
    def tick_loop(self):
        while not self.stopping:
            time.sleep(0.5)
            try:
                self.tick()
            except Exception:
                log.exception("calls: tick")

    def tick(self):
        now = time.monotonic()
        for c in list(self.calls.values()):
            if c.state == "ringing" and c.ring_started and now - c.ring_started > RING_MAX_S:
                self.end(c, "no answer", line_reason="no_answer")
            elif c.state == "setup" and c.kind == "ext" and now - c.created > SETUP_S:
                self.end(c, "setup timed out")
            elif c.state == "setup" and now - c.created > 3 * SETUP_S + 30:
                self.end(c, "setup never finished", line_reason="no_carrier")
            elif c.state == "answered" and now - c.created > RING_MAX_S + SETUP_S + ANSWER_CONFIRM_S + 30:
                self.end(c, "answer never confirmed")
            elif c.state == "connected" and (now - c.last_data > IDLE_LIMIT_S or now - c.created > CALL_LIMIT_S):
                self.end(c, "time limit", line_reason="limit")
            elif c.remote and not c.remote.closed:
                if now - c.remote.last_rx > LOST_S:
                    self.end(c, "connection lost (no keepalive)", notify_remote=False, line_reason="lost")
                elif now - c.remote.last_tx > KEEPALIVE_S:
                    c.remote.send(KEEPALIVE)
        with self.lock:
            for ln, info in list(self.lines.items()):
                if now - info["seen"] > 90:          # a session that stopped saying hello
                    self.lines.pop(ln, None)

    def status(self):
        """For the LAN service's status file (the web GUI shows it)."""
        s = self.settings()
        calls = []
        for c in list(self.calls.values()):
            calls.append({"kind": c.kind, "state": c.state, "line": c.local_peer() if c.kind != "ext" else c.caller_line,
                          "ringing": sorted(c.ring_lines), "number": c.number if c.kind != "in" else None,
                          "age": int(time.monotonic() - c.created)})
        return {"number": self.number(), "settings": s, "available": self.presence(), "reason": self.presence_reason(),
                "hub_ok": self.hub_ok, "lines": sorted(self.lines), "ready": self.ready_lines(), "calls": calls}
