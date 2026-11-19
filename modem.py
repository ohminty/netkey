#!/usr/bin/env python3
"""NetKey entry point: emulate a Hayes modem on a serial port and hand calls to pppd."""
import argparse
import logging
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from netkey.config import apply_overrides, line_config, load_config, pap_secrets, render_peer  # noqa: E402
from netkey.status import RUN_DIR  # noqa: E402

log = logging.getLogger("netkey")


def effective_config(base, state_dir):
    """config.toml + settings changed in the web GUI (state DB). The DB is optional."""
    try:
        from netkey.store import Store
        store = Store(state_dir)
    except Exception as e:  # noqa: BLE001 - modem lines must work without the DB
        log.warning("state DB unavailable (%s); using config.toml only", e)
        return base, None
    return apply_overrides(base, store.get_settings(), store.get_line_modes(), log.warning), store


def party_cli(args):
    """LAN party commands: queued for the root LAN service, which applies them within a couple of seconds."""
    import json
    from netkey import lan, party
    run_dir = args.lan_run_dir or lan.RUN_DIR
    cmd, rest = args.party[0], args.party[1:]
    if cmd == "status":
        p = lan.read_party(run_dir)
        print(json.dumps(p, indent=2) if p else "LAN service not running")
        return 0 if p else 1
    fields = {"create": "endpoint", "join": "code", "approve": "member", "reject": "member", "kick": "member",
              "endpoint": "endpoint"}
    if cmd == "create":
        via = "direct" if rest[:1] == ["direct"] else "hub"
        rest = rest[1:] if rest[:1] in (["direct"], ["hub"]) else rest
    kw = {fields[cmd]: " ".join(rest)} if cmd in fields and rest else {}
    if cmd == "create":
        kw["via"] = via
    cid = party.queue_command(args.state_dir, cmd, **kw)
    r = party.wait_result(run_dir, cid)
    if r is None:
        print("no answer from the LAN service (is netkey-lan running?)", file=sys.stderr)
        return 2
    print(r["message"])
    if cmd == "create" and r["ok"]:
        p = lan.read_party(run_dir) or {}
        if p.get("code"):
            print(f"invite code: {p['code']}")
    return 0 if r["ok"] else 1


def wifi_cli(args):
    """Network uplink: status | scan | connect SSID [PASSWORD] COUNTRY=xx | forget (queued for the LAN service)."""
    import json
    from netkey import lan, party
    run_dir = args.lan_run_dir or lan.RUN_DIR
    cmd, rest = args.wifi[0], args.wifi[1:]
    if cmd == "status":
        n = lan.read_net(run_dir)
        print(json.dumps(n, indent=2) if n else "LAN service not running")
        return 0 if n else 1
    kw = {}
    if cmd == "connect":
        country = next((r.split("=", 1)[1] for r in rest if r.lower().startswith("country=")), "")
        rest = [r for r in rest if not r.lower().startswith("country=")]
        if not rest:
            print("usage: --wifi connect SSID [PASSWORD] [country=NO]", file=sys.stderr)
            return 2
        kw = {"ssid": rest[0], "password": rest[1] if len(rest) > 1 else "", "country": country}
    elif cmd not in ("scan", "forget"):
        print("usage: --wifi status | scan | connect SSID [PASSWORD] [country=NO] | forget", file=sys.stderr)
        return 2
    cid = party.queue_command(args.state_dir, "wifi_" + cmd, **kw)
    r = party.wait_result(run_dir, cid, timeout=80)
    if r is None:
        print("no answer from the LAN service (is netkey-lan running?)", file=sys.stderr)
        return 2
    print(r["message"])
    if cmd == "scan" and r["ok"]:
        for n in (lan.read_net(run_dir) or {}).get("scan", {}).get("networks", []):
            print(f"  {n['signal']:3d}%  {n['band']:7s}  {'secured' if n['secure'] else 'open   '}  {n['ssid']}")
    return 0 if r["ok"] else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="/etc/netkey/config.toml")
    p.add_argument("--manager", action="store_true", help="run the hot-plug manager (one line per USB adapter)")
    p.add_argument("--state-dir", default=os.environ.get("STATE_DIRECTORY", "/var/lib/netkey"))
    p.add_argument("--line", type=int, default=0, help="run a single modem line")
    p.add_argument("--device", help="override serial.device for --line")
    p.add_argument("--control-lines", choices=["auto", "off"], help="override serial.control_lines")
    p.add_argument("--mode", choices=["virtual", "physical"], help="override the line's modem mode")
    p.add_argument("--print-pap-secrets", action="store_true", help="print pap-secrets lines and exit")
    p.add_argument("--print-max-lines", action="store_true", help="print lines.max and exit")
    p.add_argument("--render-peer", metavar="TEMPLATE", help="print the pppd peers file for --line and exit")
    p.add_argument("--run-dir", default=os.environ.get("RUNTIME_DIRECTORY", RUN_DIR))
    p.add_argument("--web", action="store_true", help="run the web GUI and stats collector")
    p.add_argument("--demo", action="store_true", help="with --web: simulated lines, plain HTTP, login admin/admin")
    p.add_argument("--port", type=int, help="with --web: listen port (default web.port, demo 8080)")
    p.add_argument("--init-db", action="store_true",
                   help="create the state DB, import [auth] users once, export them for pap-secrets")
    p.add_argument("--set-admin-password", action="store_true",
                   help="set the web GUI password (reads it from stdin; random one printed if empty)")
    p.add_argument("--reset-admin", action="store_true",
                   help="reset the web GUI login to admin / admin")
    p.add_argument("--if-unset", action="store_true", help="with --reset-admin: only if no login is set yet")
    p.add_argument("--lan", action="store_true", help="(root) run the LAN party port service")
    p.add_argument("--lan-interface", help="with --lan: use this interface as retro-LAN port (name or MAC)")
    p.add_argument("--lan-once", action="store_true", help="with --lan: reconcile once and exit (debugging)")
    p.add_argument("--lan-share", choices=["on", "off"], help="with --lan: override lan.share_internet (self-test)")
    p.add_argument("--lan-teardown", action="store_true", help="(root) remove the LAN namespace, rules and NIC pin")
    p.add_argument("--lan-netns", help="with --lan: party namespace name (self-test runs a second box)")
    p.add_argument("--lan-run-dir", default=None, help="with --lan/--party: run directory (default /run/netkey-lan)")
    p.add_argument("--party", nargs="+", metavar="CMD",
                   help="LAN party: status | create (via the cloud hub) | create direct [ENDPOINT] | join CODE | "
                        "approve NAME|all | reject NAME | kick NAME|ID | regenerate | endpoint HOST | leave | close")
    p.add_argument("--wifi", nargs="+", metavar="CMD",
                   help="network uplink: status | scan | connect SSID [PASSWORD] [country=NO] | forget")
    p.add_argument("--update", action="store_true",
                   help="(root, netkey-update.service) install the release an admin asked for in the GUI")
    p.add_argument("--sync-pap-secrets", action="store_true",
                   help="(root) rewrite the netkey block of /etc/ppp/pap-secrets from the GUI user list")
    args = p.parse_args()

    base = load_config(args.config)
    if args.print_pap_secrets:
        print(pap_secrets(base, int(base["lines"]["max"])))
        return 0
    if args.print_max_lines:
        print(int(base["lines"]["max"]))
        return 0
    if args.render_peer:
        with open(args.render_peer) as f:
            sys.stdout.write(render_peer(base, args.line, f.read()))
        return 0

    if args.web:
        logging.basicConfig(level=base["log"]["level"], format="%(levelname)s %(message)s", stream=sys.stderr)
        from netkey import web
        return web.run(base, args.state_dir, args.run_dir, port=args.port, demo=args.demo)
    if args.lan or args.lan_teardown:
        logging.basicConfig(level=base["log"]["level"], format="%(levelname)s %(message)s", stream=sys.stderr)
        from netkey import lan
        run_dir = args.lan_run_dir or lan.RUN_DIR
        if args.lan_teardown:
            return lan.teardown(base, args.state_dir, netns=args.lan_netns, run_dir=run_dir)
        share = None if args.lan_share is None else args.lan_share == "on"
        return lan.run(base, args.state_dir, interface=args.lan_interface, once=args.lan_once, share=share,
                       netns=args.lan_netns, run_dir=run_dir)
    if args.update:
        from netkey import updater
        return updater.run(base, args.state_dir)
    if args.party:
        return party_cli(args)
    if args.wifi:
        return wifi_cli(args)
    if args.init_db:
        from netkey.store import Store
        store = Store(args.state_dir)
        n = store.seed_users(base["auth"]["users"])
        if not os.path.exists(os.path.join(args.state_dir, "pap-users.json")):
            store.export_users()
        store.export_settings()
        print(f"state DB {store.path}: {len(store.list_users())} dial-in user(s)" +
              (f", imported {n} from config.toml" if n else ""))
        return 0
    if args.reset_admin:
        from netkey.store import Store
        store = Store(args.state_dir)
        if args.if_unset and store.admin_hash():
            return 0
        store.reset_admin()
        print("web GUI login reset to admin / admin")
        return 0
    if args.set_admin_password:
        import secrets
        from netkey.store import Store
        store = Store(args.state_dir)
        if sys.stdin.isatty():
            import getpass
            password = getpass.getpass("New web GUI password (empty = random): ")
        else:
            password = sys.stdin.readline().strip()
        if not password:
            password = secrets.token_urlsafe(9)
            print(password)
        store.set_admin_password(password)
        return 0
    if args.sync_pap_secrets:
        from netkey.papsync import sync
        changed, names = sync(base, args.state_dir)
        print(f"pap-secrets {'updated' if changed else 'unchanged'}: {' '.join(names) or '(no enabled users)'}")
        return 0

    if args.manager:
        from netkey.manager import Manager
        logging.basicConfig(level=base["log"]["level"], format="%(levelname)s %(message)s", stream=sys.stderr)
        manager = Manager(base, os.path.abspath(args.config), args.state_dir, args.run_dir)
        signal.signal(signal.SIGTERM, manager.stop)
        signal.signal(signal.SIGINT, manager.stop)
        manager.run()
        return 0

    from netkey.session import Session
    logging.basicConfig(level=base["log"]["level"], format=f"%(levelname)s line{args.line}: %(message)s",
                        stream=sys.stderr)
    eff, store = effective_config(base, args.state_dir)
    if args.mode:
        eff["lines"]["modes"][str(args.line)] = args.mode
    cfg = line_config(eff, args.line)
    logging.getLogger().setLevel(cfg["log"]["level"])
    if args.device:
        cfg["serial"]["device"] = args.device
    if args.control_lines:
        cfg["serial"]["control_lines"] = args.control_lines

    session = Session(cfg, run_dir=args.run_dir if os.path.isdir(args.run_dir) else None, store=store)
    signal.signal(signal.SIGTERM, session.stop)
    signal.signal(signal.SIGINT, session.stop)
    if cfg["mode"] == "physical":
        log.info("starting line %d (physical modem mode): device=%s %d baud rtscts=%s init=%r pppd=%r",
                 args.line, cfg["serial"]["device"], cfg["physical"]["baud"], cfg["physical"]["rtscts"],
                 cfg["physical"]["init_string"], " ".join(cfg["ppp"]["command"]))
    else:
        log.info("starting line %d (virtual modem mode): device=%s pppd=%r connect=%r", args.line,
                 cfg["serial"]["device"], " ".join(cfg["ppp"]["command"]), cfg["modem"]["connect_string"])
    session.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
