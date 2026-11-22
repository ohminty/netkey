"""Runtime status files in /run/netkey (tmpfs): written by the manager, the line workers and the pppd
ip-up hook; read by the web GUI. Every file is replaced atomically."""
import json
import os
import re
import time

RUN_DIR = "/run/netkey"
MANAGER_FILE = "manager.json"
STALE_S = 30            # a line status older than this means the worker is gone
MANAGER_STALE_S = 10

# Line states shown in the GUI
UNPLUGGED, STOPPED, STARTING = "unplugged", "stopped", "starting"
IDLE, DIALING, CONNECTED = "idle", "dialing", "connected"                 # both modes
INITIALIZING, MODEM_ERROR, RINGING, ANSWERING = "initializing", "modem_error", "ringing", "answering"  # physical
BUSY_STATES = (DIALING, RINGING, ANSWERING, CONNECTED)


def line_file(run_dir, line):
    return os.path.join(run_dir, f"line{line}.json")


def ppp_file(run_dir, line):
    """Written by /etc/ppp/ip-up.d/netkey (root) when PPP is up: user, iface, ip."""
    return os.path.join(run_dir, f"ppp{line}.json")


def write(path, data):
    from .store import write_atomic
    try:
        write_atomic(path, json.dumps(data), mode=0o644)
    except OSError:
        pass


def read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def iface_counters(iface, sys_net="/sys/class/net"):
    """(rx_bytes, tx_bytes) of a network interface, or None when it doesn't exist."""
    try:
        base = os.path.join(sys_net, iface, "statistics")
        with open(os.path.join(base, "rx_bytes")) as f:
            rx = int(f.read())
        with open(os.path.join(base, "tx_bytes")) as f:
            tx = int(f.read())
        return rx, tx
    except (OSError, ValueError):
        return None


def adapter_label(key):
    """Short human name for a registry key like serial/by-id/usb-FTDI_FT232R_A1B2-if00-port0."""
    name = key.rsplit("/", 1)[-1]
    if "/by-id/" in f"/{key}":
        name = re.sub(r"^usb-", "", name)
        name = re.sub(r"-if\d+(-port\d+)?$", "", name)
        return name.replace("_", " ")
    m = re.search(r"usb-\d+:([\d.]+):", name)
    return f"USB port {m.group(1)}" if m else name


def manager_alive(run_dir, now=None):
    m = read(os.path.join(run_dir, MANAGER_FILE))
    return bool(m) and (now or time.time()) - m.get("updated", 0) < MANAGER_STALE_S
