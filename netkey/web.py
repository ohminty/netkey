"""Web GUI: HTTPS dashboard, dial-in users and settings, plus the stats collector.

Runs as the netkey user (netkey-web.service, CAP_NET_BIND_SERVICE for ports 80/443).
Dev/demo:  python3 modem.py --web --demo --port 8080   (plain HTTP, simulated lines, login admin / admin)
"""
import collections
import errno
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import fileshare as fs
from . import protostats as ps
from . import status as st
from .config import (MODES, SETTINGS, apply_overrides, box_name, check_setting, clean_box_name, client_ip, get_setting,
                     line_mode)
from .store import Store, read_json, write_atomic, write_json_atomic

log = logging.getLogger("netkey")

WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
VERSION_FILE = os.path.join(os.path.dirname(WEB_ROOT), "VERSION")


_WEB_BUILD = None


def web_build():
    """Short hash of the GUI files as this process loaded them: the page compares it with the one it started with
    and offers a reload after an update (a deploy doesn't always change VERSION)."""
    global _WEB_BUILD
    if _WEB_BUILD is None:
        import hashlib
        h = hashlib.sha256()
        for name in sorted(os.listdir(WEB_ROOT)):
            try:
                with open(os.path.join(WEB_ROOT, name), "rb") as f:
                    h.update(name.encode() + f.read())
            except OSError:
                pass
        _WEB_BUILD = h.hexdigest()[:12]
    return _WEB_BUILD


def undervoltage_file(sys_hwmon="/sys/class/hwmon"):
    """The Pi firmware's undervoltage alarm (1 while the supply is too weak), or None on other hardware."""
    try:
        for h in sorted(os.listdir(sys_hwmon)):
            d = os.path.join(sys_hwmon, h)
            try:
                with open(os.path.join(d, "name")) as f:
                    if f.read().strip() == "rpi_volt" and os.path.exists(os.path.join(d, "in0_lcrit_alarm")):
                        return os.path.join(d, "in0_lcrit_alarm")
            except OSError:
                continue
    except OSError:
        pass
    return None


def clean_file_part(text):
    """A safe piece of a download's file name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or ""))[:40].strip("-.") or "box"


def app_version():
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"
SAMPLE_S = 5
POWER_WARN_S = 24 * 3600           # the dashboard warns about undervoltage seen this recently
DIAG_WAIT = 60                     # seconds the GUI waits for netkey-diag.service's report
UPLOAD_RESERVE = 200 * 1048576     # uploads leave this much free on the memory card
UPDATE_CMD = "curl -fsSL https://raw.githubusercontent.com/ohminty/netkey/main/get.sh | sudo bash"
UPDATE_EVERY_S = 6 * 3600          # the hub is asked about new releases this often (while the GUI is in use)
UPDATE_RETRY_S = 15 * 60           # after a failed check
VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")


def parse_version(v):
    """'0.10.4' -> ((0, 10, 4), (1,)); a pre-release sorts below its release; None for 'dev' and other non-versions."""
    v = str(v or "").strip()
    if not VERSION_RE.match(v):
        return None
    core, _, pre = v.lstrip("v").partition("-")
    nums = tuple(int(x) for x in core.split("."))
    if not pre:
        return nums, (1,)
    return nums, (0,) + tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))


def version_newer(a, b):
    pa, pb = parse_version(a), parse_version(b)
    return bool(pa and pb and pa > pb)
LIVE_POINTS = 15 * 60 // SAMPLE_S
RANGES = {"1h": (3600, 60), "24h": (86400, 600), "7d": (7 * 86400, 3600)}
COOKIE = "netkey_session"
LOGIN_WINDOW_S, LOGIN_MAX_FAILS = 300, 8


def local_midnight(now):
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def host_info():
    info = {"hostname": socket.gethostname(), "uptime_s": None, "load": None, "mem_used_pct": None,
            "cpu_temp_c": None}
    try:
        with open("/proc/uptime") as f:
            info["uptime_s"] = int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    try:
        info["load"] = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
        info["mem_used_pct"] = round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1)
    except (OSError, ValueError, KeyError):
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            info["cpu_temp_c"] = round(int(f.read()) / 1000, 1)
    except (OSError, ValueError):
        pass
    return info


class ChatWatcher(threading.Thread):
    """Holds one long-poll to the hub for the chat room the GUI shows, and buffers what arrives. The browser polls
    its own box every few seconds; this keeps the hub to one waiting request per box instead of a TLS handshake per
    browser poll. Stops 30 s after the last browser poll."""
    IDLE_S, WAIT_S, KEEP = 30, 20, 500

    def __init__(self, app, room):
        super().__init__(daemon=True, name="chat-watch")
        self.app, self.room = app, room
        self.lock = threading.Lock()
        self.msgs, self.deleted, self.meta, self.last = [], set(), {}, 0
        self.touched, self.error = time.time(), None
        self.stopping = threading.Event()
        self.wake = threading.Event()

    def merge(self, d):
        with self.lock:
            by_id = {m["id"]: m for m in self.msgs}
            by_id.update((m["id"], m) for m in d.get("messages", []))      # fresh copies win (e.g. muted flag)
            self.deleted |= set(d.get("deleted") or [])
            self.msgs = [by_id[k] for k in sorted(by_id) if k not in self.deleted][-self.KEEP:]
            self.last = max(self.last, int(d.get("last") or 0))
            self.meta = {k: d.get(k) for k in ("host", "can_post", "why")}
            self.error = None

    def prime(self):
        try:
            self.merge(self.app.hub_call("GET", f"/v1/chat?room={self.room}&after=0"))
        except PartiesError as e:
            self.error = (e.status, str(e))
            if e.status in (403, 404):
                raise

    def run(self):
        while not self.stopping.is_set() and time.time() - self.touched < self.IDLE_S:
            try:
                wait = 0 if self.wake.is_set() else self.WAIT_S
                self.wake.clear()
                self.merge(self.app.hub_call("GET", f"/v1/chat?room={self.room}&after={self.last}&wait={wait}",
                                             timeout=self.WAIT_S + 15))
            except PartiesError as e:
                self.error = (e.status, str(e))
                if e.status in (401, 403, 404):
                    return
                self.stopping.wait(3)

    def nudge(self):
        """Fetch at once (after this box posted, deleted or muted)."""
        self.wake.set()
        try:
            self.merge(self.app.hub_call("GET", f"/v1/chat?room={self.room}&after=0"))
        except PartiesError:
            pass

    def since(self, after):
        self.touched = time.time()
        with self.lock:
            if self.error and not self.msgs and not self.meta:
                raise PartiesError(*self.error)
            return dict(self.meta, room=self.room, last=self.last, deleted=sorted(self.deleted),
                        messages=[m for m in self.msgs if m["id"] > after])

    def stop(self):
        self.stopping.set()


class ApiRefusal(Exception):
    """An API call the box refuses on purpose, with the HTTP status to answer."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class PartiesError(Exception):
    """A public-parties call that failed; status is the HTTP status for the GUI (hub answers keep theirs)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, base_cfg, state_dir, run_dir, store=None):
        self.base = base_cfg
        self.state_dir = state_dir
        self.run_dir = run_dir
        self.store = store or Store(state_dir)
        self.trash_tidied = 0.0                 # last time old trash was emptied (web.py trash_tidy)
        self.diag_asked = 0.0                   # last diagnostics request (one at a time)
        self.power_file = undervoltage_file()
        self.power_seen = None                  # last time the supply was too weak (undervoltage), since start
        self.sessions = {}            # token -> expiry
        self.fails = collections.defaultdict(list)
        self.lock = threading.Lock()
        self.collector = Collector(self)

    # configuration
    def cfg(self):
        return apply_overrides(self.base, self.store.get_settings(), self.store.get_line_modes(), log.warning)

    def max_lines(self):
        return int(self.base["lines"]["max"])

    # sessions
    def login(self, user, password, client):
        now = time.time()
        with self.lock:
            self.fails[client] = [t for t in self.fails[client] if now - t < LOGIN_WINDOW_S]
            if len(self.fails[client]) >= LOGIN_MAX_FAILS:
                return None, (429, "Too many attempts. Try again in a few minutes.")
        if not self.store.check_admin_login(user, password):
            with self.lock:
                self.fails[client].append(now)
            return None, (401, "Wrong username or password.")
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.fails.pop(client, None)
            self.sessions = {t: exp for t, exp in self.sessions.items() if exp > now}
            self.sessions[token] = now + float(self.base["web"]["session_hours"]) * 3600
        return token, None

    def must_change_password(self):
        """The factory login admin / admin must be replaced before anything else (not in the demo)."""
        return not getattr(self, "demo", False) and self.store.admin_is_default()

    def valid(self, token):
        with self.lock:
            exp = self.sessions.get(token or "")
            return exp is not None and exp > time.time()

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token or "", None)

    def logout_others(self, keep):
        """After a password change: every other browser signed in with the old password has to sign in again."""
        with self.lock:
            self.sessions = {t: exp for t, exp in self.sessions.items() if t == keep}

    # live state
    def device_present(self, key):
        return os.path.exists(os.path.join("/dev", key))

    def lines(self, now=None):
        now = now or time.time()
        cfg = self.cfg()
        reg = read_json(os.path.join(self.state_dir, "lines.json"), {}) or {}
        registry, absent_since = reg.get("lines", {}), reg.get("absent_since", {})
        manager_up = st.manager_alive(self.run_dir, now)
        bps = self.collector.latest_bps
        out = []
        for key, n in sorted(registry.items(), key=lambda kv: kv[1]):
            if not 0 <= n < self.max_lines():
                continue
            s = st.read(st.line_file(self.run_dir, n)) or {}
            fresh = manager_up and s and now - s.get("updated", 0) < st.STALE_S
            present = self.device_present(key)
            if fresh:
                state = s.get("state", st.STARTING)
            elif not manager_up:
                state = st.STOPPED
            elif not present:
                state = st.UNPLUGGED
            else:
                state = st.STARTING
            call = s.get("call") if fresh and state == st.CONNECTED else None
            if call:
                call = dict(call)
                call["rx_bps"], call["tx_bps"] = bps.get(n, (0, 0))
                call["duration_s"] = int(now - call.get("started", now))
            out.append({
                "line": n, "adapter": st.adapter_label(key), "device": f"/dev/{key}", "present": present,
                "mode": line_mode(cfg, n), "mode_override": cfg["lines"]["modes"].get(str(n)),
                "running_mode": s.get("mode") if fresh else None,
                "state": state, "state_since": s.get("state_since") if fresh else None,
                "detail": s.get("detail", "") if fresh else "", "baud": s.get("baud") if fresh else None,
                "client_ip": client_ip(cfg, n), "call": call,
                "absent_since": None if present else absent_since.get(key),
            })
        return out

    def lan_status(self):
        from .lan import read_status
        s = read_status()
        if s is None:
            return {"state": "stopped", "detail": "LAN service is not running", "enabled": self.cfg()["lan"]["enabled"],
                    "share_internet": self.cfg()["lan"]["share_internet"], "leases": [], "nic": None}
        if s.get("stale"):
            s["state"], s["detail"] = "stopped", "LAN service is not responding"
        return s

    # LAN party (commands go to the root LAN service through the party-cmd queue)
    party_timeout = 25
    PARTY_CMDS = {"create": "endpoint", "join": "code", "approve": "member", "reject": "member", "kick": "member",
                  "regenerate": None, "endpoint": "endpoint", "leave": None, "close": None, "relays": None,
                  "start": "listing", "watch": "listing", "share_files": None}

    def party_get(self):
        from . import lan
        p = lan.read_party(lan.RUN_DIR)
        if p is None or time.time() - p.get("updated", 0) > 20:
            return {"role": "unknown", "error": "The LAN service is not running", "results": []}
        return p

    def party_command(self, data):
        from . import lan, party as pt
        cmd = str(data.get("cmd", ""))
        if cmd not in self.PARTY_CMDS:
            raise ValueError("unknown LAN party command")
        field = "listing" if cmd == "join" and data.get("listing") else self.PARTY_CMDS[cmd]
        kw = {}
        if field:
            value = str(data.get(field, "")).strip()
            if len(value) > 512 or any(ord(ch) < 32 for ch in value):
                raise ValueError(f"invalid {field}")
            if value or field in ("code", "member", "listing") or cmd == "endpoint":
                kw[field] = value
        if cmd == "create":
            via = str(data.get("via") or "hub")
            if via not in ("hub", "direct"):
                raise ValueError("via must be hub or direct")
            kw["via"] = via
            if data.get("public"):
                if via != "hub" or not isinstance(data["public"], dict) or len(json.dumps(data["public"])) > 4096:
                    raise ValueError("a public party needs the cloud hub and a listing")
                kw["public"] = data["public"]                    # the hub checks every field
        if cmd == "watch":
            kw.update(starts=float(data.get("starts") or 0), title=str(data.get("title") or "")[:40],
                      on=bool(data.get("on", True)))
        if cmd == "share_files":
            kw["on"] = data.get("on") is True
        cid = pt.queue_command(self.state_dir, cmd, **kw)
        result = pt.wait_result(lan.RUN_DIR, cid, timeout=self.party_timeout)
        if result is None:
            return None
        return {"result": result, "party": self.party_get()}

    # public parties: calls that don't change the box's network setup go straight to the hub through the GUI's own
    # client (same box identity as the LAN service, its own token file), so they answer at once
    def hub_client(self):
        from . import hubclient as hc, identity as ident
        cfg = self.cfg()
        lan = cfg["lan"]
        if not lan.get("hub_url") or not lan.get("hub_fingerprint"):
            return None
        key = (lan["hub_url"], lan["hub_fingerprint"])
        with self.lock:
            if getattr(self, "_hub", None) is None or self._hub_key != key:
                self._hub = hc.HubClient(lan["hub_url"], lan["hub_fingerprint"], self.state_dir, box_name(cfg),
                                         identity=ident.try_load(self.state_dir), creds_file=hc.WEB_CREDS_FILE)
                self._hub_key = key
            return self._hub

    def hub_call(self, method, path, body=None, timeout=15):
        from . import hubclient as hc
        client = self.hub_client()
        if client is None:
            raise PartiesError(503, "No cloud hub is configured on this box.")
        try:
            return client.call(method, path, body, timeout=timeout)
        except hc.HubError as e:
            if e.status == 0:
                raise PartiesError(502, f"Cannot reach the cloud hub: {e}") from None
            raise PartiesError(e.status, str(e)) from None

    def parties_me(self):
        """This box's standing in the public part: nickname, ban, and whether public parties are available."""
        try:
            d = self.hub_call("GET", "/v1/me")
        except PartiesError as e:
            if e.status == 404:
                return {"available": False, "reason": "The cloud hub doesn't support public parties yet."}
            if e.status == 503:
                return {"available": False, "reason": str(e)}
            raise
        if not d.get("permanent"):
            return dict(d, available=False, reason="This box has no permanent identity (see journalctl -u netkey-web).")
        return dict(d, available=True, reason=None)

    # ---- update notice: the hub knows the latest release (it reads GitHub); nothing is installed from here
    def update_brief(self):
        """status["update"]: what the last check found; starts a check in the background when one is due."""
        u = getattr(self, "_update", None)
        wait = UPDATE_RETRY_S if not u or u.get("error") else UPDATE_EVERY_S
        with self.lock:
            due = (u is None or time.time() - u["checked"] > wait) and not getattr(self, "_update_busy", False)
            if due:
                self._update_busy = True
        if due:
            threading.Thread(target=self.check_update, daemon=True, name="update-check").start()
        return u

    def system_info(self):
        """Hardware and OS for the hub's box statistics (sent with the update check): Pi model, OS, architecture,
        memory and the retro LAN adapter's driver. Nothing that identifies a person or a device."""
        def first(path):
            try:
                with open(path, "rb") as f:
                    return f.read(200).replace(b"\0", b"").decode(errors="replace").strip()
            except OSError:
                return ""
        osr = {}
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    k, _, v = line.strip().partition("=")
                    osr[k] = v.strip('"')
        except OSError:
            pass
        mem = 0
        try:
            with open("/proc/meminfo") as f:
                mem = int(f.readline().split()[1]) // 1024
        except (OSError, ValueError, IndexError):
            pass
        lan = self.lan_status() or {}
        nic = lan.get("nic") or {}
        return {"model": first("/proc/device-tree/model")[:80], "os": (osr.get("PRETTY_NAME") or "")[:80],
                "arch": os.uname().machine[:20], "ram": str(mem) if mem else "",
                "nic": (nic.get("driver") or ("none" if lan.get("state") in ("no_nic", "disabled") else ""))[:24]}

    # ---- click-to-update: the GUI only asks; netkey-update.service (root) does the work (updater.py)
    def update_job(self):
        """The current or last update job, short (for every status poll)."""
        from . import updater as up
        j = up.read_progress(up.WORK_DIR, self.state_dir)["job"]
        return {k: j.get(k) for k in ("state", "target", "from", "message", "updated")}

    def update_progress(self):
        from . import updater as up
        return up.read_progress(up.WORK_DIR, self.state_dir)

    def request_update(self, data, ip=""):
        """POST /api/update/install {version, password, force}: the admin password again (a sensitive action), and
        only the release this box was told about. Writes the request; nothing else reaches the root updater."""
        from . import updater as up
        if not self.store.check_admin_password(str(data.get("password") or "")):
            log.warning("web: update refused from %s: wrong admin password", ip)
            time.sleep(1)
            raise ApiRefusal(403, "The admin password is wrong.")
        if self.store.admin_is_default():
            raise ApiRefusal(403, "Change the default admin password first (Settings).")
        if not os.path.isdir(up.WORK_DIR):
            raise ApiRefusal(503, "This box can't update itself yet: install this update once with the update command.")
        u = getattr(self, "_update", None) or {}
        version = str(data.get("version") or "")
        if not (u.get("available") and u.get("latest") == version and up.parse_version(version)):
            raise ApiRefusal(409, "That release is not the one offered to this box. Check for updates again.")
        if up.read_progress(up.WORK_DIR, self.state_dir)["job"].get("state") in up.RUNNING:
            raise ApiRefusal(409, "An update is already running.")
        write_json_atomic(os.path.join(self.state_dir, up.REQUEST_FILE),
                          {"version": version, "force": data.get("force") is True, "requested": time.time(),
                           "by": self.store.admin_user()}, mode=0o640)
        log.warning("web: admin %s (%s) asked to update to NetKey %s%s", self.store.admin_user(), ip, version,
                    " (during a party or call)" if data.get("force") is True else "")
        return self.update_progress()

    def check_update(self):
        """Ask the hub which release is current (the box's version goes along, for the hub's version count)."""
        from urllib.parse import quote
        cur = app_version()
        prev = getattr(self, "_update", None) or {}
        u = {"current": cur, "checked": time.time(), "command": UPDATE_CMD, "error": None,
             "latest": prev.get("latest"), "url": prev.get("url"), "notes": prev.get("notes"),
             "published": prev.get("published"), "required": prev.get("required", False)}
        try:
            info = "".join(f"&{k}={quote(v)}" for k, v in self.system_info().items() if v)
            d = self.hub_call("GET", f"/v1/release?v={quote(cur)}{info}", timeout=20)
            u.update(latest=d.get("latest"), url=d.get("url"), notes=(d.get("notes") or "")[:2000],
                     published=d.get("published"), required=bool(d.get("required")))
        except PartiesError as e:
            u["error"] = ("The cloud hub doesn't announce updates yet." if e.status == 404 else str(e))
        except Exception as e:                       # never let a check break the status
            u["error"] = f"Update check failed: {e}"
        u["available"] = version_newer(u["latest"], cur)
        u["required"] = u["required"] and u["available"]
        if u["available"] and not prev.get("available"):
            log.info("web: NetKey %s is available (this box runs %s)", u["latest"], cur)
        self._update = u
        self._update_busy = False
        return u

    def parties_nick_check(self, nick):
        return self.hub_call("POST", "/v1/nick/check", {"nick": str(nick or "")[:32]})

    def parties_set_nick(self, nick):
        self.hub_call("POST", "/v1/nick", {"nick": str(nick or "")[:32]})
        return self.parties_me()

    def cached(self, key, ttl, fn):
        """Hub answers kept for `ttl` s: every call is a new TLS connection to the hub, so polls are served here."""
        now = time.time()
        with self.lock:
            hit = getattr(self, "_cache", {}).get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = fn()
        with self.lock:
            self._cache = getattr(self, "_cache", {})
            self._cache[key] = (now, value)
        return value

    def forget(self, *keys):
        with self.lock:
            for k in keys:
                getattr(self, "_cache", {}).pop(k, None)

    def parties_directory(self):
        return self.cached("directory", 10, lambda: self.hub_call("GET", "/v1/directory"))

    def parties_games(self):
        return self.cached("games", 86400, lambda: self.hub_call("GET", "/v1/games"))

    def parties_mine(self):
        return self.cached("mine", 5, lambda: self.hub_call("GET", "/v1/mine"))

    @staticmethod
    def listing_ref(ref):
        ref = str(ref or "").strip()
        if not re.fullmatch(r"[0-9A-Za-z-]{8,12}", ref):
            raise PartiesError(404, "no such party")
        return ref

    def parties_listing(self, ref):
        return self.hub_call("GET", f"/v1/listing/{self.listing_ref(ref)}")

    def parties_create_listing(self, body):
        out = self.hub_call("POST", "/v1/listing", body)
        self.forget("directory", "mine")
        return out

    LISTING_ACTIONS = ("edit", "cancel", "rsvp", "preapprove", "decline", "vote", "settings")

    def parties_listing_action(self, ref, action, body):
        """RSVP, votes and the host's edits. An RSVP with "join automatically" also tells the LAN service to watch
        for the start (it holds the hub's event channel around the start time and joins)."""
        from . import party as pt
        if action not in self.LISTING_ACTIONS:
            raise PartiesError(404, "not found")
        if action == "settings":
            body = {"chat": bool(body.get("chat", True))}        # auto-approve and sharing come from the LAN service
        out = self.hub_call("POST", f"/v1/listing/{self.listing_ref(ref)}/{action}", body)
        self.forget("directory", "mine")
        if action == "rsvp":
            going, auto = bool(body.get("going", True)), bool(body.get("autojoin", True))
            pt.queue_command(self.state_dir, "watch", listing=out["id"], starts=out.get("starts") or 0,
                             title=out.get("title", ""), on=going and auto)
        if action == "cancel":
            return {"ok": True}
        return out

    def parties_chat(self, room, after):
        """Chat for the GUI: one watcher per box long-polls the hub for the room on screen (see ChatWatcher)."""
        room = str(room or "")
        if not re.fullmatch(r"[lp]:[0-9a-f]{12}", room):
            raise PartiesError(404, "no such chat")
        with self.lock:
            w = getattr(self, "_chat", None)
            fresh = not w or w.room != room or not w.is_alive()
            if fresh:
                if w:
                    w.stop()
                w = self._chat = ChatWatcher(self, room)
        if fresh:
            w.prime()
            w.start()
        return w.since(int(after or 0))

    def parties_chat_post(self, body):
        out = self.hub_call("POST", "/v1/chat", {"room": str(body.get("room") or ""), "text": str(body.get("text") or "")[:300]})
        with self.lock:
            w = getattr(self, "_chat", None)
        if w:
            w.nudge()
        return out

    def parties_chat_action(self, action, body):
        if action not in ("delete", "mute"):
            raise PartiesError(404, "not found")
        out = self.hub_call("POST", f"/v1/chat/{action}", {"room": str(body.get("room") or ""), "id": int(body.get("id") or 0),
                                                           "on": bool(body.get("on", True))})
        with self.lock:
            w = getattr(self, "_chat", None)
        if w:
            w.nudge()
        return out

    def parties_report(self, body):
        keep = {k: body[k] for k in ("room", "id", "listing", "text") if k in body}
        return self.hub_call("POST", "/v1/report", keep)

    def parties_ics(self, ref):
        """A calendar file for a planned event, built here (no web page involved)."""
        lst = self.parties_listing(ref)
        if not lst.get("starts"):
            raise PartiesError(404, "this party has no start time")
        stamp = lambda t: time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t))
        esc = lambda s: str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
        games = ", ".join(g["name"] for g in lst.get("games", []))
        desc = (f"{lst.get('description', '')}\n\nGames: {games}\nHost: {lst.get('host')}\n"
                f"Event ID {lst.get('code')}: open Parties on your NetKey box to join.")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//NetKey//Public parties//EN", "BEGIN:VEVENT",
                 f"UID:{lst['id']}@netkey", f"DTSTAMP:{stamp(time.time())}", f"DTSTART:{stamp(lst['starts'])}",
                 f"DTEND:{stamp(lst['starts'] + 4 * 3600)}", f"SUMMARY:{esc(lst['title'])} (NetKey LAN party)",
                 f"DESCRIPTION:{esc(desc)}"]
        if lst.get("repeat") in ("weekly", "monthly"):
            lines.append(f"RRULE:FREQ={lst['repeat'].upper()}")
        lines += ["END:VEVENT", "END:VCALENDAR", ""]
        name = re.sub(r"[^a-z0-9]+", "-", lst["title"].lower()).strip("-") or "netkey-party"
        return "\r\n".join(lines).encode(), f"{name}.ics"

    # network uplink (Wi-Fi, built-in Ethernet port, rescue hotspot); commands share the party-cmd queue
    NET_CMDS = {"wifi_scan": 40, "wifi_connect": 75, "wifi_forget": 20}

    def net_get(self):
        from . import lan
        n = lan.read_net(lan.RUN_DIR)
        if n is None or time.time() - n.get("updated", 0) > 20:
            return {"error": "The LAN service is not running", "wifi_available": False}
        return n

    def net_brief(self):
        """Uplink summary for /api/status (dashboard): no hotspot password, no scan list."""
        n = self.net_get()
        if n.get("error"):
            return None
        wifi, eth, hs = n.get("wifi") or {}, n.get("ethernet") or {}, n.get("hotspot") or {}
        return {"mode": n.get("mode"), "uplink_ok": n.get("uplink_ok"), "uplink_dev": n.get("uplink_dev"),
                "wifi_available": n.get("wifi_available"),
                "wifi": {k: wifi.get(k) for k in ("state", "connection", "ip", "ssid", "signal", "band")}
                if wifi else None,
                "ethernet": {k: eth.get(k) for k in ("role", "ip")} if eth else None,
                "hotspot": {k: hs.get(k) for k in ("enabled", "state", "ssid", "clients")}}

    def net_args(self, data):
        cmd = str(data.get("cmd", ""))
        if cmd not in self.NET_CMDS:
            raise ValueError("unknown network command")
        kw = {}
        if cmd == "wifi_forget" and data.get("ssid"):
            ssid = str(data["ssid"])
            if len(ssid.encode()) > 32 or any(ord(ch) < 32 for ch in ssid):
                raise ValueError("invalid ssid")
            kw["ssid"] = ssid
        if cmd == "wifi_connect":
            for field, limit in (("ssid", 32), ("password", 63), ("country", 2)):
                value = str(data.get(field) or "")
                if len(value.encode()) > limit or any(ord(ch) < 32 for ch in value):
                    raise ValueError(f"invalid {field}")
                kw[field] = value
        return cmd, kw

    def net_command(self, data):
        from . import lan, party as pt
        cmd, kw = self.net_args(data)
        cid = pt.queue_command(self.state_dir, cmd, **kw)
        result = pt.wait_result(lan.RUN_DIR, cid, timeout=self.NET_CMDS[cmd])
        if result is None:
            return None
        return {"result": result, "network": self.net_get()}

    # phone numbers and modem calls (calls.py): the LAN service holds the number and the calls; the GUI keeps the
    # phone book (phone-book.json, read by the LAN service for "phone book only") and asks the hub for presence
    PHONE_CMDS = {"get_number": ("phone_get", 25), "release": ("phone_release", 25), "hangup": ("phone_hangup", 8),
                  "refresh": ("phone_refresh", 25)}

    def phone_book(self):
        from . import calls as cl
        rows = read_json(os.path.join(self.state_dir, cl.BOOK_FILE), []) or []
        return [r for r in rows if isinstance(r, dict) and r.get("number")][:200]

    def set_phone_book(self, rows, check_only=False):
        from . import calls as cl
        if not isinstance(rows, list) or len(rows) > 200:
            raise ValueError("the phone book holds up to 200 entries")
        out, seen = [], set()
        for r in rows:
            if not isinstance(r, dict):
                raise ValueError("bad phone book entry")
            n = cl.digits(r.get("number"))[-7:]
            if cl.classify(n) != ("number", n):
                raise ValueError(f"{r.get('number')!r} is not a NetKey number (555-1000 to 555-9999)")
            if n in seen:
                raise ValueError(f"{cl.fmt_number(n)} is in the phone book twice")
            seen.add(n)
            name = " ".join(str(r.get("name") or "").split())[:32]
            out.append({"name": name, "number": n})
        if check_only:
            return out
        write_json_atomic(os.path.join(self.state_dir, cl.BOOK_FILE), out, mode=0o644)
        return out

    def phone_get(self):
        from . import lan
        st = lan.read_status() or {}
        cfg = self.cfg()
        return {"phone": st.get("phone"), "lan_running": bool(st), "book": self.phone_book(),
                "settings": dict(cfg.get("phone") or {}), "hub": bool(cfg["lan"].get("hub_fingerprint"))}

    def phone_command(self, data):
        from . import lan, party as pt
        what = str(data.get("cmd") or "")
        if what not in self.PHONE_CMDS:
            raise ValueError("unknown phone command")
        cmd, timeout = self.PHONE_CMDS[what]
        kw = {"line": int(data.get("line", -1))} if what == "hangup" else {}
        cid = pt.queue_command(self.state_dir, cmd, **kw)
        result = pt.wait_result(lan.RUN_DIR, cid, timeout=timeout)
        if result is None:
            return None
        return {"result": result, **self.phone_get()}

    def phone_presence(self):
        """available / busy / unreachable for the phone book's numbers (only what the hub lets this box see)."""
        nums = [r["number"] for r in self.phone_book()]
        if not nums:
            return {"numbers": {}}
        return self.hub_call("POST", "/v1/numbers/presence", {"numbers": nums})

    def phone_lookup(self, n):
        from . import calls as cl
        n = cl.digits(n)[-7:]
        if cl.classify(n) != ("number", n):
            raise ValueError("not a NetKey number (555-1000 to 555-9999)")
        return self.hub_call("GET", f"/v1/numbers/lookup?n={n}")

    def calls(self, limit):
        """Call log; ongoing calls get their live byte counters from the line status."""
        calls = self.store.list_calls(limit)
        live = {l["line"]: l["call"] for l in self.lines() if l["call"]}
        for c in calls:
            call = live.get(c["line"])
            if c["ended"] is None and call and abs(call.get("started", 0) - c["started"]) < 2:
                c["rx_bytes"], c["tx_bytes"] = call.get("rx_bytes", 0), call.get("tx_bytes", 0)
                c["user"] = c["user"] or call.get("user")
        return calls

    def status(self):
        now = time.time()
        cfg = self.cfg()
        lines = self.lines(now)
        midnight = local_midnight(now)
        today = self.store.call_counts(midnight)
        traffic = self.store.stats_bytes_since(midnight)
        pending = self.collector.pending_bytes()
        rx_bps = sum((l["call"] or {}).get("rx_bps", 0) for l in lines)
        tx_bps = sum((l["call"] or {}).get("tx_bps", 0) for l in lines)
        lan_pcs_bps, lan_party_bps = self.collector.lan_bps()
        lan_today = self.store.proto_dir_bytes_since(midnight)
        lan_today_bytes = lan_today["lan"] + lan_today["party"] + sum(
            a[1] for (d, _), a in list(self.collector.proto_acc.items()))
        return {
            "time": now,
            "service": {"manager": "running" if st.manager_alive(self.run_dir, now) else "stopped",
                        "demo": getattr(self, "demo", False)},
            "summary": {
                "lines_max": self.max_lines(), "lines_registered": len(lines),
                "lines_present": sum(1 for l in lines if l["present"]),
                "connected": sum(1 for l in lines if l["state"] == st.CONNECTED),
                "busy": sum(1 for l in lines if l["state"] in st.BUSY_STATES),
                "rx_bps": rx_bps, "tx_bps": tx_bps, "default_mode": cfg["lines"]["default_mode"],
                "lan_pcs_bps": lan_pcs_bps, "lan_party_bps": lan_party_bps, "lan_bytes_today": lan_today_bytes,
                "calls_today": today["n"], "failed_logins_today": today["f"],
                "bytes_today": traffic["rx"] + traffic["tx"] + pending,
            },
            "lines": lines,
            "host": host_info(),
            "users_sync": self.store.users_sync_state(),
            "admin": {"username": self.store.admin_user(), "default_credentials": self.store.admin_is_default()},
            "version": app_version(), "web_build": web_build(),
            "power": {"undervoltage_seen": self.power_seen if self.power_seen and now - self.power_seen < POWER_WARN_S
                      else None},
            "box_name": box_name(cfg),
            "lan": self.lan_status(),
            "network": self.net_brief(),
            "update": self.update_brief(), "update_job": self.update_job(),
        }

    def protocols(self, rng):
        """Retro-LAN traffic by protocol: totals over the range, current rates, IPX frame types seen."""
        totals = collections.defaultdict(lambda: [0, 0])
        if rng == "live":
            for _, d in list(self.collector.proto_ring):
                for key, (p, b) in d.items():
                    totals[key][0] += p
                    totals[key][1] += b
        else:
            span, _ = RANGES[rng]
            for r in self.store.proto_totals_since(time.time() - span):
                totals[(r["dir"], r["proto"])] = [r["packets"], r["bytes"]]
            for key, (p, b) in list(self.collector.proto_acc.items()):     # the minute being collected
                totals[key][0] += p
                totals[key][1] += b
        ring = list(self.collector.proto_ring)
        last = ring[-1][1] if ring and time.time() - ring[-1][0] < 3 * SAMPLE_S else {}
        rows = []
        for key, label, group in ps.PROTOCOLS:
            row = {"key": key, "label": label, "group": group}
            for d in ps.DIRS:
                p, b = totals.get((d, key), [0, 0])
                lp, lb = last.get((d, key), (0, 0))
                row[d] = {"packets": p, "bytes": b, "pps": round(lp / SAMPLE_S, 1), "bps": round(lb / SAMPLE_S, 1)}
            rows.append(row)
        seen, mixed = ps.ipx_mismatch({r["key"]: r["lan"]["packets"] + r["party"]["packets"] for r in rows})
        lan = self.lan_status()
        return {"range": rng, "available": lan.get("state") == "up" and self.collector.proto_prev is not None,
                "role": lan.get("role"), "protocols": rows, "ipx_frame_types": seen, "ipx_mixed": mixed}

    def stats(self, rng):
        if rng == "live":
            pts = list(self.collector.ring)
            per_line = {}
            for i, p in enumerate(pts):
                for n, (rx, tx) in p["lines"].items():
                    d = per_line.setdefault(str(n), {"rx_bps": [0] * len(pts), "tx_bps": [0] * len(pts)})
                    d["rx_bps"][i], d["tx_bps"][i] = rx, tx
            return {"range": "live", "step": SAMPLE_S, "t": [p["t"] for p in pts],
                    "connected": [p["connected"] for p in pts], "present": [p["present"] for p in pts],
                    "rx_bps": [p["rx_bps"] for p in pts], "tx_bps": [p["tx_bps"] for p in pts],
                    "per_line": per_line, **self.lan_series_live(pts)}
        span, step = RANGES[rng]
        now = time.time()
        end = int(now // step) * step
        start = end - span + step
        buckets = list(range(start, end + step, step))
        index = {b: i for i, b in enumerate(buckets)}
        n = len(buckets)
        have = [False] * n
        rx, tx = [0] * n, [0] * n
        minute_conn = collections.defaultdict(lambda: collections.defaultdict(int))
        minute_pres = collections.defaultdict(lambda: collections.defaultdict(int))
        per_line = {}
        rows = self.store.stats_since(start)
        if self.collector.minute is not None:     # include the minute being collected right now
            rows += [{"ts": self.collector.minute, "line": n, "present": a[0], "connected": a[1],
                      "rx_bytes": a[2], "tx_bytes": a[3]} for n, a in list(self.collector.acc.items())]
        for r in rows:
            b = r["ts"] // step * step
            i = index.get(b)
            if i is None:
                continue
            have[i] = True
            rx[i] += r["rx_bytes"]
            tx[i] += r["tx_bytes"]
            minute_conn[b][r["ts"]] += r["connected"]
            minute_pres[b][r["ts"]] += r["present"]
            d = per_line.setdefault(str(r["line"]), {"rx_bps": [0] * n, "tx_bps": [0] * n})
            d["rx_bps"][i] += r["rx_bytes"]
            d["tx_bps"][i] += r["tx_bytes"]
        for d in per_line.values():
            d["rx_bps"] = [round(v / step, 1) for v in d["rx_bps"]]
            d["tx_bps"] = [round(v / step, 1) for v in d["tx_bps"]]
        return {
            "range": rng, "step": step, "t": buckets,
            "connected": [max(minute_conn[b].values()) if have[i] else None for i, b in enumerate(buckets)],
            "present": [max(minute_pres[b].values()) if have[i] else None for i, b in enumerate(buckets)],
            "rx_bps": [round(rx[i] / step, 1) if have[i] else None for i in range(n)],
            "tx_bps": [round(tx[i] / step, 1) if have[i] else None for i in range(n)],
            "per_line": per_line,
            **self.lan_series(start, step, buckets, index),
        }

    def lan_series_live(self, pts):
        by_t = dict(self.collector.proto_ring)
        pcs, party = [], []
        for p in pts:
            d = by_t.get(p["t"])
            if d is None:
                pcs.append(None)
                party.append(None)
                continue
            pcs.append(round(sum(b for (k, _), (_, b) in d.items() if k == "lan") / SAMPLE_S, 1))
            party.append(round(sum(b for (k, _), (_, b) in d.items() if k != "lan") / SAMPLE_S, 1))
        return {"lan_pcs_bps": pcs, "lan_party_bps": party}

    def lan_series(self, start, step, buckets, index):
        """Retro-LAN bytes/s per bucket from the stored protocol counters (rows only exist for minutes with frames)."""
        pcs, party = [0] * len(buckets), [0] * len(buckets)
        rows = [(r["ts"], r["dir"], r["bytes"] or 0) for r in self.store.proto_dir_series(start)]
        if self.collector.minute is not None:
            rows += [(self.collector.minute, d, a[1]) for (d, _), a in list(self.collector.proto_acc.items())]
        for ts, d, b in rows:
            i = index.get(ts // step * step)
            if i is not None:
                (pcs if d == "lan" else party)[i] += b
        return {"lan_pcs_bps": [round(v / step, 1) for v in pcs], "lan_party_bps": [round(v / step, 1) for v in party]}

    def settings(self):
        cfg = self.cfg()
        overrides = self.store.get_settings()
        schema = []
        for key, kind, label, help_, mode, extra in SETTINGS:
            item = {"key": key, "type": kind, "label": label, "help": help_, "mode": mode,
                    "group": key.split(".")[0] if key != "lines.default_mode" else "mode"}
            item.update(extra)
            if key == "box.name":
                item["placeholder"] = clean_box_name(socket.gethostname())   # what an empty name means
            schema.append(item)
        return {
            "schema": schema,
            "values": {s[0]: get_setting(cfg, s[0]) for s in SETTINGS},
            "defaults": {s[0]: get_setting(self.base, s[0]) for s in SETTINGS},
            "overridden": sorted(k for k in overrides if k in {s[0] for s in SETTINGS}),
            "line_modes": cfg["lines"]["modes"],
            "readonly": {
                "lines.max": self.max_lines(), "ppp.server_ip": self.base["ppp"]["server_ip"],
                "ppp.first_client_ip": self.base["ppp"]["first_client_ip"],
                "web.port": self.base["web"]["port"],
            },
        }

    def update_settings(self, values):
        if not isinstance(values, dict) or not values:
            raise ValueError("nothing to save")
        clean = {}
        for key, value in values.items():
            if value is None:
                clean[key] = None
                continue
            value = check_setting(key, value)
            clean[key] = None if value == get_setting(self.base, key) else value
        self.store.set_settings(clean)


class Collector(threading.Thread):
    """Samples line traffic every 5 s: live ring (15 min) + per-minute rows in the stats table."""

    def __init__(self, app):
        super().__init__(daemon=True, name="collector")
        self.app = app
        self.ring = collections.deque(maxlen=LIVE_POINTS)
        self.prev = {}                # line -> (call started, rx, tx, t)
        self.latest_bps = {}
        self.minute = None
        self.acc = {}                 # line -> [present, connected, rx, tx]
        self.stopping = threading.Event()
        self.last_prune = 0
        self.proto_prev = None        # (counter table creation time, counters) from the LAN service
        self.proto_acc = {}           # (dir, proto) -> [packets, bytes] for the current minute
        self.proto_ring = collections.deque(maxlen=LIVE_POINTS)   # (t, {(dir, proto): (packets, bytes)})

    def run(self):
        while not self.stopping.wait(SAMPLE_S - time.time() % SAMPLE_S):
            try:
                self.sample(time.time())
            except Exception:
                log.exception("collector: sample failed")

    def pending_bytes(self):
        return sum(a[2] + a[3] for a in list(self.acc.values()))

    def lan_bps(self):
        """(from this box's PCs, from the party) retro-LAN bytes/s over the latest sample."""
        ring = self.proto_ring
        if not ring or time.time() - ring[-1][0] >= 3 * SAMPLE_S:
            return 0.0, 0.0
        pcs = party = 0
        for (d, _), (_, b) in ring[-1][1].items():
            if d == "lan":
                pcs += b
            else:
                party += b
        return round(pcs / SAMPLE_S, 1), round(party / SAMPLE_S, 1)

    def sample(self, now):
        minute = int(now // 60 * 60)
        if self.minute is not None and minute != self.minute:
            self.flush()
        self.minute = minute
        lines = self.app.lines(now)
        point = {"t": int(now), "connected": 0, "present": 0, "rx_bps": 0.0, "tx_bps": 0.0, "lines": {}}
        bps = {}
        for l in lines:
            n = l["line"]
            acc = self.acc.setdefault(n, [0, 0, 0, 0])
            if l["present"]:
                acc[0] = 1
                point["present"] += 1
            call = l["call"]
            if not call:
                self.prev.pop(n, None)
                continue
            acc[1] = 1
            point["connected"] += 1
            rx, tx = int(call.get("rx_bytes", 0)), int(call.get("tx_bytes", 0))
            prev = self.prev.get(n)
            if prev and prev[0] == call.get("started") and rx >= prev[1] and tx >= prev[2]:
                drx, dtx, dt = rx - prev[1], tx - prev[2], max(now - prev[3], 0.5)
            else:
                drx, dtx, dt = rx, tx, max(now - call.get("started", now - SAMPLE_S), SAMPLE_S)
            self.prev[n] = (call.get("started"), rx, tx, now)
            acc[2] += drx
            acc[3] += dtx
            bps[n] = (round(drx / dt, 1), round(dtx / dt, 1))
            point["rx_bps"] += bps[n][0]
            point["tx_bps"] += bps[n][1]
            point["lines"][n] = bps[n]
        self.latest_bps = bps
        point["rx_bps"], point["tx_bps"] = round(point["rx_bps"], 1), round(point["tx_bps"], 1)
        self.ring.append(point)
        self.sample_protocols(now)
        self.sample_power(now)

    def sample_power(self, now):
        f = self.app.power_file
        if not f:
            return
        try:
            with open(f) as fh:
                if fh.read().strip() == "1":
                    if self.app.power_seen is None or now - self.app.power_seen > 3600:
                        log.warning("power: the Pi reports undervoltage (power supply too weak; can corrupt the SD card)")
                    self.app.power_seen = now
        except OSError:
            pass

    def sample_protocols(self, now):
        from .lan import read_status
        s = read_status() or {}
        cur, since = s.get("proto"), s.get("proto_since")
        if not cur or s.get("stale"):
            return
        prev = self.proto_prev
        self.proto_prev = (since, cur)
        if prev is None:
            return                    # first reading after start: earlier growth is already in the DB
        d = ps.deltas(prev[1] if prev[0] == since else None, cur)
        for key, (p, b) in d.items():
            acc = self.proto_acc.setdefault(key, [0, 0])
            acc[0] += p
            acc[1] += b
        self.proto_ring.append((int(now), d))

    def flush(self):
        if self.acc and self.minute is not None:
            self.app.store.add_stats(self.minute, [(n, *a) for n, a in self.acc.items()])
        self.acc = {}
        if self.proto_acc and self.minute is not None:
            self.app.store.add_proto_stats(self.minute, [(d, k, p, b) for (d, k), (p, b) in self.proto_acc.items()])
        self.proto_acc = {}
        if time.time() - self.last_prune > 3600:
            self.app.store.prune(int(self.app.base["web"]["stats_days"]))
            self.last_prune = time.time()


def make_handler(app, secure):
    class Handler(BaseHTTPRequestHandler):
        server_version = "netkey"
        sys_version = ""
        timeout = 30
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("web %s " + fmt, self.client_address[0], *args)

        # responses
        def send(self, code, body=b"", ctype="application/json", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if secure:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, code, data, headers=None):
            body = json.dumps(data, separators=(",", ":")).encode()
            self.send(code, body, "application/json", dict({"Cache-Control": "no-store"}, **(headers or {})))

        def error(self, code, message):
            self.json(code, {"error": message})

        def token(self):
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
            return None

        def read_body(self):
            """Always consume the request body, so a kept-alive connection stays in sync."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n > 65536:
                self.close_connection = True
                self.raw_body = None
                return
            self.raw_body = self.rfile.read(n) if n > 0 else b""

        def body(self):
            raw = self.raw_body
            if raw is None:
                raise ValueError("request too large")
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise ValueError("invalid JSON") from None
            if not isinstance(data, dict):
                raise ValueError("JSON object expected")
            return data

        # dispatch
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def do_PUT(self):
            self.route("PUT")

        def do_DELETE(self):
            self.route("DELETE")

        def route(self, method):
            url = urlsplit(self.path)
            path = url.path
            if method == "PUT" and path == "/api/files/upload":
                return self.upload(parse_qs(url.query))      # streamed: the body is the file
            self.read_body()
            try:
                if not path.startswith("/api/"):
                    if method in ("GET", "HEAD"):
                        return self.static(path)
                    return self.error(405, "method not allowed")
                if method != "GET" and self.headers.get("X-Requested-With") != "netkey":
                    return self.error(403, "missing X-Requested-With header")
                if path == "/api/session" and method == "GET":
                    authed = app.valid(self.token())
                    return self.json(200, {"authenticated": authed, "hostname": socket.gethostname(),
                                           "demo": getattr(app, "demo", False),
                                           "must_change_password": authed and app.must_change_password()})
                if path == "/api/login" and method == "POST":
                    return self.api_login()
                if not app.valid(self.token()):
                    return self.error(401, "login required")
                return self.api(method, path, parse_qs(url.query))
            except ApiRefusal as e:
                return self.error(e.status, str(e))
            except ValueError as e:
                return self.error(400, str(e))
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError, socket.timeout):
                raise
            except Exception:
                log.exception("web: %s %s failed", method, path)
                return self.error(500, "internal error (see journalctl -u netkey-web)")

        def api_login(self):
            data = self.body()
            token, err = app.login(str(data.get("username", "")), str(data.get("password", "")),
                                   self.client_address[0])
            if not token:
                return self.error(*err)
            cookie = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
            if secure:
                cookie += "; Secure"
            self.json(200, {"ok": True, "must_change_password": app.must_change_password()}, {"Set-Cookie": cookie})

        def api(self, method, path, query):
            parts = [unquote(p) for p in path.strip("/").split("/")[1:]]
            res = parts[0] if parts else ""
            arg = parts[1] if len(parts) > 1 else None
            if res == "logout" and method == "POST":
                app.logout(self.token())
                return self.json(200, {"ok": True},
                                 {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
            if res != "password" and app.must_change_password():
                return self.json(403, {"error": "Set a new admin password first.", "code": "password_change_required"})
            if res == "status" and method == "GET":
                return self.json(200, app.status())
            if res == "stats" and method == "GET":
                rng = (query.get("range") or ["live"])[0]
                if rng != "live" and rng not in RANGES:
                    raise ValueError("range must be live, 1h, 24h or 7d")
                return self.json(200, app.stats(rng))
            if res == "protocols" and method == "GET":
                rng = (query.get("range") or ["live"])[0]
                if rng != "live" and rng not in RANGES:
                    raise ValueError("range must be live, 1h, 24h or 7d")
                return self.json(200, app.protocols(rng))
            if res == "calls" and method == "GET":
                limit = min(max(int((query.get("limit") or ["100"])[0]), 1), 1000)
                return self.json(200, {"calls": app.calls(limit)})
            if res == "users":
                return self.api_users(method, arg)
            if res == "files":
                return self.api_files(method, arg, query)
            if res == "backup" and method == "POST":
                return self.api_backup(arg)
            if res == "diagnostics" and method == "POST":
                return self.api_diagnostics()
            if res == "settings":
                if method == "GET":
                    return self.json(200, app.settings())
                if method == "PUT":
                    app.update_settings(self.body().get("values"))
                    return self.json(200, app.settings())
            if res == "lines" and arg is not None and method == "DELETE":
                if not arg.isdigit():
                    return self.error(404, "no such line")
                from .manager import Registry
                key = Registry(os.path.join(app.state_dir, "lines.json"), app.max_lines()).forget(int(arg), app.device_present)
                if key is None:
                    return self.error(409, "Only an unplugged adapter's line can be forgotten.")
                app.store.set_line_mode(int(arg), None)
                log.info("web: line %s forgotten (adapter %s)", arg, key)
                return self.json(200, {"ok": True})
            if res == "lines" and arg is not None and method == "PUT":
                if not arg.isdigit() or not 0 <= int(arg) < app.max_lines():
                    return self.error(404, "no such line")
                mode = self.body().get("mode")
                if mode not in ("default",) + MODES:
                    raise ValueError("mode must be default, virtual or physical")
                app.store.set_line_mode(int(arg), None if mode == "default" else mode)
                return self.json(200, {"ok": True, "line_modes": app.cfg()["lines"]["modes"]})
            if res == "party":
                if method == "GET":
                    return self.json(200, app.party_get())
                if method == "POST":
                    out = app.party_command(self.body())
                    if out is None:
                        return self.error(504, "The LAN service did not answer. Is netkey-lan running?")
                    return self.json(200 if out["result"]["ok"] else 400,
                                     dict(out, error=None if out["result"]["ok"] else out["result"]["message"]))
            if res == "parties":
                return self.api_parties(method, parts[1:], query)
            if res == "phone":
                return self.api_phone(method, arg, query)
            if res == "update" and arg == "check" and method == "POST":
                return self.json(200, app.check_update())
            if res == "update" and arg == "install" and method == "POST":
                return self.json(200, app.request_update(self.body(), self.client_address[0]))
            if res == "update" and arg == "progress" and method == "GET":
                return self.json(200, app.update_progress())
            if res == "network":
                if method == "GET":
                    return self.json(200, app.net_get())
                if method == "POST":
                    out = app.net_command(self.body())
                    if out is None:
                        return self.error(504, "The LAN service did not answer. Is netkey-lan running?")
                    return self.json(200 if out["result"]["ok"] else 400,
                                     dict(out, error=None if out["result"]["ok"] else out["result"]["message"]))
            if res == "password" and method == "PUT":
                data = self.body()
                if not app.store.check_admin_password(str(data.get("current", ""))):
                    return self.error(403, "Current password is wrong.")
                user, new = data.get("username"), str(data.get("new") or "")
                if not new and app.must_change_password():
                    raise ValueError("choose a new password (the default admin password must be replaced)")
                if not new and (user is None or user == app.store.admin_user()):
                    raise ValueError("nothing to change")
                if new:
                    app.store.set_admin_password(new)        # validates first, so nothing half-applied
                    app.logout_others(self.token())
                if user is not None:
                    app.store.set_admin_user(user)
                return self.json(200, {"ok": True, "username": app.store.admin_user(),
                                       "default_credentials": app.store.admin_is_default()})
            return self.error(404, "not found")

        def api_phone(self, method, arg, query):
            if arg is None and method == "GET":
                return self.json(200, app.phone_get())
            if arg is None and method == "POST":
                out = app.phone_command(self.body())
                if out is None:
                    return self.error(504, "The LAN service did not answer. Is netkey-lan running?")
                return self.json(200 if out["result"]["ok"] else 400,
                                 dict(out, error=None if out["result"]["ok"] else out["result"]["message"]))
            if arg == "book" and method == "PUT":
                return self.json(200, {"book": app.set_phone_book(self.body().get("book"))})
            try:
                if arg == "presence" and method == "GET":
                    return self.json(200, app.phone_presence())
                if arg == "lookup" and method == "GET":
                    return self.json(200, app.phone_lookup((query.get("n") or [""])[0]))
            except PartiesError as e:
                return self.error(e.status, str(e))
            return self.error(404, "not found")

        def api_parties(self, method, sub, query=None):
            """Public parties. GET me | directory | games | mine | listing/<id> | chat?room=&after= | ics/<id>;
            POST nick/check | nick | listing | listing/<id>/<action> | chat | chat/delete | chat/mute | report.
            Starting, joining and leaving parties go through /api/party (the LAN service). Hub refusals keep their
            status (403 banned or no nickname, 409 taken or full, 429 too often); an unreachable hub is 502."""
            query = query or {}
            try:
                n, get, post = len(sub), method == "GET", method == "POST"
                if sub == ["me"] and get:
                    return self.json(200, app.parties_me())
                if sub == ["nick", "check"] and post:
                    return self.json(200, app.parties_nick_check(self.body().get("nick")))
                if sub == ["nick"] and post:
                    return self.json(200, app.parties_set_nick(self.body().get("nick")))
                if sub == ["directory"] and get:
                    return self.json(200, app.parties_directory())
                if sub == ["games"] and get:
                    return self.json(200, app.parties_games())
                if sub == ["mine"] and get:
                    return self.json(200, app.parties_mine())
                if sub == ["listing"] and post:
                    return self.json(200, app.parties_create_listing(self.body()))
                if n == 2 and sub[0] == "listing" and get:
                    return self.json(200, app.parties_listing(sub[1]))
                if n == 3 and sub[0] == "listing" and post:
                    return self.json(200, app.parties_listing_action(sub[1], sub[2], self.body()))
                if sub == ["chat"] and get:
                    return self.json(200, app.parties_chat((query.get("room") or [""])[0],
                                                           (query.get("after") or ["0"])[0]))
                if sub == ["chat"] and post:
                    return self.json(200, app.parties_chat_post(self.body()))
                if n == 2 and sub[0] == "chat" and post:
                    return self.json(200, app.parties_chat_action(sub[1], self.body()))
                if sub == ["report"] and post:
                    return self.json(200, app.parties_report(self.body()))
                if n == 2 and sub[0] == "ics" and get:
                    body, name = app.parties_ics(sub[1])
                    return self.send(200, body, "text/calendar; charset=utf-8",
                                     {"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
            except PartiesError as e:
                status = e.status if e.status in (400, 403, 404, 409, 429, 502, 503) else 502
                return self.error(status, str(e))
            return self.error(404, "not found")

        def api_users(self, method, name):
            store = app.store
            if method == "GET" and name is None:
                return self.json(200, {"users": store.list_users(), "sync": store.users_sync_state()})
            if method == "POST" and name is None:
                data = self.body()
                try:
                    user = store.add_user(str(data.get("name", "")).strip(), str(data.get("password", "")),
                                          data.get("enabled", True) is not False, str(data.get("note", "")))
                except KeyError:
                    return self.error(409, "A user with that name already exists.")
                return self.json(201, user)
            if name is None:
                return self.error(405, "method not allowed")
            try:
                if method == "GET":
                    user = store.get_user(name)
                    return self.json(200, user) if user else self.error(404, "no such user")
                if method == "PUT":
                    data = self.body()
                    pw = data.get("password")
                    return self.json(200, store.update_user(
                        name, password=str(pw) if pw not in (None, "") else None,
                        enabled=data.get("enabled") if isinstance(data.get("enabled"), bool) else None,
                        note=data.get("note")))
                if method == "DELETE":
                    store.delete_user(name)
                    return self.json(200, {"ok": True})
            except KeyError:
                return self.error(404, "no such user")
            return self.error(405, "method not allowed")

        # ---- the shared folder (fileshare.py serves it to the retro LAN)
        def share_root(self):
            root = app.cfg()["share"]["dir"]
            if not os.path.isdir(root):
                os.makedirs(root, exist_ok=True)
                os.chmod(root, 0o2775)
            return os.path.realpath(root)          # fs.resolve() works with real paths

        # ---- backup and restore (backup.py): both need the admin password again, like an update does
        def api_backup(self, arg):
            from . import backup as bk
            from .config import check_user
            d = self.body()
            if not app.store.check_admin_password(str(d.get("password") or "")):
                return self.error(403, "The admin password is not right.")
            try:
                if arg is None:
                    data = bk.seal(bk.collect(app, app_version()), str(d.get("passphrase") or ""))
                    name = f"netkey-{clean_file_part(socket.gethostname())}-{time.strftime('%Y-%m-%d')}.nkbackup"
                    log.info("web: backup downloaded")
                    return self.send_file_bytes(data, name)
                if arg == "restore":
                    payload = bk.open_sealed(str(d.get("file") or "").encode(), str(d.get("passphrase") or ""))
                    from . import identity as ident
                    new_secret = (payload.get("identity") or {}).get("secret")
                    try:
                        same = new_secret == ident.load(app.state_dir)["secret"]
                    except (ValueError, OSError):
                        same = False
                    if new_secret and not same and app.party_get().get("role") not in ("standalone", "unknown"):
                        return self.error(409, "Leave or close the LAN party first: restoring changes the box identity.")
                    report = bk.apply(app, payload, lambda k: get_setting(app.base, k), check_setting, check_user)
                    if report["identity"]:
                        with app.lock:
                            app._hub = None                       # the GUI's hub client registers again
                        try:
                            os.unlink(os.path.join(app.state_dir, "hub-web.json"))
                        except OSError:
                            pass
                        from . import party as pt
                        pt.queue_command(app.state_dir, "identity")
                    log.info("web: backup restored: %s", report)
                    return self.json(200, dict(report, ok=True))
            except bk.BackupError as e:
                return self.error(400, str(e))
            return self.error(404, "not found")

        # ---- diagnostics: netkey-diag.path sees the request and runs diagnose.sh as root; the report comes back here
        def api_diagnostics(self):
            req = os.path.join(app.state_dir, "diag-request")
            out = os.path.join(app.state_dir, "diagnostics.txt")
            with app.lock:
                if time.time() - app.diag_asked < 5:
                    return self.error(429, "A report is already being made.")
                app.diag_asked = time.time()
            asked = time.time()
            try:
                write_atomic(req, f"{asked}\n", mode=0o640)
            except OSError as e:
                return self.error(500, f"Could not ask for the report: {e}")
            deadline = time.monotonic() + DIAG_WAIT
            while time.monotonic() < deadline:
                try:
                    st_ = os.lstat(out)
                except OSError:
                    st_ = None
                if st_ is not None and st_.st_mtime >= asked - 1 and not os.path.lexists(req):
                    try:
                        fd = os.open(out, os.O_RDONLY | os.O_NOFOLLOW)
                        with os.fdopen(fd, "rb") as f:
                            data = f.read(4 * 1024 * 1024)
                    except OSError:
                        data = b""
                    if data:
                        log.info("web: diagnostics report downloaded")
                        name = f"netkey-diagnostics-{clean_file_part(socket.gethostname())}-{time.strftime('%Y-%m-%d-%H%M')}.txt"
                        return self.send_file_bytes(data, name)
                time.sleep(0.5)
            try:
                os.unlink(req)
            except OSError:
                pass
            return self.error(504, "The box did not make the report in time. Its diagnostics service may be missing: "
                                   "run the NetKey installer again, or run sudo /opt/netkey/diagnose.sh over SSH.")

        def send_file_bytes(self, data, name):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def trash_tidy(self, root, force=False):
            """Empty trash entries older than TRASH_DAYS; at most every 10 minutes unless forced."""
            now = time.time()
            if not force and now - app.trash_tidied < 600:
                return
            app.trash_tidied = now
            try:
                n, freed = fs.trash_purge(root, older_than=now - fs.TRASH_DAYS * 86400)
                if n:
                    log.info("web: emptied %d trash item(s) older than %d days (%d MB)", n, fs.TRASH_DAYS, freed >> 20)
            except (OSError, ValueError) as e:
                log.warning("web: could not tidy the trash: %s", e)

        def api_files(self, method, arg, query):
            root = self.share_root()
            q = lambda k, d="": (query.get(k) or [d])[0]           # noqa: E731
            try:
                if method == "GET" and arg is None:
                    rel = "/".join(fs.rel_parts(q("path")))
                    self.trash_tidy(root)
                    du = shutil.disk_usage(root)
                    trash = fs.trash_list(root)
                    return self.json(200, {"path": rel, "entries": fs.listing(root, rel), "free": du.free,
                                           "total": du.total, "reserve": UPLOAD_RESERVE,
                                           "trash": {"count": len(trash), "size": sum(x["size"] or 0 for x in trash)}})
                if method == "GET" and arg == "trash":
                    self.trash_tidy(root, force=True)
                    return self.json(200, {"items": fs.trash_list(root), "days": fs.TRASH_DAYS})
                if method == "POST" and arg == "restore":
                    ids = [str(i) for i in (self.body().get("ids") or [])]
                    try:
                        n = fs.trash_restore(root, ids)
                    except FileExistsError as e:
                        return self.error(409, f"{e}. Rename or move that one first.")
                    log.info("web: restored %d item(s) from the trash", n)
                    return self.json(200, {"ok": True, "restored": n})
                if method == "POST" and arg == "purge":
                    d = self.body()
                    if d.get("all"):
                        n, freed = fs.trash_purge(root, older_than=float("inf"))
                    else:
                        ids = [str(i) for i in (d.get("ids") or [])]
                        for i in ids:
                            fs.trash_entry(root, i)                       # every id checked before anything goes
                        n, freed = fs.trash_purge(root, ids=set(ids))
                    log.info("web: emptied %d item(s) from the trash", n)
                    return self.json(200, {"ok": True, "deleted": n, "freed": freed})
                if method == "GET" and arg == "download":
                    return self.download(fs.resolve(root, q("path")))
                if method == "POST" and arg == "mkdir":
                    d = self.body()
                    path = os.path.join(fs.resolve(root, d.get("path")), fs.check_name(d.get("name")))
                    if os.path.lexists(path):
                        return self.error(409, "Something with that name is already there.")
                    os.mkdir(path)
                    os.chmod(path, 0o2775)
                    return self.json(200, {"ok": True})
                if method == "POST" and arg == "rename":
                    d = self.body()
                    src = fs.resolve(root, d.get("path"))
                    if os.path.realpath(src) == os.path.realpath(root):
                        raise ValueError("the shared folder itself can't be renamed")
                    dst = os.path.join(os.path.dirname(src), fs.check_name(d.get("to")))
                    if os.path.lexists(dst) and os.path.normcase(dst) != os.path.normcase(src):
                        return self.error(409, "Something with that name is already there.")
                    fs.move(src, dst)
                    return self.json(200, {"ok": True})
                if method == "POST" and arg == "move":
                    d = self.body()
                    to = fs.resolve(root, d.get("to"))
                    if not os.path.isdir(to):
                        raise ValueError("move to a folder")
                    # check every item first, so a clash halfway doesn't leave a selection half moved
                    plan, names = [], set()
                    for rel in d.get("paths") or []:
                        src = fs.resolve(root, rel)
                        name = os.path.basename(src)
                        dst = os.path.join(to, name)
                        if os.path.realpath(src) == os.path.realpath(root) or \
                                (os.path.realpath(to) + os.sep).startswith(os.path.realpath(src) + os.sep):
                            raise ValueError("a folder can't be moved into itself")
                        if os.path.normcase(os.path.dirname(src)) == os.path.normcase(to):
                            continue                                  # already there
                        if os.path.lexists(dst) or name.lower() in names:
                            return self.error(409, f"{name} is already in that folder.")
                        names.add(name.lower())
                        plan.append((src, dst))
                    for src, dst in plan:
                        fs.move(src, dst)
                    return self.json(200, {"ok": True, "moved": len(plan)})
                if method == "POST" and arg == "delete":
                    # into the trash (kept TRASH_DAYS days), or for good with "forever"
                    d = self.body()
                    forever = bool(d.get("forever"))
                    n, skipped, paths, ids = 0, 0, [], []
                    for rel in d.get("paths") or []:                     # check them all before deleting any
                        path = fs.resolve(root, rel)
                        if os.path.realpath(path) == os.path.realpath(root):
                            raise ValueError("the shared folder itself can't be deleted")
                        paths.append(path)
                    for path in paths:
                        if not os.path.lexists(path):
                            continue                                  # inside a folder deleted just before
                        if not forever:
                            try:
                                ids.append(fs.to_trash(root, path))
                                n += 1
                                continue
                            except OSError as e:
                                if e.errno != errno.EXDEV:
                                    raise
                                skipped += 1                          # on another drive mounted inside: no trash
                        if os.path.isdir(path) and not os.path.islink(path):
                            shutil.rmtree(path)
                        else:
                            os.unlink(path)
                        fs.remove_fork(path)
                        n += 1
                    log.info("web: %s %d item(s) from the shared folder", "deleted" if forever else "trashed", n)
                    return self.json(200, {"ok": True, "deleted": n, "trashed": not forever, "ids": ids, "no_trash": skipped})
            except FileNotFoundError:
                return self.error(404, "That file or folder is gone (someone may have moved it from a PC).")
            except NotADirectoryError:
                return self.error(400, "That is not a folder.")
            except PermissionError:
                return self.error(403, "The box may not change that file (check the folder's permissions).")
            return self.error(404, "not found")

        def download(self, path):
            if not os.path.isfile(path):
                return self.error(400, "only files can be downloaded")
            name = os.path.basename(path)
            with open(path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    shutil.copyfileobj(f, self.wfile, 256 * 1024)

        def upload(self, query):
            """PUT /api/files/upload?path=DIR&name=NAME[&overwrite=1]: the body is the file, streamed to disk.
            NAME may hold subfolders (a dropped folder), created as needed."""
            self.raw_body = b""
            try:
                n = int(self.headers.get("Content-Length") or -1)
            except ValueError:
                n = -1
            def refuse(code, msg):                                # noqa: E306
                self.close_connection = True                     # the body is not read: drop the connection
                return self.error(code, msg)
            if self.headers.get("X-Requested-With") != "netkey":
                return refuse(403, "missing X-Requested-With header")
            if not app.valid(self.token()):
                return refuse(401, "login required")
            if app.must_change_password():
                return refuse(403, "Set a new admin password first.")
            if n < 0:
                return refuse(411, "Content-Length is required")
            q = lambda k, d="": (query.get(k) or [d])[0]           # noqa: E731
            tmp = None
            try:
                root = self.share_root()
                parts = fs.rel_parts(q("name"))
                if not parts:
                    raise ValueError("name is missing")
                du = shutil.disk_usage(root)
                if n > du.free - UPLOAD_RESERVE:                      # the trash gives way first, oldest first
                    gone, freed = fs.trash_purge(root, need=n + UPLOAD_RESERVE, free=du.free)
                    if gone:
                        log.info("web: emptied %d trash item(s) (%d MB) to make room for an upload", gone, freed >> 20)
                    du = shutil.disk_usage(root)
                if n > du.free - UPLOAD_RESERVE:
                    return refuse(507, f"Not enough space on the memory card: {du.free // 1048576} MB free.")
                folder = fs.resolve(root, q("path"))
                for p in parts[:-1]:
                    folder = os.path.join(folder, p)
                    fs.resolve(root, os.path.relpath(folder, root), must_exist=False)   # each step stays inside
                    if not os.path.isdir(folder):
                        os.mkdir(folder)
                        os.chmod(folder, 0o2775)
                fs.resolve(root, os.path.relpath(folder, root))     # still inside (a symlinked folder is refused)
                dst = os.path.join(folder, parts[-1])
                if os.path.isdir(dst) or (os.path.lexists(dst) and q("overwrite") != "1"):
                    return refuse(409, f"{parts[-1]} is already there.")
                tmp = os.path.join(folder, f".upload-{secrets.token_hex(6)}")
                left = n
                with open(tmp, "wb") as f:
                    while left:
                        chunk = self.rfile.read(min(left, 256 * 1024))
                        if not chunk:
                            raise ConnectionResetError("upload broke off")
                        f.write(chunk)
                        left -= len(chunk)
                os.chmod(tmp, 0o664)
                os.replace(tmp, dst)
                tmp = None
                fs.remove_fork(dst)          # a replaced Mac file's old resource fork doesn't belong to the new one
                log.info("web: uploaded %s (%d bytes) to the shared folder", "/".join(parts), n)
                return self.json(200, {"ok": True, "name": parts[-1], "size": n})
            except ValueError as e:
                return refuse(400, str(e))
            except FileNotFoundError:
                return refuse(404, "That folder is gone.")
            except PermissionError:
                return refuse(403, "The box may not write there (check the folder's permissions).")
            except (ConnectionResetError, BrokenPipeError, socket.timeout, ssl.SSLError):
                self.close_connection = True
                log.info("web: an upload to the shared folder broke off")
            except OSError as e:
                return refuse(500, f"Could not save the file: {e.strerror or e}")
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

        def static(self, path):
            name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            if name.startswith("static/"):
                name = name[len("static/"):]
            if not re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", name) or ".." in name:
                return self.error(404, "not found")
            full = os.path.join(WEB_ROOT, name)
            if not os.path.isfile(full):
                if "." not in name.rsplit("/", 1)[-1]:      # client-side routes fall back to the app
                    full = os.path.join(WEB_ROOT, "index.html")
                else:
                    return self.error(404, "not found")
            with open(full, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            self.send(200, data, ctype, {"Cache-Control": "no-cache"})

    return Handler


def make_redirect_handler(https_port):
    class Redirect(BaseHTTPRequestHandler):
        server_version = "netkey"
        sys_version = ""
        timeout = 10

        def log_message(self, *_):
            pass

        def do_GET(self):
            host = (self.headers.get("Host") or socket.gethostname()).strip()
            try:
                host = str(ipaddress.ip_address(host.strip("[]")))
                host = f"[{host}]" if ":" in host else host
            except ValueError:
                host = re.sub(r":\d+$", "", host)
                if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
                    host = socket.gethostname()
            port = "" if https_port == 443 else f":{https_port}"
            self.send_response(301)
            self.send_header("Location", f"https://{host}{port}{self.path if self.path.startswith('/') else '/'}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_HEAD = do_POST = do_PUT = do_DELETE = do_GET

    return Redirect


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


def serve(app, bind, port, http_port, cert, key):
    """Start the HTTPS (or HTTP fallback) server and the redirect server. Returns the server objects."""
    servers = []
    ctx = None
    if cert and key and os.path.exists(cert) and os.path.exists(key):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
    elif cert:
        log.warning("web: no TLS certificate (%s); serving plain HTTP on port %s", cert, http_port or port)
        port, http_port = (http_port or port), 0
    main = Server((bind, port), make_handler(app, ctx is not None))
    if ctx:
        main.socket = ctx.wrap_socket(main.socket, server_side=True, do_handshake_on_connect=False)
    servers.append(main)
    if ctx and http_port:
        try:
            servers.append(Server((bind, http_port), make_redirect_handler(port)))
        except OSError as e:
            log.warning("web: cannot listen on port %d for the HTTPS redirect: %s", http_port, e)
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True, name=f"http-{s.server_address[1]}").start()
    log.info("web: %s on %s:%d%s", "HTTPS" if ctx else "HTTP", bind or "*", port,
             f", redirect from :{http_port}" if len(servers) > 1 else "")
    return servers


def run(base_cfg, state_dir, run_dir, port=None, demo=False):
    web = base_cfg["web"]
    if demo:
        from .demo import setup_demo
        app = setup_demo(base_cfg)
        servers = serve(app, web["bind"] or "127.0.0.1", port or 8080, 0, None, None)
    else:
        app = App(base_cfg, state_dir, run_dir)
        if not app.store.admin_hash():
            app.store.reset_admin()
            log.warning("web: no admin login set; using the default admin / admin")
        servers = serve(app, web["bind"], port or int(web["port"]), int(web["http_port"]), web["cert"], web["key"])
    app.collector.start()
    stop = threading.Event()
    import signal
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    app.collector.stopping.set()
    try:
        app.collector.flush()
        app.store.write_stats(force=True)           # what waited in memory (SD card wear) goes to disk now
    except Exception:
        log.exception("web: could not save the last statistics")
    for s in servers:
        s.shutdown()
    return 0
