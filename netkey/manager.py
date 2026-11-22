"""Hot-plug manager: one modem line per USB serial adapter, assigned persistently on first sight."""
import hashlib
import json
import logging
import os
import subprocess
import sys
import time

from . import status as st
from .config import apply_overrides, line_config

log = logging.getLogger("netkey")

SCAN_INTERVAL_S = 2.0
RESPAWN_DELAY_S = 3.0
UDEV_DATA = "/run/udev/data"


def udev_record(devnode, udev_data=UDEV_DATA):
    """(properties, symlinks) from udev's database, or ({}, []) if udev hasn't processed the device yet."""
    try:
        rdev = os.stat(devnode).st_rdev
        with open(os.path.join(udev_data, f"c{os.major(rdev)}:{os.minor(rdev)}")) as f:
            lines = f.read().splitlines()
    except OSError:
        return {}, []
    props = dict(line[2:].split("=", 1) for line in lines if line.startswith("E:") and "=" in line)
    links = [line[2:] for line in lines if line.startswith("S:")]
    return props, links


def stable_link(props, links):
    """Identity of an adapter: by-id when it has a serial number (can move ports), else its USB port path."""
    by_id = sorted(link for link in links if link.startswith("serial/by-id/"))
    by_path = sorted(link for link in links if link.startswith("serial/by-path/") and "-usbv" not in link)
    if props.get("ID_SERIAL_SHORT") and by_id:
        return by_id[0]
    return by_path[0] if by_path else None


def scan_adapters(sys_class="/sys/class/tty", dev="/dev", udev_data=UDEV_DATA):
    """{stable link: device path} for every USB serial adapter udev has finished setting up."""
    found = {}
    try:
        names = sorted(os.listdir(sys_class))
    except OSError:
        return found
    for name in names:
        if not name.startswith(("ttyUSB", "ttyACM")):
            continue
        props, links = udev_record(os.path.join(dev, name), udev_data)
        link = stable_link(props, links)
        if link:
            found[link] = os.path.join(dev, link)
    return found


class Registry:
    """Persistent adapter -> line number map, so an adapter keeps its line (client IP, modem mode) across replugs.

    lines.json: {"lines": {key: line}, "absent_since": {key: unix time}}. Unplugged adapters keep their line; only when
    every slot is taken does a new adapter get the slot of the one unplugged longest (at least RECYCLE_GRACE_S ago).
    The manager and the web GUI ("forget") both change the file, always read-modify-write under a file lock."""

    RECYCLE_GRACE_S = 60     # a USB glitch or a quick replug never loses its line

    def __init__(self, path, max_lines):
        self.path = path
        self.max_lines = max_lines
        self.lines, self.absent = {}, {}
        self.mtime = None
        self.refresh()

    def _read(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        return ({k: int(v) for k, v in data.get("lines", {}).items()},
                {k: float(v) for k, v in data.get("absent_since", {}).items()})

    def refresh(self):
        """Reload when another process (the web GUI) changed the file."""
        mtime = self._stamp()
        if mtime != self.mtime:
            self.lines, self.absent = self._read()
            self.mtime = mtime

    def _stamp(self):
        """Every save replaces the file (new inode), so two saves within one mtime tick still differ."""
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return st.st_ino, st.st_mtime_ns, st.st_size

    def _update(self, change):
        """change(lines, absent) -> result; runs under the lock on fresh data and saves if anything changed."""
        import fcntl
        fd = os.open(self.path + ".lock", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o640)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            lines, absent = self._read()
            before = (dict(lines), dict(absent))
            result = change(lines, absent)
            if (lines, absent) != before:
                from .store import write_atomic
                write_atomic(self.path, json.dumps({"lines": lines, "absent_since": {k: round(v) for k, v in
                                                                                     absent.items()}},
                                                   indent=2, sort_keys=True), mode=0o644)
            self.lines, self.absent = lines, absent
            self.mtime = self._stamp()
        return result

    def mark_present(self, present, now=None):
        """Record when registered adapters disappear (and forget that once they are back)."""
        now = time.time() if now is None else now
        present = set(present)
        if all((k in present) != (k in self.absent) for k in self.lines) and not set(self.absent) - set(self.lines):
            return

        def change(lines, absent):
            for k in list(absent):
                if k in present or k not in lines:
                    del absent[k]
            for k in lines:
                if k not in present and k not in absent:
                    absent[k] = now
        self._update(change)

    def line_for(self, key, now=None):
        """(line, recycled key or None); line is None when every slot belongs to a present or just-unplugged adapter."""
        if key in self.lines:
            return (self.lines[key] if self.lines[key] < self.max_lines else None), None
        now = time.time() if now is None else now

        def change(lines, absent):
            if key in lines:
                return (lines[key] if lines[key] < self.max_lines else None), None
            used = {n for n in lines.values() if n < self.max_lines}
            free = next((n for n in range(self.max_lines) if n not in used), None)
            if free is not None:
                lines[key] = free
                return free, None
            old = min((k for k in absent if k in lines and lines[k] < self.max_lines
                       and now - absent[k] >= self.RECYCLE_GRACE_S), key=lambda k: absent[k], default=None)
            if old is None:
                return None, None
            n = lines.pop(old)
            del absent[old]
            lines[key] = n
            return n, old
        return self._update(change)

    def forget(self, line, is_present):
        """Drop the unplugged adapter on `line` (web GUI). Returns its key, or None if there is none to forget."""
        def change(lines, absent):
            key = next((k for k, n in lines.items() if n == int(line)), None)
            if key is None or is_present(key):
                return None
            del lines[key]
            absent.pop(key, None)
            return key
        return self._update(change)


class Manager:
    def __init__(self, cfg, config_path, state_dir, run_dir=None):
        self.cfg = cfg
        self.config_path = config_path
        self.state_dir = state_dir
        self.run_dir = run_dir if run_dir and os.path.isdir(run_dir) else None
        self.registry = Registry(os.path.join(state_dir, "lines.json"), int(cfg["lines"]["max"]))
        self.workers = {}   # line -> Popen
        self.fingerprints = {}   # line -> effective line config the worker was started with
        self.exited_at = {}
        self.warned = set()
        self.stopping = False
        self.stamp = None
        self.dirty = False
        self.store = None

    def effective(self):
        """config.toml + GUI settings from the state DB (DB errors fall back to config.toml)."""
        try:
            if self.store is None:
                from .store import Store
                self.store = Store(self.state_dir)
            return apply_overrides(self.cfg, self.store.get_settings(), self.store.get_line_modes(), log.warning)
        except Exception as e:  # noqa: BLE001
            log.warning("manager: state DB unavailable (%s)", e)
            return self.cfg

    def fingerprint(self, eff, line):
        c = line_config(eff, line)
        del c["lines"]["modes"], c["lines"]["default_mode"]   # only this line's resolved c["mode"] matters
        for section in ("lan", "network", "web", "auth"):                 # not used by modem lines
            c.pop(section, None)
        return hashlib.sha1(json.dumps(c, sort_keys=True, default=str).encode()).hexdigest()

    def settings_stamp(self):
        """mtime plus contents: two changes within one filesystem timestamp tick still differ."""
        path = os.path.join(self.state_dir, "settings.stamp")
        try:
            with open(path) as f:
                return os.fstat(f.fileno()).st_mtime_ns, f.read()
        except OSError:
            return None

    def check_settings(self):
        """Restart lines whose effective config changed; lines in a call wait until they hang up."""
        stamp = self.settings_stamp()
        if stamp != self.stamp:
            self.stamp = stamp
            self.dirty = True
        if not self.dirty:
            return
        eff = self.effective()
        waiting = False
        for line, worker in list(self.workers.items()):
            if self.fingerprints.get(line) == self.fingerprint(eff, line):
                continue
            state = (st.read(st.line_file(self.run_dir, line)) or {}).get("state") if self.run_dir else None
            if state in st.BUSY_STATES:
                waiting = True
                continue
            log.info("manager: line %d settings changed; restarting it", line)
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()
            del self.workers[line]
            self.exited_at.pop(line, None)
        self.dirty = waiting

    def heartbeat(self):
        if self.run_dir:
            st.write(os.path.join(self.run_dir, st.MANAGER_FILE),
                     {"pid": os.getpid(), "updated": time.time(), "lines": sorted(self.workers)})

    def stop(self, *_):
        self.stopping = True

    def run(self):
        log.info("manager: up to %d lines, registry %s", self.registry.max_lines, self.registry.path)
        self.stamp = self.settings_stamp()
        while not self.stopping:
            self.heartbeat()
            self.check_settings()
            found = scan_adapters()
            self.registry.refresh()
            self.registry.mark_present(found)
            for key, device in found.items():
                line, recycled = self.registry.line_for(key)
                if recycled:
                    self.recycled(line, recycled, key)
                if line is None:
                    if key not in self.warned:
                        log.warning("manager: no free line for %s (lines.max=%d)", key, self.registry.max_lines)
                        self.warned.add(key)
                    continue
                self.ensure_worker(line, key, device)
            self.reap()
            time.sleep(SCAN_INTERVAL_S)
        self.stop_workers()

    def recycled(self, line, old, new):
        """All slots were taken: `new` got the line of `old`, the adapter unplugged longest."""
        log.warning("manager: all %d lines in use; line %d moves from unplugged %s to %s",
                    self.registry.max_lines, line, old, new)
        worker = self.workers.pop(line, None)
        if worker is not None:
            worker.terminate()
        try:
            self.effective()
            if self.store is not None:
                self.store.set_line_mode(line, None)     # the old adapter's mode is not the new one's
        except Exception as e:  # noqa: BLE001
            log.warning("manager: could not reset the mode of line %d (%s)", line, e)
        self.dirty = True

    def ensure_worker(self, line, key, device):
        worker = self.workers.get(line)
        if worker is not None:
            return
        if time.monotonic() - self.exited_at.get(line, 0) < RESPAWN_DELAY_S:
            return
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modem.py"),
               "--config", self.config_path, "--line", str(line), "--device", device,
               "--state-dir", self.state_dir]
        if self.run_dir:
            cmd += ["--run-dir", self.run_dir]
        self.fingerprints[line] = self.fingerprint(self.effective(), line)
        log.info("manager: line %d <- %s", line, key)
        self.workers[line] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)

    def reap(self):
        for line, worker in list(self.workers.items()):
            rc = worker.poll()
            if rc is not None:
                log.warning("manager: line %d worker exited with %d; restarting", line, rc)
                del self.workers[line]
                self.exited_at[line] = time.monotonic()

    def stop_workers(self):
        for worker in self.workers.values():
            worker.terminate()
        deadline = time.monotonic() + 10
        for worker in self.workers.values():
            try:
                worker.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                worker.kill()
        log.info("manager: stopped %d line(s)", len(self.workers))
