"""Serial session: command mode with autobaud, then hands the open tty to pppd for the call.

Virtual Modem Mode lines also take part in modem calls (calls.py): 555 numbers ring other lines or other boxes, and
the session pumps the bytes of such a call itself (no pppd), with +++ / ATO / ATH like a real modem."""
import errno
import fcntl
import logging
import os
import select
import struct
import subprocess
import termios
import time

from . import calls as cl
from . import status as st
from .atparser import Answer, ATParser, Dial, Hangup, Online

log = logging.getLogger("netkey")

BAUDS = {1200: termios.B1200, 2400: termios.B2400, 4800: termios.B4800, 9600: termios.B9600,
         19200: termios.B19200, 38400: termios.B38400, 57600: termios.B57600, 115200: termios.B115200}
# Windows 95 detection probes at 9600 (about 4 tries, 0.5 s apart) then falls back to 2400.
SLOW_LADDER = [9600, 2400, 1200]
FAST_LADDER = [115200, 57600, 38400, 19200]
DTR_DEBOUNCE_S = 0.2
AUTOBAUD_IDLE_RESET_S = 3.0
HEARTBEAT_S = 10.0          # idle status refresh (the GUI treats > status.STALE_S as a dead worker)
CALL_STATUS_S = 2.0         # status refresh with traffic counters during a call
DCD_DEBOUNCE_S = 0.5
RING_RESET_S = 8.0          # RINGs further apart than this start a new incoming call
MODEM_RETRY_S = 10.0
# pppd exit status -> call result (pppd(8) "EXIT STATUS")
PPPD_RESULTS = {0: "ok", 5: "ok", 16: "ok", 11: "auth_failed", 19: "auth_failed", 10: "negotiation_failed",
                15: "timeout", 12: "ok", 13: "ok"}


def looks_valid(data):
    printable = sum(1 for b in data if 32 <= b < 127 or b in (8, 10, 13))
    return printable * 2 >= len(data)


def next_baud(baud, burst_len, fast_mode):
    """Pick the next speed after a garbage burst. Returns (baud, fast_mode)."""
    if burst_len > 12:
        # many garbage bytes per character: the sender is slower than us
        if baud > SLOW_LADDER[0]:
            return SLOW_LADDER[0], False
        i = SLOW_LADDER.index(baud) if baud in SLOW_LADDER else -1
        return SLOW_LADDER[(i + 1) % len(SLOW_LADDER)], False
    if not fast_mode:
        return FAST_LADDER[0], True
    i = FAST_LADDER.index(baud) if baud in FAST_LADDER else -1
    return FAST_LADDER[(i + 1) % len(FAST_LADDER)], True


class DeviceGone(Exception):
    pass


class Session:
    def __init__(self, cfg, run_dir=None, store=None):
        self.cfg = cfg
        self.serial = cfg["serial"]
        self.line = int(cfg.get("line", 0))
        self.mode = cfg.get("mode", "virtual")
        self.physical = self.mode == "physical"
        self.run_dir = run_dir
        self.store = store
        self.stopping = False
        self.fd = None
        self.child = None
        self.parser = None
        self.lines_ok = False
        self.baud = self.serial["baud"]
        self.state = None
        self.state_since = 0.0
        self.detail = ""
        self.call = None
        self.last_status = 0.0
        self.modem_buf = b""
        self.modem_model = None
        self.last_call_id = None
        self.last_reason = ""
        self.calls = None if self.physical else cl.CallClient(self.line, cfg.get("calls_socket"))
        self.ring = None                 # {"call", "count", "next"} while a call rings this line
        self.confirmed_baud = None       # speed of the last AT command understood (RING goes out at this speed)

    def stop(self, *_):
        self.stopping = True

    # status for the web GUI
    def set_state(self, state, detail=""):
        if state != self.state:
            self.state_since = time.time()
            log.debug("state %s (%s)", state, detail)
        self.state, self.detail = state, detail
        self.write_status()

    def write_status(self):
        self.last_status = time.monotonic()
        if not self.run_dir:
            return
        st.write(st.line_file(self.run_dir, self.line), {
            "line": self.line, "mode": self.mode, "state": self.state, "state_since": self.state_since,
            "detail": self.detail, "baud": self.baud, "pid": os.getpid(), "updated": time.time(),
            "call": self.call})

    def heartbeat(self):
        if time.monotonic() - self.last_status >= HEARTBEAT_S:
            self.write_status()

    def db(self, method, *args, **kwargs):
        """Store call, never fatal: the modem line must keep working when the DB is busy or broken."""
        if self.store is None:
            return None
        try:
            return getattr(self.store, method)(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            log.warning("state DB %s failed: %s", method, e)
            return None

    # lifecycle
    def run(self):
        self.db("close_stale_calls", self.line)
        while not self.stopping:
            if not self.wait_for_device():
                break
            try:
                self.open()
                if self.physical:
                    self.physical_loop()
                else:
                    self.command_loop()
            except DeviceGone as e:
                log.warning("serial device lost: %s", e)
            except (OSError, termios.error) as e:
                if getattr(e, "errno", None) in (errno.EIO, errno.ENXIO, errno.ENODEV) or \
                        (isinstance(e, termios.error) and e.args and e.args[0] in (errno.EIO, errno.ENXIO)):
                    log.warning("serial device lost: %s", e)
                else:
                    log.error("serial error: %s", e)
                time.sleep(1)
            finally:
                self.close()
        self.set_state(st.STOPPED)
        log.info("stopped")

    def wait_for_device(self):
        dev = self.serial["device"]
        logged = False
        while not self.stopping:
            if os.path.exists(dev):
                return True
            if not logged:
                log.warning("waiting for %s", dev)
                self.set_state(st.UNPLUGGED, dev)
                logged = True
            self.heartbeat()
            time.sleep(1)
        return False

    def open(self):
        dev = self.serial["device"]
        self.fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, termios.TIOCEXCL)
        self.baud = self.cfg["physical"]["baud"] if self.physical else self.serial["baud"]
        self.rtscts = self.cfg["physical"]["rtscts"] if self.physical else self.serial["rtscts"]
        self.fast_mode = False
        self.set_termios()
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.lines_ok = self.serial["control_lines"] != "off" and self.probe_lines()
        self.dtr_armed = False
        self.dtr_low_since = None
        self.dcd_armed = False
        self.dcd_low_since = None
        self.set_line(termios.TIOCM_RTS, True)
        if self.physical:
            self.set_line(termios.TIOCM_DTR, True)   # the modem ignores us (&D2) while DTR is low
        else:
            self.set_carrier(False)
        self.parser = ATParser(self.cfg["modem"]["model"], self.cfg["modem"]["connect_string"])
        self.modem_buf = b""
        log.info("opened %s (%s) at %d 8N1, control lines %s%s", dev, os.path.realpath(dev), self.baud,
                 "available" if self.lines_ok else "not used", ", RTS/CTS" if self.rtscts else "")

    def close(self):
        if self.child:
            self.stop_child()
        if self.fd is not None:
            try:
                self.set_carrier(False)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None

    # tty helpers
    def set_termios(self):
        a = termios.tcgetattr(self.fd)
        a[0] = 0
        a[1] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        if getattr(self, "rtscts", self.serial["rtscts"]):
            a[2] |= getattr(termios, "CRTSCTS", 0)
        a[3] = 0
        a[4] = a[5] = BAUDS[self.baud]
        a[6][termios.VMIN] = 1
        a[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, a)

    def probe_lines(self):
        try:
            fcntl.ioctl(self.fd, termios.TIOCMGET, struct.pack("i", 0))
            return True
        except OSError:
            return False

    def modem_bits(self):
        return struct.unpack("i", fcntl.ioctl(self.fd, termios.TIOCMGET, struct.pack("i", 0)))[0]

    def set_line(self, bit, on):
        if self.lines_ok:
            fcntl.ioctl(self.fd, termios.TIOCMBIS if on else termios.TIOCMBIC, struct.pack("i", bit))

    def set_carrier(self, on):
        # Pi DTR drives the PC's DCD on full-handshake cables
        self.set_line(termios.TIOCM_DTR, on or self.serial["dcd_mode"] == "always_on")

    def pc_dtr_dropped(self):
        """True once the PC's DTR has been low for DTR_DEBOUNCE_S after having been seen high."""
        src = self.serial["pc_dtr_input"]
        if not self.lines_ok or src == "none":
            return False
        bits = self.modem_bits()
        if self.cfg["log"]["raw_command_bytes"] and bits != getattr(self, "last_bits", None):
            names = [(termios.TIOCM_DTR, "DTR"), (termios.TIOCM_RTS, "RTS"), (termios.TIOCM_CTS, "CTS"),
                     (termios.TIOCM_DSR, "DSR"), (termios.TIOCM_CAR, "DCD"), (termios.TIOCM_RNG, "RI")]
            log.info("LINES %s", " ".join(f"{n}={int(bool(bits & b))}" for b, n in names))
            self.last_bits = bits
        high = bool(bits & (termios.TIOCM_DSR if src == "dsr" else termios.TIOCM_CAR))
        if high:
            if not self.dtr_armed:
                log.info("PC DTR seen asserted; DTR hang-up enabled")
                if self.child is None:
                    self.reset_autobaud("PC opened the port")
            self.dtr_armed = True
            self.dtr_low_since = None
            return False
        if not self.dtr_armed:
            return False
        now = time.monotonic()
        if self.dtr_low_since is None:
            self.dtr_low_since = now
        return now - self.dtr_low_since >= DTR_DEBOUNCE_S

    def read(self):
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return b""
        except OSError as e:
            raise DeviceGone(e) from e
        if not data:
            raise DeviceGone("end of file (adapter unplugged?)")
        return data

    def write(self, data):
        if data and self.child is None and self.cfg["log"]["raw_command_bytes"]:
            log.info("TX @%d %s", self.baud, data.hex(" "))
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                time.sleep(0.01)
            except OSError as e:
                raise DeviceGone(e) from e

    # command mode
    def reset_autobaud(self, why):
        if self.serial["autobaud"] and self.baud != self.serial["baud"]:
            self.baud = self.serial["baud"]
            self.fast_mode = False
            self.set_termios()
            self.parser.reset_line()
            log.info("autobaud reset -> %d (%s)", self.baud, why)

    def command_loop(self):
        last_rx = time.monotonic()
        self.set_state(st.IDLE, "Waiting for ATD")
        while not self.stopping:
            self.heartbeat()
            self.call_tick()
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if self.pc_dtr_dropped() and self.parser.amp_d:
                log.info("PC dropped DTR in command mode")
                self.parser.reset_line()
                self.dtr_armed = False
            if not r:
                if time.monotonic() - last_rx > AUTOBAUD_IDLE_RESET_S:
                    self.reset_autobaud("idle")
                continue
            time.sleep(0.02)
            data = self.read()
            if not data:
                continue
            last_rx = time.monotonic()
            if self.cfg["log"]["raw_command_bytes"]:
                log.info("RX @%d %s", self.baud, data.hex(" "))
            if self.serial["autobaud"] and not looks_valid(data):
                time.sleep(0.15)  # let the rest of the garbage burst arrive before switching
                try:
                    data += os.read(self.fd, 4096)
                except BlockingIOError:
                    pass
                self.baud, self.fast_mode = next_baud(self.baud, len(data), self.fast_mode)
                self.set_termios()
                termios.tcflush(self.fd, termios.TCIFLUSH)
                self.parser.reset_line()
                log.info("autobaud -> %d", self.baud)
                continue
            self.fast_mode = False
            out, action = self.parser.feed(data)
            for line in self.parser.commands:
                log.info("CMD %s", line)
            if self.parser.commands:
                self.confirmed_baud = self.baud
            self.write(out)
            if isinstance(action, Dial):
                self.dial(action.number)
            elif isinstance(action, Answer):
                self.answer_call()
            elif isinstance(action, (Online, Hangup)):
                self.write(self.parser.result("NO CARRIER" if isinstance(action, Online) else "OK"))

    def dial(self, number):
        kind, _ = cl.classify(number)
        if kind != "ppp" and self.calls is not None and self.netkey_dial(number, kind):
            return
        self.set_state(st.DIALING, f"ATD{number}".strip())
        deadline = time.monotonic() + float(self.cfg["modem"]["dial_delay_s"])
        while time.monotonic() < deadline:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r and self.read():
                log.info("dial aborted by keypress")
                self.write(self.parser.result("NO CARRIER"))
                self.set_state(st.IDLE, "Dial aborted")
                return
        if not number.strip():
            log.info("empty dial string -> NO CARRIER")
            self.write(self.parser.result("NO CARRIER"))
            self.set_state(st.IDLE, "Empty dial string")
            return
        connect = self.parser.connect_text()
        self.write(self.parser.result(connect))
        termios.tcdrain(self.fd)
        log.info("%s @%d baud, handing line to pppd", connect, self.baud)
        self.set_carrier(True)
        self.data_mode(connect)
        self.after_call()

    # modem calls (calls.py): ringing, answering, dialing 555 numbers, and the call's data pump
    def listening(self):
        """Is the machine on this line in modem mode? Only knowable when the cable carries DTR."""
        src = self.serial["pc_dtr_input"]
        if not self.lines_ok or src == "none":
            return True
        try:
            bits = self.modem_bits()
        except OSError:
            return False
        return bool(bits & (termios.TIOCM_DSR if src == "dsr" else termios.TIOCM_CAR))

    def call_tick(self):
        if self.calls is None:
            return
        self.calls.update(self.ring is None and self.listening(), self.confirmed_baud or self.baud)
        now = time.monotonic()
        for msg in self.calls.poll():
            op, cid = msg.get("op"), msg.get("call")
            if op == "ring" and self.ring is None:
                self.ring = {"call": cid, "count": 0, "next": now}
                if self.confirmed_baud and self.confirmed_baud != self.baud:
                    self.baud = self.confirmed_baud          # ring at the speed the machine last talked at
                    self.set_termios()
                log.info("incoming call")
                self.set_state(st.RINGING, "Incoming call")
            elif op == "stop_ring" and self.ring and self.ring["call"] == cid:
                self.ring = None
                self.parser.p.sregs[1] = 0
                if msg.get("missed"):
                    log.info("missed call")
                    t = time.time()
                    cid_db = self.db("start_call", self.line, "call", "", t, result="missed")
                    if cid_db is not None:
                        self.db("update_call", cid_db, ended=t, detail="Incoming call")
                self.set_state(st.IDLE, "Waiting for ATD")
        if self.ring and now >= self.ring["next"]:
            self.ring["count"] += 1
            self.ring["next"] = now + cl.RING_EVERY_S
            self.parser.p.sregs[1] = min(self.ring["count"], 255)
            self.write(self.parser.result("RING"))
            s0 = self.parser.p.sregs.get(0, 0)
            if s0 and self.ring["count"] >= s0:
                self.answer_call()

    def keypress(self):
        r, _, _ = select.select([self.fd], [], [], 0)
        return bool(r and self.read())

    def call_failed(self, result, detail, code):
        t = time.time()
        cid = self.db("start_call", self.line, "call", "", t, result=result)
        if cid is not None:
            self.db("update_call", cid, ended=t, detail=detail)
        self.write(self.parser.result(code))
        self.set_state(st.IDLE, "Waiting for ATD")

    def netkey_dial(self, number, kind):
        """Extensions and NetKey numbers. False: dial-up internet after all (this box has no number)."""
        n = cl.digits(number)[-7:]
        who = f"line {int(n) - cl.EXT_BASE}" if kind == "ext" else cl.fmt_number(n)
        self.set_state(st.DIALING, f"Calling {who}")
        self.calls.update(False, self.confirmed_baud or self.baud, force=True)
        r = self.calls.request("dial", "dial_result", 30, abort=self.keypress, number=n)
        if r is None:
            if kind == "number":
                return False
            self.call_failed("no_dialtone", f"To {who}", "NO DIALTONE")
            return True
        res = r.get("result")
        if res == "ppp":
            return False
        if res == "abort":
            self.calls.send(op="cancel", line=self.line, call=r.get("call"))
            self.call_failed("cancelled", f"To {who}", "NO CARRIER")
            return True
        if res != "ringing":
            code = {"busy": "BUSY", "no_dialtone": "NO DIALTONE"}.get(res, "NO CARRIER")
            self.call_failed(res if res in ("busy", "no_dialtone") else "no_carrier", f"To {who}", code)
            return True
        try:
            sock = self.calls.attach(r["call"])
        except OSError:
            self.call_failed("no_carrier", f"To {who}", "NO CARRIER")
            return True
        self.set_state(st.DIALING, f"Calling {who}, ringing")
        reader = cl.FrameReader()
        s7 = max(int(self.parser.p.sregs.get(7, 50)), 5)
        deadline = time.monotonic() + s7
        outcome = None
        try:
            while outcome is None:
                if self.stopping:
                    outcome = ("NO CARRIER", "cancelled")
                    break
                if time.monotonic() > deadline:
                    sock.sendall(cl.frame(cl.HANGUP, '{"reason":"no_answer"}'))
                    outcome = ("NO ANSWER", "no_answer")
                    break
                rr, _, _ = select.select([self.fd, sock], [], [], 0.1)
                if self.fd in rr and self.read():
                    sock.sendall(cl.frame(cl.HANGUP, '{"reason":"cancel"}'))
                    outcome = ("NO CARRIER", "cancelled")
                    break
                if sock in rr:
                    try:
                        chunk = sock.recv(8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        outcome = ("NO CARRIER", "no_carrier")
                        break
                    for k, payload in reader.feed(chunk):
                        if k == cl.ANSWER:
                            sock.sendall(cl.frame(cl.CONNECTED))
                            outcome = ("CONNECT", None)
                            break
                        if k == cl.HANGUP:
                            reason = cl.payload_json(payload).get("reason")
                            outcome = {"no_answer": ("NO ANSWER", "no_answer"),
                                       "busy": ("BUSY", "busy")}.get(reason, ("NO CARRIER", "no_carrier"))
                            break
        except (OSError, cl.FrameError):
            outcome = ("NO CARRIER", "no_carrier")
        if outcome[0] != "CONNECT":
            try:
                sock.close()
            except OSError:
                pass
            self.call_failed(outcome[1], f"To {who}", outcome[0])
            return True
        self.call_mode(sock, reader, f"To {who}")
        return True

    def answer_call(self):
        ring, self.ring = self.ring, None
        self.parser.p.sregs[1] = 0
        if not ring or self.calls is None:
            self.write(self.parser.result("NO CARRIER"))
            return
        r = self.calls.request("answer", "answer_result", 5, call=ring["call"])
        if not r or not r.get("ok"):
            self.write(self.parser.result("NO CARRIER"))
            self.set_state(st.IDLE, "Waiting for ATD")
            return
        self.set_state(st.ANSWERING, "Answering")
        reader = cl.FrameReader()
        try:
            sock = self.calls.attach(ring["call"])
            sock.sendall(cl.frame(cl.ANSWER))
            deadline = time.monotonic() + cl.ANSWER_CONFIRM_S
            confirmed = False
            while not confirmed and time.monotonic() < deadline:
                rr, _, _ = select.select([sock], [], [], 0.1)
                if not rr:
                    continue
                chunk = sock.recv(8192)
                if not chunk:
                    break
                for k, _ in reader.feed(chunk):
                    if k == cl.CONNECTED:
                        confirmed = True
                    elif k == cl.HANGUP:
                        deadline = 0
        except (OSError, cl.FrameError, BlockingIOError):
            confirmed = False
        if not confirmed:
            try:
                sock.close()
            except (OSError, UnboundLocalError):
                pass
            self.write(self.parser.result("NO CARRIER"))
            self.set_state(st.IDLE, "Waiting for ATD")
            return
        self.call_mode(sock, reader, "Incoming call")

    def call_mode(self, sock, reader, detail):
        """CONNECT, then pump bytes between the serial port and the call until either side hangs up."""
        connect = self.parser.connect_text()
        self.write(self.parser.result(connect))
        try:
            termios.tcdrain(self.fd)
        except termios.error:
            pass
        self.set_carrier(True)
        started = time.time()
        self.call = {"connect": connect, "started": started, "rx_bytes": 0, "tx_bytes": 0, "user": None, "ip": None,
                     "iface": None, "kind": "call", "peer": detail}
        call_id = self.db("start_call", self.line, "call", connect, started)
        if call_id is not None:
            self.db("update_call", call_id, detail=detail)
        self.set_state(st.CONNECTED, f"{connect} · {detail}")
        log.info("%s @%d baud, modem call (%s)", connect, self.baud, detail)
        sregs = self.parser.p.sregs
        esc = sregs.get(2, 43)
        guard = sregs.get(12, 50) / 50.0
        held, held_at, last_tx = bytearray(), 0.0, time.monotonic()
        escaped, backlog = False, bytearray()
        reason, remote_ended, result_text = None, False, "NO CARRIER"

        def send_data(data):
            for i in range(0, len(data), cl.DATA_CHUNK):
                sock.sendall(cl.frame(cl.DATA, bytes(data[i:i + cl.DATA_CHUNK])))
            self.call["tx_bytes"] += len(data)
        try:
            sock.setblocking(True)
            while reason is None:
                if self.stopping:
                    reason = "service stopping"
                    break
                if self.pc_dtr_dropped() and self.parser.amp_d:
                    reason = "PC dropped DTR"
                    break
                now = time.monotonic()
                if held and now - held_at >= guard:
                    if len(held) == 3 and esc <= 127:
                        escaped, held = True, bytearray()
                        self.parser.call_held = True
                        self.parser.reset_line()
                        self.write(self.parser.result("OK"))
                        self.set_state(st.CONNECTED, f"{connect} · {detail} · command mode")
                    else:
                        send_data(held)
                        held = bytearray()
                rr, _, _ = select.select([self.fd, sock], [], [], 0.05)
                if self.fd in rr:
                    data = self.read()
                    now = time.monotonic()
                    if escaped:
                        out, action = self.parser.feed(data)
                        self.write(out)
                        if isinstance(action, Online):
                            escaped = False
                            self.parser.call_held = False
                            self.write(self.parser.result(connect))
                            if backlog:
                                self.write(bytes(backlog))
                                backlog = bytearray()
                            self.set_state(st.CONNECTED, f"{connect} · {detail}")
                        elif isinstance(action, Hangup):
                            reason, result_text = "ATH", "OK"
                        elif isinstance(action, (Dial, Answer)):
                            self.write(self.parser.result("ERROR"))
                    elif esc <= 127 and all(b == esc for b in data) and len(held) + len(data) <= 3 and \
                            (held or now - last_tx >= guard):
                        if not held:
                            held_at = now
                        held += data
                        held_at = now
                    else:
                        if held:
                            data = bytes(held) + data
                            held = bytearray()
                        send_data(data)
                        last_tx = now
                if sock in rr:
                    chunk = sock.recv(8192)
                    if not chunk:
                        reason, remote_ended = "connection to the LAN service closed", True
                        break
                    for k, payload in reader.feed(chunk):
                        if k == cl.DATA:
                            self.call["rx_bytes"] += len(payload)
                            if escaped:
                                if len(backlog) < 65536:
                                    backlog += payload
                            else:
                                self.write(payload)
                        elif k == cl.HANGUP:
                            reason, remote_ended = "other side hung up", True
                            break
                if time.monotonic() - self.last_status >= CALL_STATUS_S:
                    self.write_status()
        except (OSError, cl.FrameError) as e:
            if isinstance(e, DeviceGone):
                raise
            reason, remote_ended = f"call link: {e}", True
        finally:
            if not remote_ended:
                try:
                    sock.sendall(cl.frame(cl.HANGUP, '{"reason":"hangup"}'))
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass
            log.info("modem call ended: %s", reason)
            c = self.call or {}
            if call_id is not None:
                self.db("update_call", call_id, ended=time.time(), rx_bytes=c.get("rx_bytes", 0),
                        tx_bytes=c.get("tx_bytes", 0), result="ok", detail=f"{detail} · {reason}")
            self.call = None
        self.after_call(result_text)

    # data mode
    def data_mode(self, connect):
        """Hand the open tty to pppd until the call ends. Returns the end reason."""
        cmd = [str(c) for c in self.cfg["ppp"]["command"]] + [str(self.baud)]
        started = time.time()
        iface = f"ppp{self.line}"
        self.call = {"connect": connect, "started": started, "rx_bytes": 0, "tx_bytes": 0, "user": None,
                     "ip": None, "iface": iface}
        call_id = self.db("start_call", self.line, self.mode, connect, started)
        self.set_state(st.CONNECTED, connect)
        ppp_info = st.ppp_file(self.run_dir, self.line) if self.run_dir else None
        if ppp_info:
            try:
                os.unlink(ppp_info)      # left over from a pppd that died without ip-down
            except OSError:
                pass
        flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        try:
            self.child = subprocess.Popen(cmd, stdin=self.fd, stdout=self.fd, close_fds=True)
        except OSError as e:
            log.error("cannot start pppd (%s): %s", " ".join(cmd), e)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags)
            self.end_call(call_id, "error", f"cannot start pppd: {e}")
            return "pppd failed"
        log.info("pppd started (pid %d)", self.child.pid)
        reason, rc = None, None
        while reason is None:
            rc = self.child.poll()
            if rc is not None:
                reason = f"pppd exited with status {rc}"
            elif self.stopping:
                reason = "service stopping"
            elif not self.physical and self.pc_dtr_dropped() and self.parser.amp_d:
                reason = "PC dropped DTR"
            elif self.physical and self.carrier_lost():
                reason = "modem dropped carrier"
            else:
                if time.monotonic() - self.last_status >= CALL_STATUS_S:
                    self.update_call(call_id, ppp_info)
                time.sleep(0.1)
        self.update_call(call_id, ppp_info)
        if self.child.poll() is None:
            self.stop_child()
            rc = self.child.returncode
        self.child = None
        log.info("call ended: %s", reason)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, flags)
        result = PPPD_RESULTS.get(rc, "error") if reason.startswith("pppd exited") else "ok"
        self.end_call(call_id, result, reason)
        self.last_call_id, self.last_reason = call_id, reason
        return reason

    def update_call(self, call_id, ppp_info):
        """Refresh traffic counters and the PPP login (from the ip-up hook) for the GUI."""
        c = self.call
        counters = st.iface_counters(c["iface"])
        if counters:
            # rx/tx from the dial-up client's point of view: what the Pi sends is the client's download
            pi_rx, pi_tx = counters
            if pi_tx >= c["rx_bytes"] and pi_rx >= c["tx_bytes"]:
                c["rx_bytes"], c["tx_bytes"] = pi_tx, pi_rx
        if ppp_info and c["user"] is None:
            info = st.read(ppp_info)
            if info and info.get("iface") == c["iface"]:
                c["user"], c["ip"] = info.get("user") or None, info.get("ip")
                log.info("PPP up: user %s, ip %s", c["user"], c["ip"])
                if call_id is not None:
                    self.db("update_call", call_id, user=c["user"], ip=c["ip"])
                if c["user"]:
                    self.db("touch_login", c["user"])
        self.write_status()

    def end_call(self, call_id, result, detail):
        c = self.call or {}
        if call_id is not None:
            self.db("update_call", call_id, ended=time.time(), rx_bytes=c.get("rx_bytes", 0),
                    tx_bytes=c.get("tx_bytes", 0), result=result, detail=detail)
        self.call = None

    def carrier_lost(self):
        """Physical mode: True once the modem's DCD has been low for DCD_DEBOUNCE_S after having been high."""
        if not self.lines_ok or not self.cfg["physical"]["dcd_hangup"]:
            return False
        bits = self.modem_bits()
        if bits & termios.TIOCM_CAR:
            if not self.dcd_armed:
                log.info("carrier detect (DCD) up")
            self.dcd_armed = True
            self.dcd_low_since = None
            return False
        if not self.dcd_armed:
            return False
        now = time.monotonic()
        if self.dcd_low_since is None:
            self.dcd_low_since = now
        if now - self.dcd_low_since >= DCD_DEBOUNCE_S:
            names = [(termios.TIOCM_DSR, "DSR"), (termios.TIOCM_CTS, "CTS"), (termios.TIOCM_RNG, "RI")]
            log.info("carrier detect (DCD) down; other lines: %s",
                     " ".join(n for b, n in names if bits & b) or "none")
            return True
        return False

    def stop_child(self):
        self.child.terminate()
        try:
            self.child.wait(timeout=float(self.cfg["ppp"]["stop_timeout_s"]))
        except subprocess.TimeoutExpired:
            log.warning("pppd did not stop, killing it")
            self.child.kill()
            self.child.wait()

    def after_call(self, result="NO CARRIER"):
        try:
            self.set_termios()
            termios.tcflush(self.fd, termios.TCIFLUSH)
            self.set_carrier(False)
            self.parser.reset_line()
            self.parser.call_held = False
            self.write(self.parser.result(result))
        except termios.error as e:
            raise DeviceGone(e) from e
        self.set_state(st.IDLE, "Waiting for ATD")
        log.info("back in command mode")

    # physical modem mode: the Pi is the DTE of a real modem that answers incoming calls
    def physical_loop(self):
        p = self.cfg["physical"]
        while not self.stopping:
            if not self.modem_init():
                self.set_state(st.MODEM_ERROR, "Modem not responding to AT")
                log.warning("modem not responding; retrying in %.0f s", MODEM_RETRY_S)
                self.pause(MODEM_RETRY_S)
                continue
            self.set_state(st.IDLE, self.idle_text())
            log.info("modem ready, waiting for RING")
            rings, last_ring, last_health = 0, 0.0, time.monotonic()
            while not self.stopping:
                self.heartbeat()
                line = self.modem_line(0.2)
                now = time.monotonic()
                if line is None:
                    if rings and now - last_ring > RING_RESET_S:
                        log.info("caller gave up after %d ring(s)", rings)
                        rings = 0
                        self.set_state(st.IDLE, self.idle_text())
                    if not rings and now - last_health > float(p["health_interval_s"]):
                        last_health = now
                        if not self.modem_command("AT", 2.0):
                            log.warning("modem stopped answering AT; re-initialising")
                            break
                    continue
                if line == "RING" or line.startswith("RING "):
                    rings += 1
                    last_ring = now
                    self.set_state(st.RINGING, f"RING {rings}")
                    log.info("RING %d", rings)
                    if rings >= int(p["answer_rings"]):
                        self.answer()
                        break
                elif line.startswith("CONNECT"):
                    log.info("modem answered by itself: %s", line)   # auto-answer (S0>0) in the init string
                    self.physical_call(line)
                    break
                else:
                    log.debug("modem: %s", line)

    def idle_text(self):
        return f"{self.modem_model} · waiting for a call" if self.modem_model else "Waiting for a call"

    def modem_query(self, cmd, timeout=3.0):
        """Information lines of a command (e.g. ATI3) up to OK, or None on ERROR/timeout."""
        self.modem_send(cmd)
        out = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.modem_line(deadline - time.monotonic())
            if line is None or line in ("ERROR", "4"):
                return None
            if line in ("OK", "0"):
                return out
            out.append(line)
        return None

    def disconnect_reason(self):
        """Ask the modem why the last call ended (USRobotics ATI6). Other modems just answer ERROR."""
        lines = self.modem_query("ATI6", 4.0) or []
        info = {}
        for line in lines:
            if line.startswith("Disconnect Reason is"):
                info["reason"] = line[len("Disconnect Reason is"):].strip()
            for key, label in (("chars", "Chars sent"), ("blocks", "Blocks resent"), ("retrains", "Retrains Requested")):
                if line.startswith(label):
                    info[key] = " ".join(line.split())
        return info

    def pause(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            self.heartbeat()
            self.modem_line(0.2)

    def modem_line(self, timeout):
        """Next non-empty, non-echo line from the modem, or None after timeout."""
        deadline = time.monotonic() + timeout
        while True:
            ends = [i for i in (self.modem_buf.find(b"\n"), self.modem_buf.find(b"\r")) if i >= 0]
            if ends:
                cut = min(ends)
                raw, self.modem_buf = self.modem_buf[:cut], self.modem_buf[cut + 1:]
                text = raw.decode("ascii", "replace").strip()
                if self.cfg["log"]["raw_command_bytes"] and raw:
                    log.info("MODEM> %s", text)
                if text and not text.upper().startswith("AT"):
                    return text
                continue
            left = deadline - time.monotonic()
            if left <= 0 or self.stopping:
                return None
            r, _, _ = select.select([self.fd], [], [], min(left, 0.2))
            if r:
                self.modem_buf += self.read()
                if len(self.modem_buf) > 4096:
                    self.modem_buf = self.modem_buf[-1024:]

    def modem_send(self, text):
        if self.cfg["log"]["raw_command_bytes"]:
            log.info("MODEM< %s", text)
        self.write(text.encode("ascii") + b"\r")

    def modem_command(self, cmd, timeout=3.0):
        """Send a command and wait for OK (True) or an error result (False)."""
        self.modem_send(cmd)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.modem_line(deadline - time.monotonic())
            if line is None:
                break
            if line == "OK" or line == "0":
                return True
            if line in ("ERROR", "4"):
                log.warning("modem rejected %r", cmd)
                return False
        return False

    def modem_init(self):
        self.set_state(st.INITIALIZING, self.cfg["physical"]["init_string"])
        self.modem_buf = b""
        termios.tcflush(self.fd, termios.TCIFLUSH)
        self.write(b"\r")
        time.sleep(0.2)
        for _ in range(3):
            if self.stopping:
                return False
            if self.modem_command("AT", 1.5):
                break
        else:
            return False
        if not self.modem_command(self.cfg["physical"]["init_string"], 5.0):
            log.warning("modem rejected the init string %r", self.cfg["physical"]["init_string"])
            return False
        if self.modem_model is None:
            model = self.modem_query("ATI3", 3.0)
            self.modem_model = model[0][:60] if model else ""
            if self.modem_model:
                log.info("modem: %s", self.modem_model)
            if "robotics" in self.modem_model.lower() and \
                    not self.cfg["physical"]["init_string"].upper().startswith(("AT&F1", "AT&F2")):
                log.warning("USRobotics modem: use AT&F1 in the init string (fixed serial speed &B1 and RTS/CTS); "
                            "AT&F loads variable DTE speed, so PPP fails right after CONNECT")
        if self.last_call_id is not None:
            info = self.disconnect_reason()
            if info:
                log.info("modem: last call %s", ", ".join(info.values()))
                if info.get("reason"):
                    self.db("update_call", self.last_call_id,
                            detail=f"{self.last_reason} · modem: {info['reason']}")
            self.last_call_id = None
        return True

    def answer(self):
        timeout = float(self.cfg["physical"]["answer_timeout_s"])
        self.set_state(st.ANSWERING, "ATA")
        log.info("answering (ATA)")
        self.modem_send("ATA")
        deadline = time.monotonic() + timeout
        while not self.stopping and time.monotonic() < deadline:
            self.heartbeat()
            line = self.modem_line(min(1.0, max(0.05, deadline - time.monotonic())))
            if line is None:
                continue
            if line.startswith("CONNECT"):
                self.physical_call(line)
                return
            if line in ("NO CARRIER", "BUSY", "NO ANSWER", "ERROR", "NO DIALTONE", "NO DIAL TONE"):
                log.info("answer failed: %s", line)
                self.record_failed(line)
                self.hangup_modem()
                return
        log.info("no CONNECT within %.0f s", timeout)
        self.record_failed("no CONNECT after ATA", "timeout")
        self.hangup_modem()

    def record_failed(self, detail, result="no_carrier"):
        call_id = self.db("start_call", self.line, self.mode, None, time.time(), result)
        if call_id is not None:
            self.db("update_call", call_id, ended=time.time(), detail=detail)
        self.last_call_id, self.last_reason = call_id, detail

    def physical_call(self, connect):
        log.info("%s, handing line to pppd", connect)
        self.dcd_armed = False
        self.dcd_low_since = None
        self.data_mode(connect)
        self.hangup_modem()

    def hangup_modem(self):
        """DTR drop hangs up with &D2; '+++' and ATH as a fallback, then the loop re-initialises."""
        self.set_state(st.INITIALIZING, "Hanging up")
        try:
            self.set_termios()
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except termios.error as e:
            raise DeviceGone(e) from e
        if self.lines_ok:
            self.set_line(termios.TIOCM_DTR, False)
            time.sleep(1.5)
            self.set_line(termios.TIOCM_DTR, True)
            time.sleep(0.5)
        self.modem_buf = b""
        if not self.modem_command("AT", 1.5):
            time.sleep(1.2)
            self.write(b"+++")
            time.sleep(1.2)
            self.modem_command("ATH0", 3.0)
        log.info("modem on hook")
