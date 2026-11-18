"""File sharing to the retro LAN: one folder (share.dir), reachable three ways from the retro PCs only.

  Windows file sharing   Samba (smbd + nmbd) inside the party namespace, guest access, SMB1 so Windows for
                         Workgroups 3.11, 95, 98, ME and XP can use it; the box shows up in Network Neighborhood.
  FTP                    logins are the modem users (from the GUI's export, hashed again for this process).
  Web                    a plain HTML 3.2 directory listing old browsers can download from (read only).

The LAN service (root) starts all of it inside its namespace, so the services only ever see the retro LAN; the
party guard keeps PCs of other boxes in a party away from them. FTP and the web listing run as the netkey-share
user (this module's main()); smbd runs as root and maps every PC to that same user. The web GUI (user netkey, in
the netkey-share group) manages the folder: uploads, folders, renames, deletes.
"""
import argparse
import html
import json
import logging
import mimetypes
import os
import re
import shutil
import signal
import socket
import stat
import sys
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

log = logging.getLogger("netkey")

SHARE_USER = "netkey-share"
SHARE_NAME = "FILES"                     # \\NETKEY\FILES
FTP_USERS_FILE = "ftp-users.json"        # run dir: modem users with hashed passwords, readable by SHARE_USER
FTP_HASH_ITERATIONS = 20000
PASV_PORTS = (40000, 40049)
SMB_DIR = "samba"                        # run dir: Samba's config, locks and state
SAMBA_LIBEXEC = "/usr/libexec/samba"
# Samba's RPC services (share lists for XP and smbclient, name lookups). Samba normally starts them on demand,
# eight helper programs at once; a Pi 1 needs longer than smbd waits, so nothing ever answers. NetKey runs
# samba-dcerpcd itself, all the time, with only these.
RPCDS = ("rpcd_classic", "rpcd_epmapper", "rpcd_lsad", "rpcd_winreg")   # classic needs winreg
TCP_PORTS = (21, 80, 139, 445)
UDP_PORTS = (137, 138)
BAD_CHARS = set('/\\:*?"<>|')            # also not allowed by Windows; Samba would mangle them


# ---- names and paths (shared with the web GUI)
def netbios_name(hostname=None):
    """\\\\NAME: the hostname in capitals, letters, digits and - only, at most 15 characters."""
    n = re.sub(r"[^A-Z0-9-]", "", str(hostname or socket.gethostname()).split(".")[0].upper())[:15].strip("-")
    return n or "NETKEY"


def check_name(name):
    """One file or folder name as a PC can use it; raises ValueError."""
    name = str(name or "").strip()
    if not name or name in (".", "..") or name.startswith("."):
        raise ValueError("name must not be empty or start with a dot")
    if len(name.encode()) > 200:
        raise ValueError("name is too long")
    if any(ch in BAD_CHARS or ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError('names can\'t contain / \\ : * ? " < > | or control characters')
    if name.endswith((" ", ".")):
        raise ValueError("names can't end with a space or a dot")
    return name


def rel_parts(rel):
    """'a/b/c' -> ['a', 'b', 'c'], every part checked. '' or '/' is the root."""
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p and p != "."]
    return [check_name(p) for p in parts]


def resolve(root, rel, must_exist=True):
    """Absolute path inside root for a relative path; never outside it (no .., no symlinks out)."""
    root = os.path.realpath(root)
    path = os.path.join(root, *rel_parts(rel))
    real = os.path.realpath(path)
    if real != root and not real.startswith(root + os.sep):
        raise ValueError("path is outside the shared folder")
    if must_exist and not os.path.lexists(path):
        raise FileNotFoundError(rel)
    return path


def listing(root, rel=""):
    path = resolve(root, rel)
    if not os.path.isdir(path):
        raise NotADirectoryError(rel)
    out = []
    with os.scandir(path) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            out.append({"name": e.name, "dir": is_dir, "size": 0 if is_dir else st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return out


def tree_size(path):
    total, files = 0, 0
    for base, dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.lstat(os.path.join(base, n)).st_size
                files += 1
            except OSError:
                pass
    return total, files


# ---- Samba
def smb_conf(cfg, run_dir, iface, gateway):
    s = cfg["share"]
    d = os.path.join(run_dir, SMB_DIR)
    return f"""# written by netkey-lan; changes are overwritten
[global]
    workgroup = {s["workgroup"]}
    netbios name = {netbios_name()}
    server string = NetKey
    server role = standalone server
    interfaces = {iface}
    bind interfaces only = yes
    smb ports = 139 445
    disable netbios = no
    # Windows for Workgroups 3.11, 95, 98, ME and XP speak SMB1 only
    server min protocol = NT1
    ntlm auth = ntlmv1-permitted
    lanman auth = no
    map to guest = Bad User
    guest account = {SHARE_USER}
    restrict anonymous = 0
    # the box is the browse master of the retro LAN: it lists the PCs in Network Neighborhood
    local master = yes
    preferred master = yes
    domain master = yes
    os level = 65
    wins support = yes
    dns proxy = no
    dos charset = CP850
    unix extensions = no
    load printers = no
    printing = bsd
    printcap name = /dev/null
    disable spoolss = yes
    usershare max shares = 0
    lock directory = {d}/lock
    state directory = {d}/state
    cache directory = {d}/cache
    private dir = {d}/private
    pid directory = {d}/pid
    ncalrpc dir = {d}/ncalrpc
    nmbd:socket dir = {d}/nmbd
    rpc start on demand helpers = no
    passdb backend = tdbsam:{d}/private/passdb.tdb
    log level = 1

[{SHARE_NAME}]
    comment = NetKey files
    path = {s["dir"]}
    guest ok = yes
    guest only = yes
    browseable = yes
    read only = {"no" if s["writable"] else "yes"}
    force user = {SHARE_USER}
    force group = {SHARE_USER}
    create mask = 0664
    force create mode = 0664
    directory mask = 2775
    force directory mode = 2775
    hide dot files = yes
    veto files = /.*/
    delete veto files = yes
"""


# ---- FTP users: the modem users, hashed for the share process (it never sees the plain passwords)
def ftp_users(users, old=None):
    """[{name, password, enabled}] -> {name: hash}. Keeps an existing hash when the password still matches."""
    from .store import hash_password, verify_password
    old = old or {}
    out = {}
    for u in users or []:
        if not isinstance(u, dict) or not u.get("enabled", True):
            continue
        name, pw = str(u.get("name") or ""), str(u.get("password") or "")
        if not name or not pw:
            continue
        h = old.get(name)
        out[name] = h if h and verify_password(pw, h) else hash_password(pw, iterations=FTP_HASH_ITERATIONS)
    return out


# ---- the web listing (HTML 3.2, ISO-8859-1, HTTP/1.0: Mosaic, Netscape 1-4 and IE 2-6 all read it)
def fmt_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def latin1(text):
    return html.escape(text).encode("latin-1", "xmlcharrefreplace")


def make_web_handler(root, name, address):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NetKey"
        sys_version = ""
        timeout = 60

        def log_message(self, fmt, *args):
            log.debug("files web %s " + fmt, self.client_address[0], *args)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            rel = unquote(urlsplit(self.path).path)
            try:
                path = resolve(root, rel)
            except (ValueError, FileNotFoundError):
                return self.plain(404, "Not found")
            if os.path.isdir(path):
                if not rel.endswith("/"):
                    return self.redirect(quote(rel.encode()) + "/")
                return self.page(rel, path)
            return self.file(path)

        def plain(self, code, text):
            body = latin1(text)
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=iso-8859-1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def redirect(self, to):
            self.send_response(301)
            self.send_header("Location", to)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def page(self, rel, path):
            parts = rel_parts(rel)
            title = "/" + "/".join(parts) + ("/" if parts else "")
            rows = []
            if parts:
                rows.append('<TR><TD><A HREF="../">[Up one level]</A></TD><TD></TD><TD></TD></TR>')
            for e in listing(root, rel):
                href = quote(e["name"].encode()) + ("/" if e["dir"] else "")
                label = html.escape(e["name"]) + ("/" if e["dir"] else "")
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime"]))
                rows.append(f'<TR><TD><A HREF="{href}">{label}</A></TD>'
                            f'<TD ALIGN="right">{"" if e["dir"] else fmt_size(e["size"])}</TD><TD>{when}</TD></TR>')
            if not rows:
                rows.append("<TR><TD COLSPAN=3><I>This folder is empty.</I></TD></TR>")
            doc = ('<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">\n'
                   f"<HTML><HEAD><TITLE>NetKey files {html.escape(title)}</TITLE></HEAD>\n"
                   '<BODY BGCOLOR="#FFFFFF" TEXT="#000000" LINK="#0000CC" VLINK="#551A8B">\n'
                   f"<H2>NetKey files: {html.escape(title)}</H2>\n"
                   '<TABLE BORDER=0 CELLPADDING=3 CELLSPACING=0>\n'
                   "<TR><TH ALIGN=left>Name</TH><TH ALIGN=right>Size</TH><TH ALIGN=left>Modified</TH></TR>\n"
                   + "\n".join(rows) + "\n</TABLE>\n<HR>\n"
                   f"<ADDRESS>Also in Network Neighborhood as \\\\{html.escape(name)}\\{SHARE_NAME} and on "
                   f"ftp://{html.escape(address)}/</ADDRESS>\n</BODY></HTML>\n")
            body = doc.encode("latin-1", "xmlcharrefreplace")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=iso-8859-1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def file(self, path):
            try:
                f = open(path, "rb")
            except OSError:
                return self.plain(404, "Not found")
            with f:
                st = os.fstat(f.fileno())
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(st.st_size))
                self.send_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
                self.end_headers()
                if self.command != "HEAD":
                    try:
                        shutil.copyfileobj(f, self.wfile, 64 * 1024)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
    return Handler


# ---- FTP (pyftpdlib)
def ftp_server(root, address, users_path, name):
    from pyftpdlib.authorizers import AuthenticationFailed, DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import ThreadedFTPServer
    from .store import verify_password

    class Auth(DummyAuthorizer):
        """Modem users; the file is re-read when it changes, so new or removed users apply at the next login."""
        def __init__(self):
            super().__init__()
            self.users, self.mtime = {}, None

        def load(self):
            try:
                m = os.stat(users_path).st_mtime
                if m != self.mtime:
                    with open(users_path) as f:
                        self.users = (json.load(f) or {}).get("users", {})
                    self.mtime = m
            except (OSError, ValueError):
                self.users = {}
            return self.users

        def validate_authentication(self, username, password, handler):
            h = self.load().get(username)
            if not h or not verify_password(password, h):
                time.sleep(1)
                raise AuthenticationFailed("Login incorrect.")
            if self.has_user(username):
                self.remove_user(username)
            self.add_user(username, "", root, perm="elradfmwMT",
                          msg_login=f"Welcome to {name}. These are the NetKey files.", msg_quit="Goodbye.")

    class Handler(FTPHandler):
        authorizer = Auth()
        banner = f"NetKey FTP on {name} ready."
        passive_ports = range(PASV_PORTS[0], PASV_PORTS[1] + 1)
        permit_foreign_addresses = False
        max_login_attempts = 3
        timeout = 600
        use_sendfile = False

    server = ThreadedFTPServer((address, 21), Handler)
    server.max_cons = 32
    server.max_cons_per_ip = 8
    return server


def main(argv=None):
    p = argparse.ArgumentParser(description="NetKey FTP and web file servers (run by netkey-lan in its namespace)")
    p.add_argument("--root", required=True)
    p.add_argument("--address", required=True)
    p.add_argument("--name", default=netbios_name())
    p.add_argument("--ftp", action="store_true")
    p.add_argument("--web", action="store_true")
    p.add_argument("--users", help="FTP users file (name -> hash)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("pyftpdlib").setLevel(logging.WARNING)
    os.umask(0o002)
    servers = []
    if args.ftp:
        ftp = ftp_server(args.root, args.address, args.users, args.name)
        servers.append(ftp)
        threading.Thread(target=ftp.serve_forever, kwargs={"handle_exit": False}, daemon=True).start()
        log.info("files: FTP on %s:21", args.address)
    if args.web:
        web = ThreadingHTTPServer((args.address, 80), make_web_handler(args.root, args.name, args.address))
        web.daemon_threads = True
        servers.append(web)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        log.info("files: web on http://%s/", args.address)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *a: stop.set())
    stop.wait()
    for s in servers:
        try:
            s.close_all() if hasattr(s, "close_all") else s.shutdown()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
