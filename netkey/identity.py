"""The box's permanent identity for the cloud hub (public parties: nicknames, RSVPs, bans).

On first use the box writes box-identity.json in its state directory: a secret of 32 random bytes and a fingerprint
of the Pi it was made on. The hub derives the permanent box ID from the secret (it never stores the secret), so the ID
survives the hub forgetting the box and both box processes (GUI, LAN service) can register as the same box.
If the file turns up on another Pi (an SD card image copied to several Pis), the box sets it aside and makes a new
one, so copied Pis don't share an ID. The derivation must match netkey_hub/nicks.py in the hub project.
"""
import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import time

log = logging.getLogger("netkey")

FILE = "box-identity.json"
ID_PREFIX = "nk-"
SERIAL_PATHS = ("/sys/firmware/devicetree/base/serial-number",)
CPUINFO = "/proc/cpuinfo"
MACHINE_ID = "/etc/machine-id"
OWNER = "netkey"                    # the GUI process runs as this user; root (the LAN service) can read it anyway


def box_id(secret_b64):
    """Permanent box ID ("nk-" + 16 base32 characters) for a secret; same as the hub's nicks.box_id_from_secret."""
    try:
        raw = base64.b64decode(str(secret_b64 or ""), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("bad box identity") from None
    if len(raw) != 32:
        raise ValueError("bad box identity")
    return ID_PREFIX + base64.b32encode(hashlib.sha256(raw).digest()[:10]).decode().lower()


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read().replace(b"\x00", b"").strip().decode("ascii", "replace")
    except OSError:
        return ""


def machine_fingerprint(serial_paths=SERIAL_PATHS, cpuinfo=CPUINFO, machine_id=MACHINE_ID):
    """SHA-256 of the Pi's serial number (burned into the SoC, so it differs on every Pi even with a copied SD card),
    else of /etc/machine-id, else "" (then copies can't be detected)."""
    serial = next((s for s in map(_read, serial_paths) if s.strip("0")), "")
    if not serial:
        for line in _read(cpuinfo).splitlines():
            key, _, value = line.partition(":")
            if key.strip().lower() == "serial" and value.strip().strip("0"):
                serial = value.strip()
                break
    source = ("serial:" + serial.lower()) if serial else ("machine-id:" + _read(machine_id)) if _read(machine_id) else ""
    return hashlib.sha256(source.encode()).hexdigest() if source else ""


def _chown(path):
    if os.geteuid() != 0:
        return
    try:
        import pwd
        pw = pwd.getpwnam(OWNER)
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except (KeyError, OSError):
        pass


def _create(path, machine):
    """Write a new identity atomically: link() fails if another process created the file first, so two processes
    starting together end up with the same secret (the loser reads the winner's)."""
    data = {"secret": base64.b64encode(secrets.token_bytes(32)).decode(), "machine": machine, "created": time.time()}
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        _chown(tmp)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    log.info("netkey: created the box identity (box ID %s)", box_id(data["secret"]))
    return data


def load(state_dir, machine=None):
    """{"secret", "box_id"} for this box, created on first use. ValueError if it can't be read or written."""
    path = os.path.join(state_dir, FILE)
    machine = machine_fingerprint() if machine is None else machine
    from .store import read_json
    for _ in range(3):
        if not os.path.lexists(path):
            data = _create(path, machine)
            if data is None:
                continue                                  # another process won the race: read its file
        else:
            # root reads this too, and the folder belongs to the web service's user: no symlinks, FIFOs or huge files
            data = read_json(path, limit=64 * 1024)
            if data is None:
                raise ValueError(f"cannot read the box identity ({path})")
        if not isinstance(data, dict):
            raise ValueError(f"cannot read the box identity ({path})")
        if machine and data.get("machine") and data["machine"] != machine:
            log.warning("netkey: %s was made on another Pi (copied SD card?); creating a new box identity", path)
            try:
                os.replace(path, path + ".copied")
            except FileNotFoundError:
                pass                                      # the other process already set it aside
            continue
        return {"secret": data["secret"], "box_id": box_id(data["secret"])}
    raise ValueError(f"cannot create the box identity ({path})")


def replace(state_dir, secret, machine=None):
    """Put a restored identity in place (backup.py), bound to this Pi. The old one is kept as .replaced."""
    box_id(secret)                                       # ValueError if it is not a real identity
    path = os.path.join(state_dir, FILE)
    machine = machine_fingerprint() if machine is None else machine
    from .store import write_json_atomic
    if os.path.lexists(path) and not os.path.islink(path):
        try:
            os.replace(path, path + ".replaced")
        except OSError:
            pass
    write_json_atomic(path, {"secret": secret, "machine": machine, "created": time.time(), "restored": True}, mode=0o600)
    _chown(path)
    log.info("netkey: box identity restored from a backup (box ID %s)", box_id(secret))


def try_load(state_dir):
    """The identity, or None (logged) if it can't be had: the box then registers as before, without one."""
    try:
        return load(state_dir)
    except (ValueError, OSError) as e:
        log.warning("netkey: no box identity, public parties unavailable: %s", e)
        return None
