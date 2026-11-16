"""--demo on a development machine: a real cloud hub in this process, so the Parties page runs against the hub's own
logic (directory, listings, RSVP, votes, chat, bans) instead of a stand-in.

Used only when the hub project is next to the box repo (../netkey-hub, or NETKEY_HUB_SRC). The hub listens on
127.0.0.1 with plain HTTP and is seeded with other boxes, public parties, planned events and some chat.
"""
import base64
import http.client
import json
import os
import random
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def hub_source():
    src = os.environ.get("NETKEY_HUB_SRC") or os.path.join(HERE, "..", "..", "netkey-hub")
    return src if os.path.isfile(os.path.join(src, "netkey_hub", "api.py")) else None


def key():
    return base64.b64encode(os.urandom(32)).decode()


# nickname, box name, round-trip time to the demo relay (ms)
BOXES = [("alice", "ALICE-WIN98", 17), ("bobcat", "BOB-P133", 10), ("dosbox-den", "dosbox-den", 12),
         ("k6-2-kai", "K6-2-KAI", 22), ("orc-peon", "PEON95", 32), ("zugzug", "ZUG-PC", 35),
         ("pentium-mmx", "MMX233", 15), ("voodoo3", "V3-2000", 19), ("quakebox", "quakebox", 104),
         ("ranger", "RANGER", 98), ("cnc-fan", "RA-BOX", 21), ("sb16", "SB16", 26), ("tanya", "TANYA-PC", 24)]
LIVE = [  # host, title, description, games, max, auto, members
    ("alice", "Friday Doom Night", "Casual deathmatch and co-op on DOS 486s and Win98 boxes. New players welcome, "
     "we'll help you get IPX going. Bring your own WADs if you have favourites.", ["doom2", "heretic"], 8, False,
     ["bobcat", "dosbox-den", "k6-2-kai"]),
    ("orc-peon", "WC2 ladder warmup", "2v2 practice before the Sunday ladder. One slot left.", ["wc2"], 4, False,
     ["zugzug"]),
    ("pentium-mmx", "CS 1.6 & UT99 night", "de_dust2 until someone complains, then UT99 instagib. TetriNET in the "
     "breaks. Auto-approve is on, just join.", ["cs", "ut", {"name": "TetriNET", "proto": "TCP/IP"}], 12, True,
     ["voodoo3"]),
    ("quakebox", "Quake LAN '97", "East-coast Quake night on real hardware. TCP/IP, dedicated server on the host's PC.",
     ["quake", "quake2"], 16, False, ["ranger"]),
]
PLANNED = [  # host, title, description, games, max, days ahead, hour, repeat, rsvps
    ("bobcat", "Saturday Duke3D Marathon", "All three episodes in co-op, then Dukematch until people fall asleep. "
     "v1.5 Atomic on every PC please. C&C if we get bored.", ["duke", "cnc"], 8, 7, 20, "none",
     ["alice", "k6-2-kai", "sb16"]),
    ("cnc-fan", "Retro Sunday: Red Alert & AoE II", "Free-for-all and 2v2s. Win95/98 with IPX.", ["ra", "aoe2"], 8, 8,
     15, "none", ["tanya"]),
    ("dosbox-den", "Monthly 486 Night", "First Friday of every month. Real DOS hardware (or close to it). We rotate "
     "games every hour.", ["doom2", "descent", "rott", "diablo"], 16, 20, 19, "monthly", ["alice", "bobcat", "sb16"]),
]
CHAT = [("alice", "evening all! doom2 first, map01 dm, then co-op?"), ("bobcat", "sounds good. my 486 is on 802.2, everyone same?"),
        ("alice", "yep 802.2 everywhere tonight"), ("dosbox-den", "joining in 5, swapping the NIC"),
        ("k6-2-kai", "anyone up for heretic after?"), ("bobcat", "+1 heretic")]
CHATTER = ["restarting ipxsetup, rejoin in 30s", "gg", "map07 next?", "who has the 1.9 patch?", "brb, coffee",
           "my 3c509 just died, swapping", "that rocket was not fair", "ok everyone ready?"]


def demo_release(repo):
    """The demo hub's "GitHub": one patch release newer than this box, so the update notice shows."""
    from .web import app_version, parse_version
    cur = parse_version(app_version())
    major, minor, patch = cur[0] if cur else (0, 10, 3)
    tag = f"v{major}.{minor}.{patch + 1}"
    return {"tag_name": tag, "html_url": f"https://github.com/{repo}/releases/tag/{tag}", "published_at": "2026-11-14T18:00:00Z",
            "body": "- Saved Wi-Fi networks: up to 10, the box joins the preferred one, else any saved one in range\n"
                    "- Dashboard about LAN parties\n- Update notices"}


class DemoCloud:
    def __init__(self, box_name, my_rtt=14):
        sys.path.insert(0, hub_source())
        from netkey_hub.api import Hub, serve
        self.hub = Hub(tempfile.mkdtemp(prefix="netkey-demo-hub-"), "127.0.0.1", "127.0.0.1", (47950, 47989),
                       local_name="de-fra", release_repo="ohminty/netkey", release_fetch=demo_release)
        self.hub.releases.check()              # before the first box asks
        self.hub.start()
        limit = self.hub.rate                  # every demo box comes from 127.0.0.1: no per-IP limits for seeding
        self.hub.rate = lambda k, n, window=60: None if k[0] in ("register", "join", "nick") else limit(k, n, window)
        self.srv = serve(self.hub, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        self.tokens = {}
        self.token, self.box = self.register(box_name)
        self.hub.store.set_rtt(self.box, {"local": my_rtt})
        self.seed()
        threading.Thread(target=self.chatter, daemon=True, name="demo-chatter").start()

    def call(self, method, path, body=None, token=None, timeout=15):
        from .web import PartiesError
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = {"Content-Type": "application/json", "X-NetKey-Proto": "3", "Authorization": f"Bearer {token or self.token}"}
        try:
            c.request(method, path, json.dumps(body) if body is not None else None, h)
            r = c.getresponse()
            data = json.loads(r.read() or b"{}")
        except OSError as e:
            raise PartiesError(502, f"Cannot reach the demo hub: {e}") from None
        finally:
            c.close()
        if r.status != 200:
            raise PartiesError(r.status, data.get("error") or f"hub error {r.status}")
        return data

    def register(self, name):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/v1/register", json.dumps({"name": name, "identity": key()}),
                  {"Content-Type": "application/json", "X-NetKey-Proto": "3"})
        d = json.loads(c.getresponse().read())
        c.close()
        return d["token"], d["box"]

    def seed(self):
        h, now = self.hub, time.time()
        for nick, name, rtt in BOXES:
            token, bid = self.register(name)
            self.tokens[nick] = (token, bid)
            self.call("POST", "/v1/nick", {"nick": nick}, token)
            h.store.set_rtt(bid, {"local": rtt})
        for host, title, desc, games, mx, auto, members in LIVE:
            r = self.call("POST", "/v1/party", {"host_pub": key(), "public": {
                "title": title, "description": desc, "games": games, "lang": "EN", "max": mx, "auto": auto}},
                self.tokens[host][0])
            for i, m in enumerate(members, 1):
                self.call("POST", "/v1/join", {"listing": r["listing"], "pub": key()}, self.tokens[m][0])
                self.call("POST", "/v1/party/approve", {"box": self.tokens[m][1], "member_id": i}, self.tokens[host][0])
            h.store.x("UPDATE listings SET started=? WHERE id=?", (now - random.randint(10, 120) * 60, r["listing"]))
            if title == "Friday Doom Night":
                self.doom = r["listing"]
                for i, (nick, text) in enumerate(CHAT):
                    h.store.x("INSERT INTO chat(room, box, nick, text, created) VALUES(?, ?, ?, ?, ?)",
                              ("l:" + r["listing"], self.tokens[nick][1], nick, text, now - (len(CHAT) - i) * 240))
                for nick, game in (("bobcat", "doom2"), ("dosbox-den", "doom2"), ("k6-2-kai", "heretic"),
                                   ("bobcat", "heretic"), ("alice", "doom2")):
                    h.store.x("INSERT OR IGNORE INTO votes(listing, box, game) VALUES(?, ?, ?)",
                              (r["listing"], self.tokens[nick][1], game))
        day = 86400
        midnight = now - now % day
        for host, title, desc, games, mx, ahead, hour, repeat, rsvps in PLANNED:
            starts = midnight + ahead * day + hour * 3600
            ev = self.call("POST", "/v1/listing", {"title": title, "description": desc, "games": games, "lang": "EN",
                                                   "max": mx, "auto": False, "starts": starts, "repeat": repeat},
                           self.tokens[host][0])
            for nick in rsvps:
                self.call("POST", f"/v1/listing/{ev['id']}/rsvp", {"going": True}, self.tokens[nick][0])
            h.store.x("INSERT INTO chat(room, box, nick, text, created) VALUES(?, ?, ?, ?, ?)",
                      ("l:" + ev["id"], self.tokens[host][1], host, "Event's up! Say hi if you're coming.", now - 2 * day))

    def chatter(self):
        """Someone in Friday Doom Night says something now and then."""
        people = ["alice", "bobcat", "dosbox-den", "k6-2-kai"]
        while True:
            time.sleep(random.uniform(20, 40))
            nick = random.choice(people)
            try:
                self.call("POST", "/v1/chat", {"room": "l:" + self.doom, "text": random.choice(CHATTER)},
                          self.tokens[nick][0])
            except Exception:
                pass

    def approve_me_later(self, listing, delay=3.0):
        """The (simulated) host of a public party approves this box's join request after a moment."""
        def go():
            time.sleep(delay)
            try:
                lst = self.hub.listing_by_ref(listing)
                host = next(t for t, b in self.tokens.values() if b == lst["host_box"])
                used = {m["member_id"] for m in self.hub.store.members(lst["party_id"]) if m["member_id"]}
                mid = next(i for i in range(1, 16) if i not in used)
                self.call("POST", "/v1/party/approve", {"box": self.box, "member_id": mid}, host)
            except Exception:
                pass
        threading.Thread(target=go, daemon=True).start()
