"""Root helper (netkey-users.service): render the netkey block of /etc/ppp/pap-secrets from the user list
the web GUI exports to /var/lib/netkey/pap-users.json. Root never opens the SQLite DB."""
import os
import pwd
import tempfile
import time

from .config import pap_secrets
from .store import USERS_EXPORT, USERS_SYNCED, read_json, write_json_atomic

BEGIN = "# BEGIN netkey"
END = "# END netkey"


def replace_block(text, block):
    out, skipping = [], False
    for line in text.splitlines():
        if line.startswith(BEGIN):
            skipping = True
            continue
        if skipping:
            if line.startswith(END):
                skipping = False
            continue
        out.append(line)
    out.append(f"{BEGIN} (managed by netkey; edit users in the web GUI)")
    if block:
        out.append(block)
    out.append(END)
    return "\n".join(out) + "\n"


def sync(cfg, state_dir, secrets_path="/etc/ppp/pap-secrets", owner="netkey"):
    """Returns (changed, users written). Falls back to config [auth] until the GUI has exported users."""
    export = read_json(os.path.join(state_dir, USERS_EXPORT))
    users = export.get("users") if isinstance(export, dict) else None
    version = export.get("version") if isinstance(export, dict) else None
    lines = int(cfg["lines"]["max"])
    error = None
    try:
        block = pap_secrets(cfg, lines, users)
    except ValueError as e:
        # never lock everyone out because of one bad entry: keep the valid ones
        error = str(e)
        good = []
        for u in users or cfg["auth"]["users"]:
            try:
                pap_secrets(cfg, lines, [u])
                good.append(u)
            except ValueError:
                pass
        block = pap_secrets(cfg, lines, good)
    try:
        with open(secrets_path) as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    new = replace_block(old, block)
    changed = new != old
    if changed:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(secrets_path), prefix=".pap-secrets.")
        with os.fdopen(fd, "w") as f:
            f.write(new)
        os.chmod(tmp, 0o600)
        os.replace(tmp, secrets_path)
    if version is not None:
        synced = os.path.join(state_dir, USERS_SYNCED)
        write_json_atomic(synced, {"version": version, "at": time.time(), "error": error})
        try:
            pw = pwd.getpwnam(owner)
            os.chown(synced, pw.pw_uid, pw.pw_gid)
        except (KeyError, PermissionError):
            pass
    names = [line.split("\t", 1)[0] for line in block.splitlines() if line]
    return changed, names
