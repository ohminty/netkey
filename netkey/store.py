"""State database (SQLite, /var/lib/netkey/netkey.db): dial-in users, GUI settings, call history, stats.

Written only by the netkey user (web GUI, modem lines). Root never opens it: the user list is exported to
pap-users.json, which netkey-users.path turns into the /etc/ppp/pap-secrets block.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time

from .config import check_user

DB_NAME = "netkey.db"
USERS_EXPORT = "pap-users.json"     # watched by netkey-users.path (root re-renders pap-secrets)
USERS_SYNCED = "pap-synced.json"    # written by the root helper after pap-secrets was updated
SETTINGS_STAMP = "settings.stamp"   # touched on settings change; the manager restarts affected lines
SETTINGS_EXPORT = "settings.json"   # GUI overrides for root services (netkey-lan), which never open the DB
STATS_WRITE_S = 600                 # traffic statistics wait in memory this long, then go to the SD card in one go
PBKDF2_ITERATIONS = 60000           # ~0.5 s on a Pi 1
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"    # factory default; the GUI nags until it is changed

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS line_modes (line INTEGER PRIMARY KEY, mode TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS users (
        name TEXT PRIMARY KEY, password TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
        note TEXT NOT NULL DEFAULT '', created REAL NOT NULL, last_login REAL)""",
    """CREATE TABLE IF NOT EXISTS calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, line INTEGER NOT NULL, mode TEXT NOT NULL, user TEXT,
        ip TEXT, connect TEXT, started REAL NOT NULL, ended REAL, rx_bytes INTEGER NOT NULL DEFAULT 0,
        tx_bytes INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')""",
    """CREATE INDEX IF NOT EXISTS calls_started ON calls(started)""",
    """CREATE TABLE IF NOT EXISTS stats (
        ts INTEGER NOT NULL, line INTEGER NOT NULL, present INTEGER NOT NULL, connected INTEGER NOT NULL,
        rx_bytes INTEGER NOT NULL, tx_bytes INTEGER NOT NULL, PRIMARY KEY (ts, line))""",
    """CREATE TABLE IF NOT EXISTS proto_stats (
        ts INTEGER NOT NULL, dir TEXT NOT NULL, proto TEXT NOT NULL, packets INTEGER NOT NULL,
        bytes INTEGER NOT NULL, PRIMARY KEY (ts, dir, proto))""",
]


def hash_password(password, salt=None, iterations=PBKDF2_ITERATIONS):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password, stored):
    try:
        _, iterations, salt, _ = stored.split("$")
        return hmac.compare_digest(hash_password(password, salt, int(iterations)), stored)
    except (AttributeError, ValueError):
        return False


def write_atomic(path, text, mode=0o640):
    """Write a file in one step. The temporary file gets a fresh random name (O_EXCL), so a symlink someone planted
    in the folder is never followed: root services write into /var/lib/netkey, which the web service owns."""
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix="." + os.path.basename(path) + ".")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json_atomic(path, data, mode=0o640):
    write_atomic(path, json.dumps(data, indent=1, sort_keys=True), mode)


READ_LIMIT = 8 * 1024 * 1024        # no state file the services share comes close


def read_json(path, default=None, limit=READ_LIMIT):
    """Read a JSON file the safe way for root: /var/lib/netkey belongs to the web service's user, so whatever is
    there could be planted. Never follows a symlink (no reading /etc/shadow through it), never blocks on a FIFO or
    device, and refuses anything that is not a regular file or is bigger than `limit`."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return default
    try:
        st = os.fstat(fd)
        if (st.st_mode & 0o170000) != 0o100000 or st.st_size > limit:
            return default
        chunks, left = [], limit + 1
        while left > 0:
            b = os.read(fd, min(left, 1 << 20))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        data = b"".join(chunks)
        if len(data) > limit:
            return default
        return json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        return default
    finally:
        os.close(fd)


class Store:
    """Thread-safe wrapper around one SQLite connection (WAL, so several processes can share the file)."""

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, DB_NAME)
        self.lock = threading.RLock()
        old_umask = os.umask(0o027)
        try:
            self.db = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False)
        finally:
            os.umask(old_umask)
        self.db.row_factory = sqlite3.Row
        # finished minutes of statistics not written yet (SD card wear: one transaction every STATS_WRITE_S instead
        # of one a minute); the read methods below add them in, so charts are never behind
        self.pending_stats = {}          # (ts, line) -> (present, connected, rx, tx)
        self.pending_proto = {}          # (ts, dir, proto) -> [packets, bytes]
        self.stats_written = time.monotonic()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            for stmt in SCHEMA:
                self.db.execute(stmt)

    def close(self):
        with self.lock:
            try:
                self.write_stats(force=True)
            except sqlite3.Error:
                pass
            self.db.close()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # meta
    def get_meta(self, key, default=None):
        rows = self.q("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key, value):
        self.x("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, str(value)))

    def admin_hash(self):
        return self.get_meta("admin_hash")

    def admin_user(self):
        return self.get_meta("admin_user", DEFAULT_ADMIN_USER)

    def set_admin_password(self, password):
        if len(password) < 6:
            raise ValueError("admin password must be at least 6 characters")
        self.set_meta("admin_hash", hash_password(password))
        self.set_meta("admin_default", "0")

    def set_admin_user(self, name):
        name = str(name).strip()
        if not 1 <= len(name) <= 32 or any(ord(ch) < 33 or ord(ch) == 127 for ch in name):
            raise ValueError("username: 1-32 characters, no spaces")
        self.set_meta("admin_user", name)

    def reset_admin(self):
        """Factory default login admin / admin (install when unset, or --reset-admin)."""
        self.set_meta("admin_user", DEFAULT_ADMIN_USER)
        self.set_meta("admin_hash", hash_password(DEFAULT_ADMIN_PASSWORD))
        self.set_meta("admin_default", "1")

    def admin_is_default(self):
        return self.get_meta("admin_default") == "1"

    def check_admin_password(self, password):
        return verify_password(password, self.admin_hash())

    def check_admin_login(self, user, password):
        user_ok = hmac.compare_digest(str(user).encode(), self.admin_user().encode())
        return self.check_admin_password(password) and user_ok

    # settings
    def get_settings(self):
        return {r["key"]: json.loads(r["value"]) for r in self.q("SELECT key, value FROM settings")}

    def set_settings(self, values):
        """values: {key: value or None (None = back to the config.toml default)}. Caller validates."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for key, value in values.items():
                    if value is None:
                        self.db.execute("DELETE FROM settings WHERE key=?", (key,))
                    else:
                        self.db.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                        (key, json.dumps(value)))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        self.touch_settings()

    def get_line_modes(self):
        return {str(r["line"]): r["mode"] for r in self.q("SELECT line, mode FROM line_modes")}

    def set_line_mode(self, line, mode):
        """mode None removes the per-line override (the line follows the default mode)."""
        if mode is None:
            self.x("DELETE FROM line_modes WHERE line=?", (int(line),))
        else:
            self.x("INSERT INTO line_modes(line, mode) VALUES(?, ?) "
                   "ON CONFLICT(line) DO UPDATE SET mode=excluded.mode", (int(line), mode))
        self.touch_settings()

    def touch_settings(self):
        self.export_settings()
        write_atomic(os.path.join(self.state_dir, SETTINGS_STAMP), f"{time.time()} {secrets.token_hex(4)}\n")

    def export_settings(self):
        write_json_atomic(os.path.join(self.state_dir, SETTINGS_EXPORT),
                          {"settings": self.get_settings(), "line_modes": self.get_line_modes(),
                           "updated": time.time()})

    # users
    def list_users(self):
        return self.q("""SELECT u.name, u.password, u.enabled, u.note, u.created, u.last_login,
                                COUNT(c.id) AS calls, COALESCE(SUM(c.rx_bytes + c.tx_bytes), 0) AS bytes
                         FROM users u LEFT JOIN calls c ON c.user = u.name
                         GROUP BY u.name ORDER BY u.name COLLATE NOCASE""")

    def get_user(self, name):
        rows = [u for u in self.list_users() if u["name"] == name]
        return rows[0] if rows else None

    def add_user(self, name, password, enabled=True, note=""):
        check_user(name, password)
        try:
            self.x("INSERT INTO users(name, password, enabled, note, created) VALUES(?, ?, ?, ?, ?)",
                   (name, password, int(bool(enabled)), str(note)[:200], time.time()))
        except sqlite3.IntegrityError:
            raise KeyError(name) from None
        self.export_users()
        return self.get_user(name)

    def update_user(self, name, password=None, enabled=None, note=None):
        user = self.get_user(name)
        if user is None:
            raise KeyError(name)
        if password is not None:
            check_user(name, password)
            user["password"] = password
        if enabled is not None:
            user["enabled"] = int(bool(enabled))
        if note is not None:
            user["note"] = str(note)[:200]
        self.x("UPDATE users SET password=?, enabled=?, note=? WHERE name=?",
               (user["password"], user["enabled"], user["note"], name))
        self.export_users()
        return self.get_user(name)

    def delete_user(self, name):
        if self.x("DELETE FROM users WHERE name=?", (name,)).rowcount == 0:
            raise KeyError(name)
        self.export_users()

    def touch_login(self, name, when=None):
        self.x("UPDATE users SET last_login=? WHERE name=?", (when or time.time(), name))

    def seed_users(self, users):
        """Import config [auth] users once, on first start."""
        if self.get_meta("users_seeded"):
            return 0
        n = 0
        for u in users:
            try:
                self.add_user(str(u["name"]), str(u["password"]), note="imported from config.toml")
                n += 1
            except (KeyError, ValueError):
                pass
        self.set_meta("users_seeded", "1")
        self.export_users()
        return n

    def export_users(self):
        """Write the enabled/disabled user list for the root pap-secrets helper; bumps the version."""
        version = int(self.get_meta("users_version", "0")) + 1
        self.set_meta("users_version", version)
        users = [{"name": u["name"], "password": u["password"], "enabled": bool(u["enabled"])}
                 for u in self.q("SELECT name, password, enabled FROM users ORDER BY name")]
        write_json_atomic(os.path.join(self.state_dir, USERS_EXPORT),
                          {"version": version, "updated": time.time(), "users": users})
        return version

    def users_sync_state(self):
        export = read_json(os.path.join(self.state_dir, USERS_EXPORT), {}) or {}
        synced = read_json(os.path.join(self.state_dir, USERS_SYNCED), {}) or {}
        return {"version": export.get("version"), "synced_version": synced.get("version"),
                "synced_at": synced.get("at"),
                "pending": export.get("version") is not None and export.get("version") != synced.get("version"),
                "error": synced.get("error")}

    # calls
    def start_call(self, line, mode, connect, started=None, result="connected"):
        return self.x("INSERT INTO calls(line, mode, connect, started, result) VALUES(?, ?, ?, ?, ?)",
                      (line, mode, connect, started or time.time(), result)).lastrowid

    def update_call(self, call_id, **fields):
        allowed = {"user", "ip", "connect", "ended", "rx_bytes", "tx_bytes", "result", "detail"}
        cols = [k for k in fields if k in allowed]
        if cols:
            self.x(f"UPDATE calls SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                   [fields[k] for k in cols] + [call_id])

    def list_calls(self, limit=100, since=None):
        rows = self.q("SELECT * FROM calls WHERE started >= ? ORDER BY started DESC LIMIT ?",
                      (since or 0, int(limit)))
        now = time.time()
        for r in rows:
            r["duration_s"] = round((r["ended"] or now) - r["started"], 1)
        return rows

    def close_stale_calls(self, line, now=None):
        """Calls left open by a crashed worker are closed so the history never shows phantom sessions."""
        self.x("UPDATE calls SET ended=?, result='dropped', detail='line restarted' "
               "WHERE line=? AND ended IS NULL", (now or time.time(), line))

    def call_counts(self, since):
        rows = self.q("SELECT COUNT(*) AS n, COALESCE(SUM(rx_bytes + tx_bytes), 0) AS b, "
                      "COALESCE(SUM(result='auth_failed'), 0) AS f FROM calls WHERE started >= ?", (since,))
        return rows[0]

    # stats
    def add_stats(self, ts, rows):
        """rows: [(line, present, connected, rx_bytes, tx_bytes)] for one minute bucket ts. Kept in memory until
        write_stats() (on its own every STATS_WRITE_S)."""
        with self.lock:
            for r in rows:
                self.pending_stats[(int(ts), int(r[0]))] = tuple(r[1:5])
        self.write_stats()

    def add_proto_stats(self, ts, rows):
        """rows: [(dir, proto, packets, bytes)] for one minute bucket ts (retro-LAN protocol counters)."""
        with self.lock:
            for d, proto, packets, nbytes in rows:
                a = self.pending_proto.setdefault((int(ts), d, proto), [0, 0])
                a[0] += packets
                a[1] += nbytes
        self.write_stats()

    def write_stats(self, force=False):
        """Write the waiting statistics in one transaction: when STATS_WRITE_S has passed, or now with force."""
        with self.lock:
            if not force and time.monotonic() - self.stats_written < STATS_WRITE_S:
                return
            self.stats_written = time.monotonic()
            if not self.pending_stats and not self.pending_proto:
                return
            self.db.execute("BEGIN")
            try:
                self.db.executemany("INSERT OR REPLACE INTO stats(ts, line, present, connected, rx_bytes, "
                                    "tx_bytes) VALUES(?, ?, ?, ?, ?, ?)",
                                    [k + v for k, v in self.pending_stats.items()])
                self.db.executemany("INSERT INTO proto_stats(ts, dir, proto, packets, bytes) VALUES(?, ?, ?, ?, ?) "
                                    "ON CONFLICT(ts, dir, proto) DO UPDATE SET packets = packets + excluded.packets, "
                                    "bytes = bytes + excluded.bytes",
                                    [k + tuple(v) for k, v in self.pending_proto.items()])
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.pending_stats, self.pending_proto = {}, {}

    def _pending_proto_since(self, since):
        return [(k, v) for k, v in list(self.pending_proto.items()) if k[0] >= int(since)]

    def proto_totals_since(self, since):
        with self.lock:
            rows = self.q("SELECT dir, proto, SUM(packets) AS packets, SUM(bytes) AS bytes FROM proto_stats "
                          "WHERE ts >= ? GROUP BY dir, proto", (int(since),))
            pend = self._pending_proto_since(since)
        out = {(r["dir"], r["proto"]): r for r in rows}
        for (_, d, proto), (p, b) in pend:
            r = out.setdefault((d, proto), {"dir": d, "proto": proto, "packets": 0, "bytes": 0})
            r["packets"] = (r["packets"] or 0) + p
            r["bytes"] = (r["bytes"] or 0) + b
        return list(out.values())

    def proto_dir_series(self, since):
        """Retro-LAN bytes per minute bucket and direction ('lan' = from this box's PCs, 'party' = from the tunnels)."""
        with self.lock:
            rows = self.q("SELECT ts, dir, SUM(bytes) AS bytes FROM proto_stats WHERE ts >= ? GROUP BY ts, dir",
                          (int(since),))
            pend = self._pending_proto_since(since)
        out = {(r["ts"], r["dir"]): r for r in rows}
        for (ts, d, _), (_, b) in pend:
            r = out.setdefault((ts, d), {"ts": ts, "dir": d, "bytes": 0})
            r["bytes"] = (r["bytes"] or 0) + b
        return sorted(out.values(), key=lambda r: r["ts"])

    def proto_dir_bytes_since(self, since):
        out = {"lan": 0, "party": 0}
        for r in self.proto_dir_series(since):
            out[r["dir"]] = out.get(r["dir"], 0) + (r["bytes"] or 0)
        return out

    def stats_since(self, since):
        with self.lock:
            rows = self.q("SELECT ts, line, present, connected, rx_bytes, tx_bytes FROM stats WHERE ts >= ? "
                          "ORDER BY ts", (int(since),))
            have = {(r["ts"], r["line"]) for r in rows}
            rows += [{"ts": ts, "line": line, "present": v[0], "connected": v[1], "rx_bytes": v[2], "tx_bytes": v[3]}
                     for (ts, line), v in list(self.pending_stats.items()) if ts >= int(since) and (ts, line) not in have]
        return sorted(rows, key=lambda r: r["ts"])

    def stats_bytes_since(self, since):
        with self.lock:
            r = self.q("SELECT COALESCE(SUM(rx_bytes), 0) AS rx, COALESCE(SUM(tx_bytes), 0) AS tx FROM stats "
                       "WHERE ts >= ?", (int(since),))[0]
            for (ts, _), v in list(self.pending_stats.items()):
                if ts >= int(since):
                    r["rx"] += v[2]
                    r["tx"] += v[3]
        return r

    def prune(self, keep_days):
        self.write_stats(force=True)
        cutoff = time.time() - keep_days * 86400
        self.x("DELETE FROM stats WHERE ts < ?", (int(cutoff),))
        self.x("DELETE FROM proto_stats WHERE ts < ?", (int(cutoff),))
        self.x("DELETE FROM calls WHERE started < ?", (time.time() - max(keep_days, 90) * 86400,))
