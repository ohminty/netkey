"""Demo/dev mode for the web GUI: simulated lines, calls and a week of history in a temporary state dir."""
import base64
import hashlib
import os
import random
import socket
import tempfile
import threading
import time

from . import status as st
from .store import Store, write_json_atomic
from .web import LIVE_POINTS, SAMPLE_S, App

DEMO_LINES = {
    "serial/by-id/usb-FTDI_FT232R_USB_UART_A10KZ3QX-if00-port0": 0,
    "serial/by-path/platform-20980000.usb-usb-0:1.3:1.0-port0": 1,
    "serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller_D-if00-port0": 2,
    "serial/by-path/platform-20980000.usb-usb-0:1.5:1.0-port0": 3,
}
UNPLUGGED_LINE = 3
USERS = [("test", "test", "imported from config.toml"), ("alice", "wonderland", "Win95 Pentium 133"),
         ("bob", "builder42", "Win98 SE laptop"), ("retro", "dialup!", "XP test box")]
CONNECTS = {"virtual": ["CONNECT 56000"], "physical": ["CONNECT 33600/ARQ/V34/LAPM/V42BIS",
                                                       "CONNECT 28800/ARQ", "CONNECT 49333/ARQ/V90"]}


def demo_key():
    import base64
    return base64.b64encode(os.urandom(32)).decode()


DEMO_HUB_URL = "https://relay.netkey.no:8443"
DEMO_RELAYS = [("local", "de-fra", 24.0), ("us-nyc", "us-nyc", 109.0)]


def demo_probe():
    return {"at": time.time(), "running": False, "error": None,
            "relays": [{"id": i, "name": n, "rtt_ms": round(ms + random.uniform(-1.5, 1.5), 1)}
                       for i, n, ms in DEMO_RELAYS]}
HUB_FLAP_S = (120, 10)          # the demo hub connection drops for 10 s every 2 minutes


def demo_hub_code():
    from . import hubclient as hc
    n = hc.ID_CHARS + hc.FP_CHARS
    body = hc.b32(random.getrandbits(5 * n), n)
    return "-".join([hc.CODE_PREFIX] + [body[i:i + 4] for i in range(0, n, 4)])


def verify(seed):
    """Key check string for demo keys (real ones in lan.py come from the box's WireGuard key)."""
    from . import hubclient as hc
    try:
        return hc.verify_words(seed)
    except ValueError:
        return hc.verify_words(base64.b64encode(hashlib.sha256(str(seed).encode()).digest()).decode())


class DemoParty:
    """In-memory LAN party for --demo: starts as a cloud-hub host with two members and one pending request."""

    def __init__(self, name, hub_url=None):
        self.box = name
        self.hub_url = hub_url or DEMO_HUB_URL
        self.results = []
        self.host_state([{"id": 1, "name": "BOB-P133", "online": True, "rtt": 23.4, "endpoint": "88.90.67.98:35218"},
                         {"id": 2, "name": "dosbox-den", "online": False, "rtt": None, "endpoint": None}],
                        [{"name": "ALICE-WIN98", "since": time.time() - 40}], via="hub")

    def host_state(self, members=(), pending=(), via="direct"):
        now = time.time()
        self.s = {"role": "host", "via": via, "party_id": 0 if via == "hub" else random.randrange(1 << 31),
                  "name": self.box, "code": demo_hub_code() if via == "hub" else None,
                  "endpoint": "retro.example.org", "port": 51899, "max": 16, "closing_at": None,
                  "members": [dict(m, joined=now - 3600, kicked_at=None, rx=random.randrange(10**6, 10**7),
                                   tx=random.randrange(10**6, 10**7)) for m in members],
                  "pending": [dict(p, pub=demo_key()) for p in pending], "join_priv": demo_key(), "pub": demo_key()}

    def note(self, cid, cmd, ok, message):
        r = {"id": cid, "cmd": cmd, "ok": ok, "message": message, "at": time.time()}
        self.results = (self.results + [r])[-20:]
        return r

    def tick(self):
        s, now = self.s, time.time()
        if s["role"] == "host":
            s["members"] = [m for m in s["members"] if not (m["kicked_at"] and now - m["kicked_at"] > 5)]
            for m in s["members"]:
                if m["online"]:
                    m["rx"] += random.randrange(2000, 9000)
                    m["tx"] += random.randrange(1000, 6000)
                elif now - m["joined"] < 30 and not m["kicked_at"]:
                    m.update(online=True, rtt=round(random.uniform(15, 60), 1), endpoint="203.0.113.7:41641")
            if s["closing_at"] and now - s["closing_at"] > 3:
                self.s = {"role": "standalone"}
                self.note(None, "notice", True, "Party closed")
        elif s["role"] == "member" and s["status"] == "joining" and now - s["since"] > s.get("approve_after", 4):
            s.update(status="approved", member_id=3, joined_at=now)
            self.note(None, "notice", True, f"Joined {s['party_name']}'s LAN party as member 3")

    def view(self):
        from . import party as pt
        self.tick()
        s, now = self.s, time.time()
        via = s.get("via", "direct") if s["role"] != "standalone" else None
        hub = via == "hub"
        out = {"role": s["role"], "via": via, "results": list(self.results), "updated": now,
               "relay_probe": demo_probe()}
        out["watches"] = {}
        if hub:
            out.update(hub_party=s.get("hub_party"), public=bool(s.get("public")), listing=s.get("listing"),
                       title=s.get("title"), event_code=s.get("event_code"))
            period, outage = HUB_FLAP_S
            down_for = now % period - (period - outage)
            out["hub"] = {"url": self.hub_url, "online": down_for < 0,
                          "error": None if down_for < 0 else "Connection to the hub timed out; retrying",
                          "last_ok": now - (down_for if down_for >= 0 else random.uniform(0.5, 3))}
        if s["role"] == "host":
            out.update(party_id=s["party_id"], name=s["name"], endpoint=None if hub else s["endpoint"],
                       relay=s.get("relay", "de-fra") if hub else None,
                       port=None if hub else s["port"], max=s["max"], closing=bool(s["closing_at"]),
                       code=s["code"] if hub else pt.encode_code(s["party_id"], s["endpoint"], s["port"],
                                                                 s["pub"], s["join_priv"]),
                       pending=[{"pub": p["pub"], "name": p["name"], "since": p["since"],
                                 "verify": verify(p["pub"])} for p in s["pending"]],
                       members=[{"id": m["id"], "name": m["name"], "joined": m["joined"], "online": m["online"],
                                 "handshake_age": random.randrange(1, 20) if m["online"] else None,
                                 "endpoint": m["endpoint"], "rtt_ms": m["rtt"],
                                 "connection": "relay" if hub else "direct",
                                 "relay": s.get("relay", "de-fra") if hub else None,
                                 "last_hello": now - 2 if m["online"] else None, "kicked": bool(m["kicked_at"]),
                                 "rx_bytes": m["rx"], "tx_bytes": m["tx"], "verify": verify(m.get("pub") or m["name"])}
                                for m in s["members"]])
        elif s["role"] == "member":
            approved = s["status"] == "approved"
            out.update(status="pending" if not approved and (hub or now - s["since"] > 1.5) else s["status"],
                       party_id=s["party_id"],
                       host_endpoint="via the cloud hub" if hub else f"{s['endpoint']}:{s['port']}",
                       connection="relay" if hub else "direct",
                       relay="de-fra" if hub and approved else None,
                       member_id=s.get("member_id"), party_name=s["party_name"], error=None, unreachable_since=None,
                       verify=verify(self.box),
                       rtt_ms=round(random.uniform(30, 45), 1) if approved else None,
                       handshake_age=random.randrange(1, 25) if approved else None,
                       view={"name": s["party_name"], "share_internet": False,
                             "members": [{"id": 1, "name": "BOB-P133"}, {"id": 3, "name": self.box}]}
                       if approved else None)
        return out

    def command(self, data):
        from . import party as pt
        from . import hubclient as hc
        self.tick()
        cmd, s, cid = data.get("cmd"), self.s, f"demo-{time.time()}"
        need = {"create": "standalone", "join": "standalone", "approve": "host", "reject": "host", "kick": "host",
                "regenerate": "host", "close": "host", "endpoint": "host", "leave": "member", "relays": None}
        if cmd not in need:
            raise ValueError("unknown LAN party command")
        if cmd == "relays":
            return self.note(cid, cmd, True, "Measuring the distance to the relays")
        if s["role"] != need[cmd]:
            return self.note(cid, cmd, False, {"standalone": "Leave or close the current party first",
                                               "host": "Only the party host can do that",
                                               "member": "This box is not a party member"}[need[cmd]])
        target = str(data.get("member", "")).strip()
        if cmd == "create":
            via = str(data.get("via") or "hub")
            if via not in ("hub", "direct"):
                raise ValueError("via must be hub or direct")
            self.host_state(via=via)
            if via == "hub":
                self.s["relay"] = "de-fra"
                return self.note(cid, cmd, True, f"Party created via the cloud hub; share the code {self.s['code']}")
            if data.get("endpoint"):
                self.s["endpoint"] = data["endpoint"]
            return self.note(cid, cmd, True, f"Party created; members connect to {self.s['endpoint']}:51899")
        if cmd == "join" and hc.is_hub_code(data.get("code", "")):
            try:
                hc.parse_hub_code(data["code"])
            except ValueError as e:
                return self.note(cid, cmd, False, str(e))
            self.s = {"role": "member", "via": "hub", "status": "joining", "since": time.time(), "party_id": 0,
                      "party_name": "retrohub", "approve_after": 8}
            return self.note(cid, cmd, True, "Asked retrohub to let this box join (via the cloud hub)")
        if cmd == "join":
            try:
                info = pt.decode_code(data.get("code", ""))
            except ValueError as e:
                return self.note(cid, cmd, False, str(e))
            self.s = {"role": "member", "status": "joining", "since": time.time(), "party_id": info["party_id"],
                      "endpoint": info["endpoint"], "port": info["port"], "party_name": "retrohub"}
            return self.note(cid, cmd, True, f"Asking the host at {info['endpoint']} to let this box join")
        if cmd == "leave":
            self.s = {"role": "standalone"}
            return self.note(cid, cmd, True, "Left the party")
        if cmd in ("approve", "reject"):
            picked = [p for p in s["pending"] if target in ("all", p["pub"], p["name"])]
            if not picked:
                return self.note(cid, cmd, False, f"No pending join request from {target}")
            for p in picked:
                s["pending"].remove(p)
                if cmd == "approve":
                    used = {m["id"] for m in s["members"]}
                    mid = next(i for i in range(1, 16) if i not in used)
                    s["members"].append({"id": mid, "name": p["name"], "online": False, "rtt": None, "endpoint": None,
                                         "joined": time.time(), "kicked_at": None, "rx": 0, "tx": 0})
            names = ", ".join(p["name"] for p in picked)
            return self.note(cid, cmd, True, f"{'Approved' if cmd == 'approve' else 'Rejected'} {names}")
        if cmd == "kick":
            m = next((m for m in s["members"] if target in (str(m["id"]), m["name"])), None)
            if not m:
                return self.note(cid, cmd, False, f"No member {target}")
            m.update(kicked_at=time.time(), online=False)
            return self.note(cid, cmd, True, f"Removed {m['name']} (member {m['id']})")
        if cmd == "regenerate":
            s["join_priv"], s["code"] = demo_key(), demo_hub_code() if s.get("via") == "hub" else None
            return self.note(cid, cmd, True, "New invite code created; the old code no longer works")
        if cmd == "endpoint" and s.get("via") == "hub":
            return self.note(cid, cmd, False, "This party runs through the cloud hub; members need no address")
        if cmd == "endpoint":
            ep = str(data.get("endpoint", "")).strip() or "192.168.1.20"
            if " " in ep:
                return self.note(cid, cmd, False, "Give a host name or IP address")
            s["endpoint"] = ep
            return self.note(cid, cmd, True, f"Members now connect to {ep}; share the new invite code")
        s["closing_at"] = time.time()
        return self.note(cid, cmd, True, "Closing the party; members are told within a few seconds")


DEMO_WIFI = [("HomeNet", 82, "WPA2", "5 GHz"), ("HomeNet-2G", 71, "WPA2", "2.4 GHz"),
             ("Altibox-5F2C", 57, "WPA2 WPA3", "5 GHz"), ("Telenor-4G-21A9", 43, "WPA2", "2.4 GHz"),
             ("RetroCafe Guest", 31, "", "2.4 GHz"), ("NETGEAR42", 16, "WPA1 WPA2", "2.4 GHz")]
DEMO_WIFI_IP, DEMO_ETH_IP = "192.168.1.57", "192.168.1.42"
NET_TIMING = {"scan": 1.5, "connect": 3.0, "checking": 6.0}


class DemoNetwork:
    """In-memory uplink for --demo: Ethernet uplink, Wi-Fi not configured yet, rescue hotspot armed."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.wifi = None                 # {"ssid", "band", "signal"} once joined
        self.known = []                  # saved networks, preferred first: [{"ssid", "band", "signal"}]
        self.country = None
        self.connecting = None
        self.scan = {"at": None, "networks": []}
        self.ssid = "NetKey-" + "".join(random.choice("0123456789ABCDEF") for _ in range(4))
        from .uplink import HOTSPOT_PASSWORD
        self.password = HOTSPOT_PASSWORD
        self.mode, self.mode_since, self.lost_since = None, time.time(), None
        self.wifi_hw = not os.environ.get("NETKEY_DEMO_NO_WIFI")     # simulate a Pi 1 / VPS without Wi-Fi

    def view(self):
        cfg, now = self.app.cfg()["network"], time.time()
        with self.lock:
            mode = cfg["uplink"] if self.wifi_hw else "ethernet"
            if mode != self.mode:
                self.mode, self.mode_since = mode, now
            joined = self.wifi is not None and not self.connecting
            ok = mode == "ethernet" or joined
            self.lost_since = None if ok else (self.lost_since or now)
            lost = now - self.lost_since if self.lost_since else None
            after = 300 if self.wifi else 60
            hotspot = "off"
            if cfg["hotspot"] and lost is not None and lost >= after and not self.connecting:
                hotspot = "on" if (lost - after) % 330 < 300 else "retry"
            if hotspot == "on":
                wifi = {"state": "connected", "connection": "netkey-hotspot", "ip": "192.168.4.1",
                        "ssid": (self.wifi or {}).get("ssid"), "signal": None, "band": None}
            elif self.connecting:
                wifi = {"state": "connecting", "connection": "netkey-wifi-new", "ip": None,
                        "ssid": self.connecting, "signal": None, "band": None}
            elif self.wifi:
                wifi = {"state": "connected", "connection": "netkey-wifi-1", "ip": DEMO_WIFI_IP,
                        "ssid": self.wifi["ssid"], "band": self.wifi["band"],
                        "signal": max(5, min(99, self.wifi["signal"] + random.randint(-3, 3)))}
            else:
                wifi = {"state": "disconnected", "connection": None, "ip": None, "ssid": None, "signal": None,
                        "band": None}
            in_range = {n[0] for n in DEMO_WIFI}
            wifi.update(iface="wlan0", configured=bool(self.known), country=self.country,
                        known=[{"ssid": k["ssid"], "preferred": i == 0, "in_range": k["ssid"] in in_range,
                                "connected": bool(self.wifi) and k["ssid"] == self.wifi["ssid"] and not self.connecting}
                               for i, k in enumerate(self.known)])
            if not self.wifi_hw:
                wifi, hotspot = None, "off"
            if mode == "ethernet":
                eth = {"name": "eth0", "role": "uplink", "ip": DEMO_ETH_IP, "carrier": True}
            else:
                eth = {"name": "eth0", "role": "checking" if now - self.mode_since < NET_TIMING["checking"] else "retro",
                       "ip": None, "carrier": True}
            return {
                "mode": mode, "configured_mode": cfg["uplink"], "wifi_available": self.wifi_hw, "uplink_ok": ok,
                "uplink_dev": ("eth0" if mode == "ethernet" else "wlan0") if ok else None,
                "uplink_lost_for": int(lost) if lost is not None else None, "wifi": wifi, "ethernet": eth,
                "hotspot": {"enabled": bool(cfg["hotspot"]), "state": hotspot, "ssid": self.ssid,
                            "address": "192.168.4.1", "clients": 1 if hotspot == "on" else 0, "after_s": after,
                            "password": self.password},
                "home": {"gateway": "192.168.1.1", "subnet": "192.168.1.0/24", "gw_mac": "a4:91:b1:3c:7e:02"},
                "scan": {"at": self.scan["at"], "networks": [dict(n) for n in self.scan["networks"]]},
                "updated": now,
            }

    def command(self, cmd, kw):
        r = {"id": f"demo-{time.time()}", "cmd": cmd, "at": time.time()}
        try:
            if not self.wifi_hw:
                raise ValueError("This box has no Wi-Fi")
            r.update(ok=True, message=getattr(self, cmd)(**kw))
        except ValueError as e:
            r.update(ok=False, message=str(e))
        return r

    def wifi_scan(self):
        time.sleep(NET_TIMING["scan"])
        with self.lock:
            cur = self.wifi and not self.connecting and self.wifi["ssid"]
            nets = [{"ssid": s, "signal": max(5, sig + random.randint(-6, 6)), "secure": bool(sec), "security": sec,
                     "band": band, "in_use": s == cur} for s, sig, sec, band in DEMO_WIFI]
            self.scan = {"at": time.time(), "networks": sorted(nets, key=lambda n: (-n["in_use"], -n["signal"]))}
            return f"Found {len(nets)} Wi-Fi networks"

    def wifi_connect(self, ssid="", password="", country=""):
        if not 1 <= len(ssid.encode()) <= 32:
            raise ValueError("Network name must be 1-32 bytes")
        wrong = "wrong" in password.lower()          # "wrong", "wrongpass", ...: simulate a bad password
        if password and not wrong and not 8 <= len(password) <= 63:
            raise ValueError("Wi-Fi password must be 8-63 characters")
        cc = (country or self.country or "").upper()
        if not cc:
            raise ValueError("Choose the Wi-Fi country first")
        if len(cc) != 2 or not cc.isalpha():
            raise ValueError("Wi-Fi country must be a 2-letter code, e.g. NO")
        with self.lock:
            if self.connecting:
                raise ValueError(f"Already connecting to {self.connecting}")
            self.country, self.connecting = cc, ssid
        known = next((n for n in DEMO_WIFI if n[0] == ssid), None)
        saved = next((k for k in self.known if k["ssid"] == ssid), None)
        time.sleep(NET_TIMING["connect"])
        with self.lock:
            self.connecting = None
            if wrong or (known and known[2] and not password and not saved):
                raise ValueError(f"Could not join {ssid}: the Wi-Fi password is incorrect (Connection activation "
                                 "failed: Secrets were required, but not provided)")
            if saved and not known:
                raise ValueError(f"Could not join {ssid}: no network with that name in range")
            self.wifi = {"ssid": ssid, "signal": known[1] if known else 48, "band": known[3] if known else "2.4 GHz"}
            self.known = [dict(self.wifi)] + [k for k in self.known if k["ssid"] != ssid][:9]   # now the preferred one
            for n in self.scan["networks"]:
                n["in_use"] = n["ssid"] == ssid
        return f"Connected to {ssid} as {DEMO_WIFI_IP}"

    def wifi_forget(self, ssid=""):
        with self.lock:
            ssid = ssid or (self.wifi or {}).get("ssid") or (self.known[0]["ssid"] if self.known else "")
            if not any(k["ssid"] == ssid for k in self.known):
                raise ValueError(f"{ssid or 'That network'} is not saved on this box")
            self.known = [k for k in self.known if k["ssid"] != ssid]
            if self.wifi and self.wifi["ssid"] == ssid:
                # like NetworkManager: on to the next saved network in range
                here = {n[0]: n for n in DEMO_WIFI}
                nxt = next((k for k in self.known if k["ssid"] in here), None)
                self.wifi = {"ssid": nxt["ssid"], "signal": here[nxt["ssid"]][1], "band": here[nxt["ssid"]][3]} if nxt else None
            for n in self.scan["networks"]:
                n["in_use"] = bool(self.wifi) and n["ssid"] == self.wifi["ssid"]
        return f"Forgot {ssid}"


class DemoHub:
    """Stand-in for the cloud hub's public-party calls (nicknames). Same rules as the hub: 3-16 characters, unique by
    folded form, reserved names. Register the nickname "banme" to see how a banned box looks."""
    TAKEN = ["alice", "bobcat", "orc-peon", "quakebox", "dosbox-den", "pentium-mmx", "cnc-fan", "lars"]
    RESERVED = ["admin", "netkey", "host", "hub", "official", "moderator", "system"]

    def __init__(self):
        self.nick, self.changed, self.ban = None, None, None

    @staticmethod
    def fold(n):
        return n.lower().translate(str.maketrans({"0": "o", "1": "l", "i": "l", "5": "s", "-": None, "_": None})) \
            .replace("rn", "m")

    def problem(self, nick):
        import re
        if not re.match(r"^[A-Za-z0-9_-]{3,16}$", nick):
            return "3–16 characters: letters, digits, - and _", 400
        key = self.fold(nick)
        if len(key) < 3:
            return "use at least 3 letters or digits", 400
        if key in map(self.fold, self.RESERVED):
            return f'"{nick}" is reserved', 400
        hit = next((t for t in self.TAKEN if self.fold(t) == key), None)
        if hit and not (self.nick and self.fold(self.nick) == key):
            return (f'"{nick}" is taken' if hit == nick.lower() else f'too close to "{hit}", which is taken'), 409
        return "", 200

    def call(self, method, path, body):
        from .web import PartiesError
        time.sleep(0.15)
        if path == "/v1/me":
            return {"box": "nk-demo7qf3k2xdm4ra", "permanent": True, "nick": self.nick, "nick_changed": self.changed,
                    "can_change_at": self.changed + 30 * 86400 if self.changed else None, "ban": self.ban}
        nick = str((body or {}).get("nick") or "").strip()
        if path == "/v1/nick/check":
            msg, status = self.problem(nick)
            return {"free": not msg, "reason": msg}
        if path == "/v1/nick":
            if self.ban:
                raise PartiesError(403, "this box is banned from public parties: " + self.ban["reason"])
            msg, status = self.problem(nick)
            if msg:
                raise PartiesError(status, msg)
            if self.nick and self.fold(self.nick) != self.fold(nick) and time.time() - self.changed < 30 * 86400:
                raise PartiesError(409, "you can change your nickname once every 30 days")
            if not self.nick or self.fold(self.nick) != self.fold(nick):
                self.changed = time.time()
            self.nick = nick
            if nick.lower() == "banme":
                self.ban = {"reason": "Spam in party chats (demo)", "until": time.time() + 7 * 86400}
            return {"ok": True}
        raise PartiesError(404, "not found")


def demo_fileshare(cfg):
    s = cfg["share"]
    return {"active": True, "paused": None, "address": "10.99.0.1", "name": "NETKEY", "share": "FILES",
            "workgroup": s["workgroup"], "dir": s["dir"], "writable": s["writable"],
            **{k: {"enabled": bool(s[k]), "running": bool(s[k]), "error": None} for k in ("smb", "ftp", "web")}}


class DemoApp(App):
    demo = True

    def hub_call(self, method, path, body=None, timeout=15):
        cloud = getattr(self, "demo_cloud", None)
        if cloud:
            return cloud.call(method, path, body, timeout=timeout)
        return self.demo_hub.call(method, path, body)

    def net_get(self):
        return self.demo_net.view()

    def net_command(self, data):
        cmd, kw = self.net_args(data)
        return {"result": self.demo_net.command(cmd, kw), "network": self.demo_net.view()}

    def party_get(self):
        return self.demo_party.view()

    def party_command(self, data):
        time.sleep(0.6)                      # the real service answers within a couple of seconds
        cloud, cmd, dp = getattr(self, "demo_cloud", None), data.get("cmd"), self.demo_party
        if cloud and ((cmd == "create" and data.get("public")) or cmd in ("start", "watch")
                      or (cmd == "join" and data.get("listing"))):
            return {"result": self.demo_public(cloud, dp, data), "party": dp.view()}
        if cloud and cmd in ("close", "leave") and dp.s.get("hub_party"):
            try:
                cloud.call("POST", "/v1/party/close" if cmd == "close" else "/v1/leave", {})
            except Exception:
                pass
        result = dp.command(data)
        if cloud and cmd == "create" and result["ok"] and dp.s.get("via") == "hub":
            dp.s["hub_party"] = cloud.call("POST", "/v1/party", {"host_pub": demo_key(), "name": dp.box})["party"]
        return {"result": result, "party": dp.view()}

    def demo_public(self, cloud, dp, data):
        """Public create / start / join / watch against the demo's real hub."""
        from .web import PartiesError
        cmd, cid = data.get("cmd"), f"demo-{time.time()}"
        try:
            if cmd == "watch":
                return dp.note(cid, cmd, True, "This box joins automatically when the event starts"
                               if data.get("on", True) else "Auto-join off")
            if dp.s["role"] != "standalone":
                return dp.note(cid, cmd, False, "Leave or close the current party first")
            if cmd == "join":
                r = cloud.call("POST", "/v1/join", {"listing": data["listing"], "pub": demo_key(), "name": dp.box})
                dp.s = {"role": "member", "via": "hub", "status": "joining", "since": time.time(), "party_id": 0,
                        "party_name": r["party_name"], "approve_after": 3.5, "public": True,
                        "listing": r["listing"], "hub_party": r["party"]}
                cloud.approve_me_later(r["listing"])
                return dp.note(cid, cmd, True, f"Asked {r['party_name']} to let this box join")
            if cmd == "start":
                r = cloud.call("POST", f"/v1/listing/{data['listing']}/start", {"host_pub": demo_key()})
            else:
                r = cloud.call("POST", "/v1/party", {"host_pub": demo_key(), "public": data["public"]})
            dp.host_state(via="hub")
            dp.s.update(code=r["code"], relay=r.get("relay") or "de-fra", public=True, listing=r["listing"], max=r.get("max", 16),
                        title=r["title"], event_code=r["event_code"], hub_party=r["party"])
            return dp.note(cid, cmd, True, "Public party started via the cloud hub; it is listed under Parties")
        except PartiesError as e:
            return dp.note(cid, cmd, False, str(e))

    def lan_status(self):
        cfg = self.cfg()["lan"]
        now = time.time()
        if not cfg["enabled"]:
            return {"state": "disabled", "role": "standalone", "party": {"role": "standalone", "results": []}, "detail": "LAN party port is switched off", "enabled": False,
                    "share_internet": cfg["share_internet"], "leases": [], "nic": None, "subnet": cfg["subnet"],
                    "gateway": "10.99.0.1", "dhcp_range": ["10.99.0.100", "10.99.0.253"], "rx_bytes": 0, "tx_bytes": 0,
                    "updated": now}
        t = int(now)
        p = self.demo_party.view()
        party = {k: v for k, v in p.items() if k != "code"}
        builtin = (self.demo_net.view()["ethernet"] or {}).get("role") == "retro"    # Wi-Fi uplink: eth0 is the LAN port
        nic = {"name": "eth0", "mac": "b8:27:eb:5a:c7:b4", "carrier": True, "speed": 100, "duplex": "full"} if builtin \
            else {"name": "enx00e04c680a1b", "mac": "00:e0:4c:68:0a:1b", "carrier": True, "speed": 10, "duplex": None}
        return {"state": "up", "role": p["role"], "party": party, "detail": "", "enabled": True, "share_internet": cfg["share_internet"],
                "nic": nic,
                "subnet": cfg["subnet"], "gateway": "10.99.0.1", "dhcp_range": ["10.99.0.100", "10.99.0.253"],
                "leases": [] if p["role"] == "member" else [{"ip": "10.99.0.101", "mac": "00:a0:24:1b:2c:3d", "hostname": "WIN95-P133",
                            "expires": t + 40000},
                           {"ip": "10.99.0.102", "mac": "00:00:e8:4f:11:02", "hostname": "DOS-486",
                            "expires": t + 21000}],
                "rx_bytes": 5_400_000 + t % 100000 * 37, "tx_bytes": 1_200_000 + t % 100000 * 9,
                "fileshare": demo_fileshare(self.cfg()),
                "updated": now}

    def device_present(self, key):
        return DEMO_LINES.get(key) != UNPLUGGED_LINE


def seed_history(store, now, max_lines=4):
    rng = random.Random(95)
    rows = {}
    start = int(now // 60 * 60) - 7 * 86400
    for ts in range(start, int(now // 60 * 60), 60):
        hour = time.localtime(ts).tm_hour
        busy = 0.08 + 0.55 * max(0, 1 - abs(hour - 20) / 6)     # evenings are busy
        bucket = []
        for line in range(max_lines):
            present = line != UNPLUGGED_LINE or ts < now - 2 * 86400
            connected = present and rng.random() < busy * (1 - line * 0.18)
            rx = int(rng.uniform(40e3, 330e3)) if connected else 0
            tx = int(rx * rng.uniform(0.05, 0.2)) if connected else 0
            bucket.append((line, int(present), int(connected), rx, tx))
        rows[ts] = bucket
    for ts, bucket in rows.items():
        store.add_stats(ts, bucket)
    users = [u[0] for u in USERS]
    t = now - 7 * 86400
    while True:
        t += rng.expovariate(1 / 2400)
        if t > now - 4000:
            break
        line = rng.randrange(3)
        mode = "physical" if line == 2 else "virtual"
        dur = min(rng.uniform(60, 3600), now - 600 - t)
        fail = rng.random() < 0.08
        cid = store.start_call(line, mode, rng.choice(CONNECTS[mode]), t)
        rxb = 0 if fail else int(dur * rng.uniform(1500, 4800))
        store.update_call(cid, user=None if fail else rng.choice(users), ip=f"10.66.0.{2 + line}",
                          ended=t + (rng.uniform(3, 10) if fail else dur), rx_bytes=rxb, tx_bytes=int(rxb * 0.12),
                          result="auth_failed" if fail else "ok",
                          detail="PAP authentication failed" if fail else "pppd exited with status 16")
        if not fail:
            store.touch_login(rng.choice(users), t)


class DemoSim(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True, name="demo-sim")
        self.app = app
        self.rng = random.Random()
        self.lines = {}
        now = time.time()
        for key, n in DEMO_LINES.items():
            if n == UNPLUGGED_LINE:
                continue
            self.lines[n] = {"state": st.IDLE, "since": now, "until": now + self.rng.uniform(2, 15), "call": None,
                             "detail": ""}

    def mode(self, n):
        return self.app.cfg()["lines"]["modes"].get(str(n), self.app.cfg()["lines"]["default_mode"])

    def run(self):
        while True:
            now = time.time()
            st.write(os.path.join(self.app.run_dir, st.MANAGER_FILE), {"pid": os.getpid(), "updated": now})
            for n, line in self.lines.items():
                self.step(n, line, now)
                mode = self.mode(n)
                st.write(st.line_file(self.app.run_dir, n), {
                    "line": n, "mode": mode, "state": line["state"], "state_since": line["since"],
                    "detail": line["detail"], "baud": 115200, "updated": now, "call": line["call"]})
            time.sleep(1)

    def set(self, line, state, detail, now, until):
        line.update(state=state, detail=detail, since=now, until=now + until)

    def step(self, n, line, now):
        r = self.rng
        mode = self.mode(n)
        if line["call"]:
            line["call"]["rx_bytes"] += int(r.uniform(800, 6200) if r.random() < 0.8 else 0)
            line["call"]["tx_bytes"] += int(r.uniform(60, 700))
        if now < line["until"]:
            return
        s = line["state"]
        if s in (st.IDLE, st.MODEM_ERROR):
            if mode == "physical":
                if r.random() < 0.1:
                    return self.set(line, st.MODEM_ERROR, "Modem not responding to AT", now, 12)
                return self.set(line, st.RINGING, "RING 1", now, 6)
            return self.set(line, st.DIALING, "ATDT5551234", now, 2)
        if s == st.RINGING:
            return self.set(line, st.ANSWERING, "ATA", now, r.uniform(8, 15))
        if s in (st.DIALING, st.ANSWERING):
            connect = r.choice(CONNECTS[mode])
            user = r.choice(USERS)[0]
            line["call"] = {"connect": connect, "started": now, "rx_bytes": 0, "tx_bytes": 0, "user": user,
                            "ip": f"10.66.0.{2 + n}", "iface": f"ppp{n}"}
            line["call_id"] = self.app.store.start_call(n, mode, connect, now)
            self.app.store.update_call(line["call_id"], user=user, ip=line["call"]["ip"])
            self.app.store.touch_login(user, now)
            return self.set(line, st.CONNECTED, connect, now, r.uniform(40, 240))
        if s == st.CONNECTED:
            c = line["call"]
            self.app.store.update_call(line["call_id"], ended=now, rx_bytes=c["rx_bytes"], tx_bytes=c["tx_bytes"],
                                       result="ok", detail="pppd exited with status 16")
            line["call"] = None
            if mode == "physical":
                return self.set(line, st.INITIALIZING, "AT&FE0V1Q0X4&C1&D2S0=0", now, 2)
            return self.set(line, st.IDLE, "", now, r.uniform(5, 40))
        if s == st.INITIALIZING:
            return self.set(line, st.IDLE, "Waiting for a call", now, r.uniform(5, 40))


# simulated retro-LAN traffic per 5 s sample: (dir, proto) -> (packets, bytes per packet); a Doom game over IPX,
# a Win95 file share over NetBEUI, some TCP/IP, and one PC still on IPX 802.2 (the frame type mismatch warning)
DEMO_PROTO = {("lan", "ipx_8023"): (170, 96), ("party", "ipx_8023"): (160, 96), ("lan", "netbeui"): (14, 610),
              ("lan", "nbt"): (3, 240), ("lan", "tcp"): (42, 820), ("party", "tcp"): (9, 300), ("lan", "udp"): (6, 120),
              ("lan", "icmp"): (1, 98), ("lan", "arp"): (1, 60), ("party", "arp"): (1, 60), ("lan", "ipx_8022"): (2, 110),
              ("party", "ipx_ii"): (0, 0)}


def demo_proto_sample(collector, now, rng=random.Random(11)):
    busy = 0.4 + 0.6 * abs((now / 300) % 2 - 1)
    d = {k: (int(p * busy * rng.uniform(0.7, 1.3)), 0) for k, (p, _) in DEMO_PROTO.items() if p}
    d = {k: (p, p * DEMO_PROTO[k][1]) for k, (p, _) in d.items() if p}
    for key, (p, b) in d.items():
        acc = collector.proto_acc.setdefault(key, [0, 0])
        acc[0] += p
        acc[1] += b
    collector.proto_ring.append((int(now), d))
    collector.proto_prev = (0, {})


def seed_proto_history(store, now):
    rng = random.Random(42)
    start = int(now // 600 * 600) - 7 * 86400
    for ts in range(start, int(now // 600 * 600), 600):
        hour = time.localtime(ts).tm_hour
        busy = max(0.0, 1 - abs(hour - 21) / 4)              # game nights
        if busy <= 0:
            continue
        rows = []
        for (d, k), (p, size) in DEMO_PROTO.items():
            n = int(p * 120 * busy * rng.uniform(0.5, 1.5))
            if n:
                rows.append((d, k, n, n * size))
        store.add_proto_stats(ts, rows)


def setup_demo(base_cfg):
    root = tempfile.mkdtemp(prefix="netkey-demo-")
    state_dir, run_dir = os.path.join(root, "state"), os.path.join(root, "run")
    os.makedirs(state_dir)
    os.makedirs(run_dir)
    base_cfg = dict(base_cfg)
    base_cfg["lines"] = dict(base_cfg["lines"], max=8)
    share = os.path.join(root, "share")                 # the shared folder, with a few files to look at
    os.makedirs(os.path.join(share, "GAMES", "DUKE3D"))
    os.makedirs(os.path.join(share, "DRIVERS"))
    for rel, size in (("README.TXT", 1041), ("GAMES/DUKE3D/DUKE3D.ZIP", 6_200_000), ("DRIVERS/NE2000.COM", 3_212),
                      ("PATCHES.ZIP", 412_000)):
        with open(os.path.join(share, rel), "wb") as f:
            f.write(b"x" * size)
    base_cfg["share"] = dict(base_cfg["share"], dir=share)
    write_json_atomic(os.path.join(state_dir, "lines.json"), {"lines": DEMO_LINES})
    store = Store(state_dir)
    store.reset_admin()
    for name, password, note in USERS:
        store.add_user(name, password, note=note)
    store.update_user("retro", enabled=False)
    write_json_atomic(os.path.join(state_dir, "pap-synced.json"),
                      {"version": int(store.get_meta("users_version")), "at": time.time()})
    store.set_line_mode(2, "physical")
    seed_history(store, time.time())
    seed_proto_history(store, time.time())
    app = DemoApp(base_cfg, state_dir, run_dir, store)
    app.demo_party = DemoParty(socket.gethostname(), base_cfg.get("lan", {}).get("hub_url"))
    app.demo_net = DemoNetwork(app)
    app.demo_hub = DemoHub()
    from . import democloud
    if democloud.hub_source():
        try:
            app.demo_cloud = democloud.DemoCloud(socket.gethostname())
            app.demo_party.s["hub_party"] = app.demo_cloud.call(
                "POST", "/v1/party", {"host_pub": demo_key(), "name": socket.gethostname()})["party"]
            print(f"demo: public parties run against a real hub from {democloud.hub_source()}", flush=True)
        except Exception as e:                                 # the rest of the demo works without it
            print(f"demo: no demo hub ({e}); public parties limited to nicknames", flush=True)
    rng = random.Random(7)
    now = time.time()
    for i in range(LIVE_POINTS):
        lines = {n: (round(rng.uniform(1200, 5600), 1), round(rng.uniform(80, 600), 1))
                 for n in (0, 2) if rng.random() < 0.9}
        app.collector.ring.append({"t": int(now - (LIVE_POINTS - i) * SAMPLE_S), "connected": len(lines),
                                   "present": 3, "rx_bps": round(sum(v[0] for v in lines.values()), 1),
                                   "tx_bps": round(sum(v[1] for v in lines.values()), 1), "lines": lines})
    app.collector.sample_protocols = lambda t: demo_proto_sample(app.collector, t)
    for i in range(LIVE_POINTS):
        demo_proto_sample(app.collector, now - (LIVE_POINTS - i) * SAMPLE_S)
    app.collector.proto_acc = {}
    DemoSim(app).start()
    # pretend the root helper picks up user changes after a second
    threading.Thread(target=fake_sync, args=(store,), daemon=True).start()
    print(f"demo state in {root}; login admin / admin", flush=True)
    return app


def fake_sync(store):
    while True:
        time.sleep(1.5)
        s = store.users_sync_state()
        if s["pending"]:
            write_json_atomic(os.path.join(store.state_dir, "pap-synced.json"), {"version": s["version"],
                                                                                 "at": time.time()})
