"""Click-to-update (root, netkey-update.service): installs a newer NetKey release when an admin asks in the GUI.

Never automatic. The web GUI (user netkey) can't install anything: it only writes a request, the version and nothing
else, to /var/lib/netkey/update-request.json after checking the admin password. netkey-update.path starts this
module as root, which trusts nothing in that request beyond a well-formed version number:

  1. take the request (refused if it is a symlink, too big, malformed or older than 10 minutes) and delete it
  2. one update at a time (a lock), and only to a version that is newer than the installed one AND the one the cloud
     hub offers right now (so a release the operator holds back can't be installed)
  3. not while the box is in a LAN party or a modem line is in a call, unless the admin ticked "update anyway"
  4. download the installer of that version, and of the installed version (to roll back with), from the box
     repository's GitHub releases over HTTPS; the address is built here from fixed parts, never taken from outside
  5. sanity checks: the installer names the requested version, and checks its own payload checksum when it runs
  6. run it; then check that the services run, the web GUI answers and the LAN service reports in
  7. if the install or the check fails: run the previous version's installer to go back, and say so

Progress goes to status.json and every attempt to history.json in /var/lib/netkey-update (root-owned, readable by
the GUI), and everything to the journal (journalctl -u netkey-update).
"""
import fcntl
import glob
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

log = logging.getLogger("netkey")

WORK_DIR = "/var/lib/netkey-update"         # root-owned: status, history, installers, lock, the hub login
REQUEST_FILE = "update-request.json"        # in the state dir (/var/lib/netkey): written by the web GUI
STATUS_FILE = "status.json"
HISTORY_FILE = "history.json"
REPO = "ohminty/netkey"
OPT = "/opt/netkey"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
REQUEST_MAX_AGE_S = 600
REQUEST_MAX_BYTES = 4096
INSTALLER_MAX_BYTES = 50 * 1048576
MIN_FREE_BYTES = 400 * 1048576
DOWNLOAD_TIMEOUT_S = 60
INSTALL_TIMEOUT_S = 45 * 60
HEALTH_TIMEOUT_S = 240
HISTORY_MAX = 50
LOG_LINES = 40
RUNNING = ("queued", "checking", "downloading", "installing", "verifying", "rolling_back")


class UpdateError(Exception):
    """Stops the update before anything was installed (nothing to roll back)."""


def parse_version(v):
    v = str(v or "").strip()
    if not VERSION_RE.match(v):
        return None
    core, _, pre = v.partition("-")
    nums = tuple(int(x) for x in core.split("."))
    return (nums, (1,)) if not pre else (nums, (0,) + tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                                                           for p in pre.split(".")))


def newer(a, b):
    pa, pb = parse_version(a), parse_version(b)
    return bool(pa and pb and pa > pb)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    """Atomic, in the root-owned work dir only (nobody else can plant files there)."""
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read_untrusted(path, limit):
    """A file in a directory the netkey user can write: no symlinks, regular files only, size-capped."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not (st.st_mode & 0o170000 == 0o100000) or st.st_size > limit:
            raise ValueError("not a small regular file")
        return os.read(fd, limit + 1)[:limit]
    finally:
        os.close(fd)


class HttpsOnly(urllib.request.HTTPRedirectHandler):
    """GitHub sends release downloads on to its file servers: follow, but never to plain HTTP."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise urllib.error.URLError(f"refusing a redirect to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_https(url, dest, limit=INSTALLER_MAX_BYTES, timeout=DOWNLOAD_TIMEOUT_S):
    """Download url to dest (HTTPS with certificate checks). Returns False when there is no such file (404)."""
    ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), HttpsOnly)
    req = urllib.request.Request(url, headers={"User-Agent": "netkey-updater"})
    try:
        r = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
    with r:
        n = 0
        with open(dest + ".part", "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                n += len(chunk)
                if n > limit:
                    raise UpdateError("the download is far bigger than a NetKey installer")
                f.write(chunk)
    os.chmod(dest + ".part", 0o700)
    os.replace(dest + ".part", dest)
    return True


class Updater:
    def __init__(self, cfg, state_dir, work_dir=WORK_DIR, opt=OPT, lan_run="/run/netkey-lan", modem_run="/run/netkey",
                 fetch=fetch_https, offered=None, now=time.time, min_free=MIN_FREE_BYTES):
        self.cfg, self.state_dir, self.work_dir, self.opt = cfg, state_dir, work_dir, opt
        self.lan_run, self.modem_run = lan_run, modem_run
        self.fetch, self.now, self.min_free = fetch, now, min_free
        self.offered = offered or self.hub_offered
        self.job = None
        self.lines = []

    # ---- status and history (read by the GUI)
    def status(self, state, **kw):
        self.job = dict(self.job or {}, state=state, updated=self.now(), **kw)
        self.job["log"] = self.lines[-LOG_LINES:]
        write_json(os.path.join(self.work_dir, STATUS_FILE), self.job)

    def say(self, msg):
        log.info("update: %s", msg)
        self.lines.append(msg)

    def record(self, result, message):
        path = os.path.join(self.work_dir, HISTORY_FILE)
        hist = read_json(path) or []
        hist.insert(0, {"from": self.job.get("from"), "to": self.job.get("target"), "by": self.job.get("by"),
                        "started": self.job.get("started"), "finished": self.now(), "result": result,
                        "message": message})
        write_json(path, hist[:HISTORY_MAX])

    def installed(self):
        try:
            with open(os.path.join(self.opt, "VERSION")) as f:
                return f.read().strip()
        except OSError:
            return ""

    # ---- the request
    def take_request(self):
        """The GUI's request, or None. Deleted whatever it holds, so nothing is ever replayed."""
        path = os.path.join(self.state_dir, REQUEST_FILE)
        try:
            raw = read_untrusted(path, REQUEST_MAX_BYTES)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raw = None
            log.warning("update: ignoring a bad request file: %s", e)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            d = json.loads(raw or b"")
        except ValueError:
            d = None
        if not isinstance(d, dict):
            return None
        version, when = str(d.get("version") or ""), d.get("requested")
        if not parse_version(version) or not isinstance(when, (int, float)):
            log.warning("update: ignoring a malformed request")
            return None
        if not 0 <= self.now() - when <= REQUEST_MAX_AGE_S:
            log.warning("update: ignoring a request from %s s ago", int(self.now() - when))
            return None
        by = re.sub(r"[^A-Za-z0-9_.@-]", "", str(d.get("by") or ""))[:32]
        return {"version": version, "force": d.get("force") is True, "by": by or "admin"}

    # ---- checks before anything is changed
    def hub_offered(self, current):
        """The release the hub offers this box now (it applies the operator's hold)."""
        from . import hubclient as hc
        from . import identity as ident
        from .config import box_name
        lan = self.cfg["lan"]
        identity = None
        try:
            d = json.loads(read_untrusted(os.path.join(self.state_dir, ident.FILE), 4096))
            identity = {"secret": d["secret"], "box_id": ident.box_id(d["secret"])}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        client = hc.HubClient(lan["hub_url"], lan["hub_fingerprint"], self.work_dir, box_name(self.cfg),
                              identity=identity, creds_file="hub-update.json")
        try:
            return (client.call("GET", f"/v1/release?v={quote(current)}", timeout=20) or {}).get("latest")
        except hc.HubError as e:
            raise UpdateError(f"the cloud hub could not be asked which release is current ({e})") from None

    def busy(self):
        """Why an update would interrupt someone, or None."""
        lan = read_json(os.path.join(self.lan_run, "status.json")) or {}
        role = (lan.get("party") or {}).get("role") or lan.get("role")
        if role in ("host", "member") and self.now() - lan.get("updated", 0) < 60:
            return "this box is in a LAN party"
        for path in glob.glob(os.path.join(self.modem_run, "line*.json")):
            s = read_json(path) or {}
            if self.now() - s.get("updated", self.now()) > 30:
                continue                              # the line's worker is gone
            if s.get("state") in ("dialing", "ringing", "answering", "connected"):
                return "a modem line is in a call"
        return None

    # ---- download and run installers
    def download(self, version):
        """The installer of a release, kept in the work dir. The address is built here from fixed parts."""
        dest = os.path.join(self.work_dir, "installers", f"netkey-{version}-install.sh")
        os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
        for tag in (version, "v" + version):        # releases are tagged with or without the "v"
            url = f"https://github.com/{REPO}/releases/download/{quote(tag)}/netkey-{quote(version)}-install.sh"
            try:
                if self.fetch(url, dest):
                    break
            except (OSError, urllib.error.URLError, ssl.SSLError) as e:
                raise UpdateError(f"downloading NetKey {version} failed: {e}") from None
        else:
            return None
        with open(dest, "rb") as f:
            head = f.read(400).decode("utf-8", "replace").splitlines()
        if len(head) < 2 or not head[0].startswith("#!") or not head[1].startswith(f"# NetKey {version} installer"):
            os.unlink(dest)
            raise UpdateError(f"the file downloaded for {version} is not that version's NetKey installer")
        return dest

    def run_installer(self, path):
        """Runs an installer; its output goes to the journal and the GUI's log view. Returns the exit code."""
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "DEBIAN_FRONTEND": "noninteractive"}
        p = subprocess.Popen(["bash", path], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, cwd=self.work_dir, env=env, text=True, errors="replace")
        timer = threading.Timer(INSTALL_TIMEOUT_S, p.kill)
        timer.start()
        last = 0.0
        try:
            for line in p.stdout:
                line = re.sub(r"\x1b\[[0-9;]*m", "", line.rstrip())
                if line:
                    self.say(line[:200])
                    if time.monotonic() - last > 1:
                        last = time.monotonic()
                        self.status(self.job["state"])
            return p.wait()
        finally:
            timer.cancel()
            p.stdout.close()

    def healthy(self, version, deadline_s=HEALTH_TIMEOUT_S):
        """After installing: the version is in place, the services run, the GUI answers and the LAN service reports."""
        end = time.monotonic() + deadline_s
        why = "timed out"
        while time.monotonic() < end:
            why = self.health_problem(version)
            if not why:
                return None
            time.sleep(5)
        return why

    def health_problem(self, version):
        if self.installed() != version:
            return f"{self.opt}/VERSION says {self.installed() or 'nothing'}, not {version}"
        r = subprocess.run(["systemctl", "is-active", "netkey-web", "netkey-lan"], capture_output=True, text=True)
        if r.returncode != 0:
            return "a NetKey service is not running: " + " ".join(r.stdout.split())
        port = int(self.cfg["web"]["port"])
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE     # the box's own self-signed certificate
        try:
            with urllib.request.urlopen(f"https://127.0.0.1:{port}/api/session", context=ctx, timeout=10) as r:
                json.loads(r.read(65536))
        except (OSError, ValueError, urllib.error.URLError) as e:
            return f"the web GUI does not answer ({e})"
        lan = read_json(os.path.join(self.lan_run, "status.json")) or {}
        if self.now() - lan.get("updated", 0) > 30:
            return "the LAN service has not reported in"
        return None

    # ---- the whole job
    def run(self):
        os.makedirs(self.work_dir, mode=0o755, exist_ok=True)
        lock = open(os.path.join(self.work_dir, "lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("update: another update is running")
            return 0
        try:
            req = self.take_request()
            if not req:
                return 0
            cur = self.installed()
            self.lines = []
            self.job = {"target": req["version"], "from": cur, "by": req["by"], "started": self.now(),
                        "force": req["force"]}
            try:
                return self.update(req, cur)
            except UpdateError as e:
                self.say(f"Not updated: {e}")
                self.status("failed", message=str(e), rolled_back=False)
                self.record("not started", str(e))
                return 1
            except Exception as e:                    # never leave the GUI showing "installing" forever
                log.exception("update: failed")
                self.status("failed", message=f"unexpected error: {e}")
                self.record("failed", f"unexpected error: {e}")
                return 1
        finally:
            lock.close()

    def update(self, req, cur):
        target = req["version"]
        self.status("checking", message=f"Checking before updating {cur or '?'} to {target}")
        if not newer(target, cur):
            raise UpdateError(f"this box already runs {cur}")
        offered = self.offered(cur)
        if offered != target:
            raise UpdateError(f"the cloud hub offers {offered or 'no release'}, not {target}")
        why = self.busy()
        if why and not req["force"]:
            raise UpdateError(f"{why}; finish it first, or choose to update anyway")
        if why:
            self.say(f"Updating anyway: {why}")
        st = os.statvfs(self.work_dir)
        if st.f_bavail * st.f_frsize < self.min_free:
            raise UpdateError("not enough free space on the memory card")
        self.status("downloading", message=f"Downloading NetKey {target}")
        new = self.download(target)
        if not new:
            raise UpdateError(f"NetKey {target} has no installer on GitHub")
        self.say(f"Downloaded NetKey {target}")
        old = self.download(cur)                      # to come back to if the new version doesn't work
        if not old:
            raise UpdateError(f"the installed version {cur} is not on GitHub, so there would be nothing to go back to")
        self.say(f"Kept NetKey {cur} to go back to")

        self.status("installing", message=f"Installing NetKey {target}. The box restarts its services; "
                                          "this page reconnects by itself.")
        code = self.run_installer(new)
        problem = f"the installer stopped with exit code {code}" if code else None
        if not problem:
            self.status("verifying", message="Checking that everything runs")
            problem = self.healthy(target)
        if not problem:
            self.say(f"NetKey {target} is installed and running")
            self.status("done", message=f"Updated to NetKey {target}")
            self.record("updated", f"{cur} to {target}")
            self.prune(keep=(target,))
            return 0
        return self.roll_back(old, cur, problem)

    def roll_back(self, old, cur, problem):
        self.say(f"Update failed: {problem}. Going back to {cur}.")
        self.status("rolling_back", message=f"The update failed ({problem}). Going back to NetKey {cur}.")
        code = self.run_installer(old)
        back = None if code else self.healthy(cur)
        if code == 0 and not back:
            self.status("rolled_back", message=f"The update failed ({problem}). NetKey {cur} was put back and runs.",
                        rolled_back=True)
            self.record("rolled back", problem)
            return 1
        msg = (f"The update failed ({problem}) and going back to {cur} failed too "
               f"({back or f'exit code {code}'}). Reinstall from the Pi with the update command.")
        self.status("failed", message=msg, rolled_back=False)
        self.record("failed", msg)
        return 1

    def prune(self, keep):
        for path in glob.glob(os.path.join(self.work_dir, "installers", "netkey-*-install.sh")):
            if not any(path.endswith(f"netkey-{v}-install.sh") for v in keep):
                try:
                    os.unlink(path)
                except OSError:
                    pass


def read_progress(work_dir=WORK_DIR, state_dir=None):
    """For the web GUI: the current or last update job, and the history."""
    job = read_json(os.path.join(work_dir, STATUS_FILE)) or {"state": "idle"}
    if state_dir and os.path.exists(os.path.join(state_dir, REQUEST_FILE)) and job.get("state") not in RUNNING:
        job = {"state": "queued", "message": "Waiting for the updater to start"}
    return {"job": job, "history": read_json(os.path.join(work_dir, HISTORY_FILE)) or []}


def run(cfg, state_dir):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    return Updater(cfg, state_dir).run()
