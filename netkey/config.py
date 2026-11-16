"""Configuration: defaults merged with /etc/netkey/config.toml, plus per-line derivation."""
import copy
import ipaddress
import os
import re
import socket
import sys
import tomllib

MAX_LINES = 32

DEFAULTS = {
    "box": {
        "name": "",                # shown to other boxes in LAN parties; "" = the hostname
    },
    "serial": {
        "device": "/dev/netkey{line}",
        "baud": 115200,
        "autobaud": True,
        "rtscts": False,
        "control_lines": "auto",   # auto | off
        "pc_dtr_input": "dsr",     # dsr | dcd | none  (Pi input wired to the PC's DTR)
        "dcd_mode": "always_on",   # always_on | follow (Pi DTR -> PC DSR+DCD; XP needs DSR)
    },
    "modem": {
        "connect_string": "CONNECT 56000",
        "dial_delay_s": 1.0,
        "model": "USRobotics 56K FAX EXT",
    },
    "ppp": {
        "command": ["/usr/sbin/pppd", "call", "netkey{line}"],
        "stop_timeout_s": 5.0,
        "server_name": "netkey",
        "server_ip": "10.66.0.1",
        "first_client_ip": "10.66.0.2",   # line N gets first_client_ip + N
    },
    "auth": {
        "users": [{"name": "test", "password": "test"}],
    },
    "lines": {
        "max": 8,                  # adapters beyond this are ignored; peers files are pre-made for each
        "default_mode": "virtual", # virtual: the Pi is the modem | physical: a real modem is on the port
        "modes": {},               # per-line override, e.g. { "1" = "physical" }
    },
    "physical": {
        "baud": 115200,            # fixed DTE speed between the Pi and the real modem
        "rtscts": True,            # hardware flow control with the modem (straight cable carries RTS/CTS)
        "init_string": "AT&FE0V1Q0X4&C1&D2S0=0",
        "answer_rings": 1,         # send ATA after this many RINGs
        "answer_timeout_s": 60.0,  # ATA -> CONNECT
        "dcd_hangup": True,        # end the call when the modem drops DCD
        "health_interval_s": 60.0, # idle "AT" probe; re-initialise when it fails
    },
    "lan": {
        "enabled": True,           # LAN party port active when a second Ethernet NIC is present
        "interface": "",           # retro-LAN NIC: "" = auto (first extra Ethernet NIC), a name or MAC, or "none"
        "share_internet": False,   # party PCs may reach the internet through this Pi's WAN
        "subnet": "10.99.0.0/24",  # party subnet; the Pi is .1, DHCP hands out .100-.250
        "dns": ["1.1.1.1", "8.8.8.8"],   # upstream resolvers when share_internet is on
        "transit": "172.30.99.0/30",     # veth link between the main and the party namespace
        "netns": "netkey-lan",
        # LAN party
        "tunnel": "172.30.98.0/24",      # WireGuard addresses inside the party namespace (host .1, members .11-.25)
        "party_port": 51899,             # UDP port the host listens on (forward it on the router for internet joins)
        "party_endpoint": "",            # address members use to reach this host ("" = this Pi's LAN IP)
        "party_max": 16,                 # boxes per party, host included
        "party_mac_limit": 8,            # retro devices per member box
        "party_bcast_pps": 300,          # broadcast/multicast frames per second per member
        "party_auto_approve": False,     # host approves join requests automatically
        # cloud hub: introductions, KEY-... codes and the encrypted-packet relay; built into the product
        "hub_url": "https://relay.netkey.no:8443",
        "hub_fingerprint": "cc4045ee943d7ccb21cb6bb2601c5a3251096477bc427d482138445ad65d194f",
        "hub_relay": "",                 # pin parties this box hosts to one relay, e.g. "us-nyc" (tests; "" = nearest)
    },
    "network": {
        "uplink": "ethernet",      # ethernet: built-in port is the uplink | wifi: Wi-Fi is, the built-in port decides
        "hotspot": True,           # rescue Wi-Fi hotspot when the box has had no uplink for a while
    },
    "web": {
        "bind": "",                # all interfaces
        "port": 443,               # HTTPS (self-signed certificate made by install.sh)
        "http_port": 80,           # redirects to HTTPS; 0 disables
        "cert": "/etc/netkey/tls/cert.pem",
        "key": "/etc/netkey/tls/key.pem",
        "session_hours": 12,
        "stats_days": 7,
    },
    "log": {
        "level": "INFO",
        "raw_command_bytes": False,
    },
}

CHOICES = {
    ("serial", "control_lines"): {"auto", "off"},
    ("serial", "pc_dtr_input"): {"dsr", "dcd", "none"},
    ("serial", "dcd_mode"): {"follow", "always_on"},
    ("lines", "default_mode"): set(("virtual", "physical")),
    ("network", "uplink"): {"ethernet", "wifi"},
}
MODES = ("virtual", "physical")
BAUD_CHOICES = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]

# Settings the web GUI may change. They are stored in the state DB and override config.toml.
# (key, type, label, help, applies-to mode or None, extra)
SETTINGS = [
    ("box.name", "string", "Box name",
     "How other boxes see this one in LAN parties: in the host's member list, or as the party's name when you "
     "host. Letters, digits, spaces and - _ . (up to 32). Empty: the hostname.", None,
     {"maxlength": 32, "optional": True}),
    ("lines.default_mode", "choice", "Default modem mode",
     "Virtual: the Pi answers AT commands itself (PC connected directly). "
     "Physical: a real modem is connected to the port and answers incoming calls.", None,
     {"choices": list(MODES)}),
    ("modem.connect_string", "string", "CONNECT message",
     "Result sent to the PC after ATD. Must be listed in the Windows modem driver's INF.", "virtual", {}),
    ("modem.dial_delay_s", "number", "Dial delay (s)", "Pause between ATD and CONNECT.", "virtual",
     {"min": 0, "max": 30}),
    ("serial.autobaud", "bool", "Autobaud", "Follow the PC's speed in command mode.", "virtual", {}),
    ("serial.rtscts", "bool", "RTS/CTS flow control",
     "Only with a cable that crosses RTS/CTS.", "virtual", {}),
    ("serial.dcd_mode", "choice", "DSR/DCD towards the PC",
     "always_on keeps Pi DTR high (Windows XP needs DSR); follow raises it only during a call.", "virtual",
     {"choices": ["always_on", "follow"]}),
    ("serial.pc_dtr_input", "choice", "PC DTR wired to",
     "Pi input that sees the PC's DTR, used for DTR hang-up.", "virtual", {"choices": ["dsr", "dcd", "none"]}),
    ("physical.baud", "choice", "Speed to modem", "Fixed serial speed between the Pi and the modem.",
     "physical", {"choices": BAUD_CHOICES}),
    ("physical.rtscts", "bool", "RTS/CTS flow control", "Hardware flow control with the modem.",
     "physical", {}),
    ("physical.init_string", "string", "Init string",
     "Sent after reset. Keep E0 V1 &C1 (DCD follows carrier) and &D2 (DTR drop hangs up); the serial speed "
     "must stay fixed during calls. USRobotics: start with AT&F1 (hardware flow control, fixed speed &B1).",
     "physical", {}),
    ("physical.answer_rings", "int", "Answer after rings", "Number of RINGs before ATA.", "physical",
     {"min": 1, "max": 15}),
    ("physical.answer_timeout_s", "number", "Answer timeout (s)", "Give up if no CONNECT after ATA.",
     "physical", {"min": 10, "max": 180}),
    ("physical.dcd_hangup", "bool", "Hang up on carrier loss", "End the call when the modem drops DCD.",
     "physical", {}),
    ("physical.health_interval_s", "number", "Health check interval (s)",
     "Idle AT probe; the modem is re-initialised when it does not answer.", "physical",
     {"min": 10, "max": 3600}),
    ("lan.enabled", "bool", "LAN party port",
     "Use a second Ethernet adapter as the retro-LAN port for PCs with a network card.", None, {}),
    ("lan.share_internet", "bool", "Share my internet with the LAN",
     "Retro-LAN PCs may reach the internet through this Pi. All their traffic leaves from your IP address.",
     None, {}),
    ("lan.party_auto_approve", "bool", "Approve join requests automatically",
     "When hosting a LAN party, every box with the invite code joins without asking.", None, {}),
    ("network.uplink", "choice", "Uplink",
     "Ethernet: the built-in port connects the box to your network. Wi-Fi: Wi-Fi does, and the built-in port "
     "becomes the retro-LAN port when cabled to retro PCs (or a management port when cabled into your network).",
     None, {"choices": ["ethernet", "wifi"]}),
    ("network.hotspot", "bool", "Rescue hotspot",
     "When the box has had no network for 5 minutes, it opens its own Wi-Fi so you can reach the GUI.", None, {}),
    ("log.level", "choice", "Log level", "Journal verbosity of the modem lines.", None,
     {"choices": ["DEBUG", "INFO", "WARNING"]}),
    ("log.raw_command_bytes", "bool", "Log raw command bytes",
     "Hex dump of command-mode traffic (debugging).", None, {}),
]
SETTING_KEYS = {s[0]: s for s in SETTINGS}


# keys that existed in an earlier release: ignored (with a warning) so an old config.toml still loads
REMOVED_KEYS = {("lan", "hub_region"): "relays have no regions any more; use lan.hub_relay to pin a relay"}


def load_config(path):
    cfg = copy.deepcopy(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            data = tomllib.load(f)
        for section, values in data.items():
            if section not in cfg or not isinstance(values, dict):
                raise ValueError(f"unknown config section [{section}]")
            for key, value in values.items():
                if (section, key) in REMOVED_KEYS:
                    print(f"warning: {section}.{key} no longer exists and is ignored ({REMOVED_KEYS[section, key]})",
                          file=sys.stderr)
                    continue
                if key not in cfg[section]:
                    raise ValueError(f"unknown config key {section}.{key}")
                cfg[section][key] = value
    for (section, key), allowed in CHOICES.items():
        if cfg[section][key] not in allowed:
            raise ValueError(f"{section}.{key} must be one of {sorted(allowed)}")
    modes = cfg["lines"]["modes"]
    if not isinstance(modes, dict) or any(str(v) not in MODES for v in modes.values()):
        raise ValueError(f"lines.modes values must be one of {list(MODES)}")
    cfg["lines"]["modes"] = {str(k): str(v) for k, v in modes.items()}
    if not 1 <= int(cfg["lines"]["max"]) <= MAX_LINES:
        raise ValueError(f"lines.max must be 1..{MAX_LINES}")
    client_ip(cfg, int(cfg["lines"]["max"]) - 1)
    check_lan(cfg)
    return cfg


def check_lan(cfg):
    """Party subnet: a private /24..../28 that doesn't collide with the dial-in or transit networks."""
    lan = cfg["lan"]
    try:
        net = ipaddress.IPv4Network(lan["subnet"], strict=True)
        transit = ipaddress.IPv4Network(lan["transit"], strict=True)
        dns = [str(ipaddress.IPv4Address(d)) for d in lan["dns"]]
    except (ValueError, TypeError) as e:
        raise ValueError(f"lan: {e}") from None
    if not net.is_private or not 24 <= net.prefixlen <= 28:
        raise ValueError("lan.subnet must be a private /24 to /28 network")
    if transit.prefixlen != 30 or not transit.is_private:
        raise ValueError("lan.transit must be a private /30 network")
    ppp = ipaddress.IPv4Network(f'{cfg["ppp"]["server_ip"]}/24', strict=False)
    for a, b, what in ((net, ppp, "the dial-in network"), (transit, ppp, "the dial-in network"),
                       (net, transit, "lan.transit")):
        if a.overlaps(b):
            raise ValueError(f"lan network {a} overlaps {what} {b}")
    if not dns:
        raise ValueError("lan.dns needs at least one server")
    try:
        tunnel = ipaddress.IPv4Network(lan["tunnel"], strict=True)
    except (ValueError, TypeError) as e:
        raise ValueError(f"lan.tunnel: {e}") from None
    if tunnel.prefixlen != 24 or not tunnel.is_private:
        raise ValueError("lan.tunnel must be a private /24 network")
    for other, what in ((net, "lan.subnet"), (transit, "lan.transit"), (ppp, "the dial-in network")):
        if tunnel.overlaps(other):
            raise ValueError(f"lan.tunnel {tunnel} overlaps {what} {other}")
    if not 1024 <= int(lan["party_port"]) <= 65535:
        raise ValueError("lan.party_port must be 1024-65535")
    if not 2 <= int(lan["party_max"]) <= 16:
        raise ValueError("lan.party_max must be 2-16")
    if not 1 <= int(lan["party_mac_limit"]) <= 64:
        raise ValueError("lan.party_mac_limit must be 1-64")
    if not 10 <= int(lan["party_bcast_pps"]) <= 10000:
        raise ValueError("lan.party_bcast_pps must be 10-10000")
    if lan["hub_url"] and not str(lan["hub_url"]).startswith("https://"):
        raise ValueError("lan.hub_url must start with https://")
    fp = str(lan["hub_fingerprint"]).lower().replace(":", "")
    if fp and (len(fp) != 64 or any(ch not in "0123456789abcdef" for ch in fp)):
        raise ValueError("lan.hub_fingerprint must be a SHA-256 hex fingerprint")
    if not re.match(r"^[a-z0-9-]{0,32}$", str(lan["hub_relay"])):
        raise ValueError("lan.hub_relay must be a relay name like us-nyc (empty = the nearest relay)")
    if not str(lan["netns"]).replace("-", "").isalnum():
        raise ValueError("lan.netns must be alphanumeric")


def client_ip(cfg, line):
    if not 0 <= line < MAX_LINES:
        raise ValueError(f"line must be 0..{MAX_LINES - 1}")
    server = ipaddress.IPv4Address(cfg["ppp"]["server_ip"])
    ip = ipaddress.IPv4Address(cfg["ppp"]["first_client_ip"]) + line
    if ip == server or ipaddress.ip_network(f"{server}/24", strict=False) != \
            ipaddress.ip_network(f"{ip}/24", strict=False):
        raise ValueError(f"client IP {ip} for line {line} must be in the server's /24 and differ from it")
    return str(ip)


def get_setting(cfg, key):
    section, name = key.split(".", 1)
    return cfg[section][name]


def clean_box_name(name):
    return "".join(ch for ch in str(name or "") if ch.isalnum() or ch in "-_. ")[:32].strip()


def box_name(cfg):
    """The name other boxes see: the GUI/config name, else the hostname."""
    return clean_box_name(cfg["box"]["name"]) or clean_box_name(socket.gethostname()) or "NetKey"


def check_setting(key, value):
    """Validated, type-normalised value for a GUI setting; raises ValueError."""
    if key not in SETTING_KEYS:
        raise ValueError(f"unknown setting {key}")
    _, kind, label, _, _, extra = SETTING_KEYS[key]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{label}: must be true or false")
        return value
    if kind in ("int", "number"):
        if isinstance(value, bool):
            raise ValueError(f"{label}: must be a number")
        try:
            value = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: must be a number") from None
        if not extra.get("min", value) <= value <= extra.get("max", value):
            raise ValueError(f"{label}: must be between {extra['min']} and {extra['max']}")
        return value
    if kind == "choice":
        for choice in extra["choices"]:
            if value == choice or str(value) == str(choice):
                return choice
        raise ValueError(f"{label}: must be one of {', '.join(map(str, extra['choices']))}")
    value = str(value).strip()
    if key == "box.name":
        if value != clean_box_name(value):
            raise ValueError(f"{label}: up to 32 letters, digits, spaces and - _ .")
        return value
    if not value or len(value) > 120 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{label}: must be 1-120 printable characters")
    if key == "physical.init_string" and not value.upper().startswith("AT"):
        raise ValueError(f"{label}: must start with AT")
    return value


def apply_overrides(cfg, overrides, line_modes=None, warn=None):
    """Copy of cfg with GUI settings applied. Invalid entries are skipped (reported through warn)."""
    c = copy.deepcopy(cfg)
    for key, value in (overrides or {}).items():
        try:
            value = check_setting(key, value)
        except ValueError as e:
            if warn:
                warn(f"ignoring setting {key}: {e}")
            continue
        section, name = key.split(".", 1)
        c[section][name] = value
    for line, mode in (line_modes or {}).items():
        if mode in MODES:
            c["lines"]["modes"][str(line)] = mode
        elif warn:
            warn(f"ignoring mode {mode!r} for line {line}")
    return c


def line_mode(cfg, line):
    return cfg["lines"]["modes"].get(str(line), cfg["lines"]["default_mode"])


def line_config(cfg, line):
    """cfg specialised for one modem line: device path and pppd command get the line number, plus its mode."""
    client_ip(cfg, line)
    c = copy.deepcopy(cfg)
    c["line"] = line
    c["mode"] = line_mode(cfg, line)
    c["serial"]["device"] = cfg["serial"]["device"].format(line=line)
    c["ppp"]["command"] = [str(s).format(line=line) for s in cfg["ppp"]["command"]]
    if c["mode"] == "physical" and c["physical"]["rtscts"]:
        c["ppp"]["command"].append("crtscts")   # overrides nocrtscts from the peers file
    return c


def render_peer(cfg, line, template):
    return template.format(line=line, server_ip=cfg["ppp"]["server_ip"], client_ip=client_ip(cfg, line),
                           server_name=cfg["ppp"]["server_name"])


_SAFE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


def check_user(name, password):
    """Raises ValueError unless name/password can be written to pap-secrets safely."""
    if not _SAFE.match(str(name)):
        raise ValueError("user name: 1-64 characters from A-Z a-z 0-9 . _ @ -")
    password = str(password)
    if not password or len(password) > 64 or any(ch in password for ch in '"\\') or \
            any(ord(ch) < 32 or ord(ch) == 127 for ch in password):
        raise ValueError('password: 1-64 printable characters, no " or \\')


def pap_secrets(cfg, lines=1, users=None):
    """Lines for /etc/ppp/pap-secrets: client  server  "secret"  allowed-IPs (every line's client IP).
    users defaults to config [auth]; disabled users (enabled false) are left out."""
    ips = " ".join(client_ip(cfg, n) for n in range(lines))
    out = []
    for user in cfg["auth"]["users"] if users is None else users:
        if not user.get("enabled", True):
            continue
        name, password = str(user["name"]), str(user["password"])
        try:
            check_user(name, password)
        except ValueError:
            raise ValueError(f"invalid PPP user entry for {name!r}") from None
        out.append(f'{name}\t{cfg["ppp"]["server_name"]}\t"{password}"\t{ips}')
    return "\n".join(out)
