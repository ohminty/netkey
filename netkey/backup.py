"""Backup and restore of what makes a box yours: settings, dial-in users, per-line modes, the phone book and the box
identity (so a new SD card or a new Pi keeps the nickname and the hub's view of the box).

Not in it: call history and traffic statistics (they rebuild), Wi-Fi passwords (NetworkManager keeps those, root only),
the hub tokens and the phone number (the box gets them again from the hub with its identity), the admin login (the
person restoring is already signed in with one) and the shared files (copy those over the network).

The dial-in passwords must be stored readable (PAP needs them) and the identity is a secret, so the file is always
encrypted with a passphrase the admin chooses. Standard library only, because the box has nothing else:
  key   = scrypt(passphrase, salt, n=2**14, r=8, p=1) -> 64 bytes: 32 for encryption, 32 for authentication
  data  = plaintext XOR HMAC-SHA256(enc_key, nonce || counter) blocks (a PRF in counter mode)
  mac   = HMAC-SHA256(mac_key, header || nonce || data), checked before anything is decrypted
A wrong passphrase and a changed file look the same: the MAC does not match.
"""
import base64
import hashlib
import hmac
import json
import os
import socket
import time

FORMAT = "netkey-backup"
VERSION = 1
KDF = {"name": "scrypt", "n": 2 ** 14, "r": 8, "p": 1}
MAX_FILE = 1024 * 1024              # a backup is a few KB; refuse anything near this
MIN_PASSPHRASE = 8


class BackupError(ValueError):
    pass


def _keys(passphrase, salt, kdf):
    if kdf.get("name") != "scrypt" or not (2 ** 10 <= int(kdf.get("n", 0)) <= 2 ** 16) \
            or not (1 <= int(kdf.get("r", 0)) <= 16) or not (1 <= int(kdf.get("p", 0)) <= 4):
        raise BackupError("This backup uses settings this box does not accept.")
    k = hashlib.scrypt(str(passphrase).encode(), salt=salt, n=int(kdf["n"]), r=int(kdf["r"]), p=int(kdf["p"]),
                       maxmem=64 * 1024 * 1024, dklen=64)
    return k[:32], k[32:]


def _stream(key, nonce, data):
    out = bytearray(len(data))
    for i in range(0, len(data), 32):
        block = hmac.new(key, nonce + (i // 32).to_bytes(8, "big"), hashlib.sha256).digest()
        chunk = data[i:i + 32]
        out[i:i + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, block))
    return bytes(out)


def _header_bytes(header):
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode()


def seal(payload, passphrase, now=None):
    """payload (dict) -> the backup file's bytes."""
    if len(str(passphrase or "")) < MIN_PASSPHRASE:
        raise BackupError(f"Choose a passphrase of at least {MIN_PASSPHRASE} characters.")
    salt, nonce = os.urandom(16), os.urandom(16)
    header = {"format": FORMAT, "v": VERSION, "kdf": dict(KDF, salt=base64.b64encode(salt).decode()),
              "made": int(now or time.time()), "box": str(payload.get("box", {}).get("name", ""))[:64],
              "netkey": str(payload.get("box", {}).get("version", ""))[:20]}
    enc, mac = _keys(passphrase, salt, header["kdf"])
    data = _stream(enc, nonce, json.dumps(payload, separators=(",", ":")).encode())
    tag = hmac.new(mac, _header_bytes(header) + nonce + data, hashlib.sha256).digest()
    out = dict(header, nonce=base64.b64encode(nonce).decode(), data=base64.b64encode(data).decode(),
               mac=base64.b64encode(tag).decode())
    return (json.dumps(out, indent=1) + "\n").encode()


def peek(raw):
    """The readable part of a backup (when, which box, which version), without the passphrase."""
    d = _parse(raw)
    return {k: d.get(k) for k in ("made", "box", "netkey")}


def _parse(raw):
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_FILE:
            raise BackupError("That file is too big to be a NetKey backup.")
        try:
            raw = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BackupError("That is not a NetKey backup file.") from None
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise BackupError("That is not a NetKey backup file.")
    if raw.get("v") != VERSION:
        raise BackupError("This backup was made by a newer NetKey. Update this box first.")
    return raw


def open_sealed(raw, passphrase):
    """The backup file (bytes or the parsed JSON) -> payload dict. BackupError on a wrong passphrase or damage."""
    d = _parse(raw)
    try:
        header = {k: d[k] for k in ("format", "v", "kdf", "made", "box", "netkey")}
        salt = base64.b64decode(d["kdf"]["salt"], validate=True)
        nonce = base64.b64decode(d["nonce"], validate=True)
        data = base64.b64decode(d["data"], validate=True)
        tag = base64.b64decode(d["mac"], validate=True)
    except (KeyError, TypeError, ValueError):
        raise BackupError("The backup file is damaged.") from None
    enc, mac = _keys(passphrase, salt, header["kdf"])
    if not hmac.compare_digest(hmac.new(mac, _header_bytes(header) + nonce + data, hashlib.sha256).digest(), tag):
        raise BackupError("Wrong passphrase, or the file was changed.")
    try:
        payload = json.loads(_stream(enc, nonce, data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BackupError("The backup file is damaged.") from None
    if not isinstance(payload, dict):
        raise BackupError("The backup file is damaged.")
    return payload


# ---- what goes in, and putting it back
def collect(app, version=""):
    """The payload for app (web.App): everything listed in the module docstring."""
    from . import identity as ident
    st = app.store
    users = [{"name": u["name"], "password": u["password"], "enabled": bool(u["enabled"]), "note": u["note"] or ""}
             for u in st.q("SELECT name, password, enabled, note FROM users ORDER BY name")]
    out = {"box": {"name": socket.gethostname(), "version": version}, "settings": st.get_settings(),
           "line_modes": st.get_line_modes(), "users": users, "phone_book": app.phone_book()}
    try:
        out["identity"] = {"secret": ident.load(app.state_dir)["secret"]}
    except (ValueError, OSError):
        pass                                   # no identity (yet): nothing to carry over
    return out


def apply(app, payload, base_get_setting, check_setting, check_user):
    """Put a payload back. Every part is checked first; only then is anything changed. Returns a report:
    {"settings": n, "skipped": [keys], "users": n, "line_modes": n, "phone_book": n, "identity": bool}."""
    from . import identity as ident
    from .config import MODES
    if not isinstance(payload, dict):
        raise BackupError("The backup file is damaged.")
    # settings: the ones this version knows, each checked; the rest are listed, not fatal (older/newer versions)
    settings, skipped = {}, []
    for key, value in (payload.get("settings") or {}).items():
        try:
            v = check_setting(key, value)
            settings[key] = None if v == base_get_setting(key) else v
        except (ValueError, KeyError, TypeError):
            skipped.append(str(key))
    users = []
    for u in payload.get("users") or []:
        if not isinstance(u, dict):
            raise BackupError("The backup's user list is damaged.")
        name, pw = str(u.get("name", "")), str(u.get("password", ""))
        try:
            check_user(name, pw)
        except ValueError as e:
            raise BackupError(f"Dial-in user {name!r} in the backup is not valid: {e}") from None
        users.append((name, pw, bool(u.get("enabled", True)), str(u.get("note") or "")[:200]))
    if len({n.lower() for n, *_ in users}) != len(users):
        raise BackupError("The backup lists a dial-in user twice.")
    modes = {}
    for line, mode in (payload.get("line_modes") or {}).items():
        if not str(line).isdigit():
            raise BackupError("The backup's line modes are damaged.")
        if mode is not None and mode not in MODES:
            raise BackupError(f"The backup has an unknown modem mode {mode!r}.")
        modes[int(line)] = mode
    try:
        book = app.set_phone_book(payload.get("phone_book") or [], check_only=True)
    except ValueError as e:
        raise BackupError(f"The backup's phone book is not valid: {e}") from None
    secret = (payload.get("identity") or {}).get("secret")
    if secret is not None:
        ident.box_id(secret)                   # ValueError if it is not a real identity
    # all checked: now change things
    current = set(app.store.get_settings())
    app.store.set_settings(dict({k: None for k in current - set(settings)}, **settings))
    for old in app.store.get_line_modes():
        if int(old) not in modes:
            app.store.set_line_mode(int(old), None)
    for line, mode in modes.items():
        app.store.set_line_mode(line, mode)
    with app.store.lock:
        app.store.x("DELETE FROM users")
        for name, pw, enabled, note in users:
            app.store.x("INSERT INTO users(name, password, enabled, note, created) VALUES(?, ?, ?, ?, ?)",
                        (name, pw, int(enabled), note, time.time()))
    app.store.export_users()
    app.set_phone_book(book)
    changed_identity = False
    if secret is not None:
        try:
            now = ident.load(app.state_dir)["secret"]
        except (ValueError, OSError):
            now = None
        if now != secret:
            ident.replace(app.state_dir, secret)
            changed_identity = True
    return {"settings": len(settings), "skipped": skipped, "users": len(users), "line_modes": len(modes),
            "phone_book": len(book), "identity": changed_identity}
