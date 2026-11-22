"""Hayes AT command parser and modem state. Pure logic: bytes in, reply bytes and actions out."""
from dataclasses import dataclass, field

RESULT_CODES = {"OK": 0, "CONNECT": 1, "RING": 2, "NO CARRIER": 3, "ERROR": 4,
                "NO DIALTONE": 6, "BUSY": 7, "NO ANSWER": 8}
DEFAULT_SREGS = {0: 0, 1: 0, 2: 43, 3: 13, 4: 10, 5: 8, 7: 50, 8: 2, 10: 14, 12: 50}
MAX_LINE = 255

# Frozen: Windows derives the modem's detection ID (UNIMODEMxxxxxxxx) from these exact replies.
ATI_TEXT = {
    0: "5601",
    1: "000",
    2: "OK",
    3: "{model} V1.0",
    4: "{model}\r\n115200 BPS\r\nDATA/FAX\r\nV90",
    5: "{model} NVRAM PROFILE",
    6: "{model} LINK DIAGNOSTICS\r\nNO CURRENT CALL",
    7: "Configuration Profile\r\nProduct type: External\r\nProduct ID: 5601\r\nOptions: V.90 Fax",
}


@dataclass
class Dial:
    number: str


@dataclass
class Answer:
    """ATA: answer a ringing call (the session sends NO CARRIER when nothing rings)."""


@dataclass
class Online:
    """ATO: back to the held call."""


@dataclass
class Hangup:
    """ATH while a call is held in online command mode."""


@dataclass
class Profile:
    echo: bool = True
    quiet: bool = False
    verbose: bool = True
    x: int = 4
    amp_c: int = 1
    amp_d: int = 2
    amp_k: int = 3
    sregs: dict = field(default_factory=lambda: dict(DEFAULT_SREGS))

    def copy(self):
        return Profile(self.echo, self.quiet, self.verbose, self.x, self.amp_c, self.amp_d, self.amp_k,
                       dict(self.sregs))


class _Abort(Exception):
    """Stop processing the command line and send this result."""


class _Act(Exception):
    """Stop processing the command line and hand this action to the session."""


class ATParser:
    def __init__(self, model="USRobotics 56K FAX EXT", connect_string="CONNECT 56000"):
        self.model = model
        self.connect_string = connect_string
        self.stored = Profile()
        self.p = self.stored.copy()
        self.buf = bytearray()
        self.overflow = False
        self.last_line = b""
        self.last_number = ""
        self.out = bytearray()
        self.commands = []  # sanitized command lines executed by the last feed(), for logging
        self.call_held = False  # a call is up and the line is in online command mode (+++): ATO and ATH act on it

    # public API
    def feed(self, data):
        """Process received bytes. Returns (reply_bytes, Dial or None). Bytes after a dial are not consumed."""
        self.out = bytearray()
        self.commands = []
        action = None
        for b in data:
            if self.p.echo:
                self.out.append(b)
            if b == self.p.sregs[3]:
                line, self.buf = bytes(self.buf), bytearray()
                if self.overflow:
                    self.overflow = False
                    self._result("ERROR")
                    continue
                action = self._line(line)
            elif b in (self.p.sregs[5], 0x7F):
                del self.buf[-1:]
            elif b in (self.p.sregs[4], 0x0A):
                continue
            elif b == ord("/") and self.buf[-1:].upper() == b"A":
                self.buf.clear()
                action = self._line(self.last_line)
            else:
                self.buf.append(b)
                if len(self.buf) > MAX_LINE:
                    self.buf.clear()
                    self.overflow = True
            if action:
                break
        return bytes(self.out), action

    def result(self, text):
        """Formatted result code (honours Q/V), e.g. for CONNECT and NO CARRIER sent by the session."""
        self.out = bytearray()
        self._result(text)
        return bytes(self.out)

    def connect_text(self):
        return "CONNECT" if self.p.x == 0 else self.connect_string

    def reset_line(self):
        self.buf.clear()
        self.overflow = False

    @property
    def amp_d(self):
        return self.p.amp_d

    # line handling
    def _line(self, line):
        i = line.upper().find(b"AT")
        if i < 0:
            return None  # line noise / leftover PPP frames: ignore silently
        self.last_line = line[i:]
        cmd = line[i + 2:].decode("latin-1")
        self.commands.append("AT" + cmd)
        try:
            return self._execute(cmd)
        except _Abort as e:
            self._result(e.args[0])
            return None
        except _Act as e:
            return e.args[0]

    @staticmethod
    def _number(u, i):
        j = i
        while j < len(u) and u[j].isdigit():
            j += 1
        return j, (int(u[i:j]) if j > i else None)

    def _execute(self, cmd):
        u = cmd.upper()
        i, n = 0, len(u)
        while i < n:
            c = u[i]
            if c == " ":
                i += 1
            elif c == "D":
                return self._dial(cmd[i + 1:])
            elif c == "Z":
                self.p = self.stored.copy()
                self._result("OK")
                return None
            elif c in "+#":
                self._extended(u[i:])
                return None
            elif c in "&\\%":
                letter = u[i + 1:i + 2]
                if not letter.isalpha():
                    raise _Abort("ERROR")
                i, num = self._number(u, i + 2)
                self._ampersand(c + letter, num)
            elif c == "S":
                i = self._sreg(u, i + 1)
            elif c.isalpha():
                i, num = self._number(u, i + 1)
                self._basic(c, num)
            else:
                raise _Abort("ERROR")
        self._result("OK")
        return None

    def _basic(self, c, num):
        num = num or 0
        if c == "E":
            self.p.echo = bool(num)
        elif c == "Q":
            self.p.quiet = num == 1
        elif c == "V":
            self.p.verbose = bool(num)
        elif c == "X":
            if num > 7:
                raise _Abort("ERROR")
            self.p.x = num
        elif c == "I":
            if num not in ATI_TEXT:
                raise _Abort("ERROR")
            self._info(ATI_TEXT[num].format(model=self.model))
        elif c == "A":
            raise _Act(Answer())
        elif c == "O":
            if self.call_held:
                raise _Act(Online())
            raise _Abort("NO CARRIER")
        elif c == "H" and self.call_held and not num:
            raise _Act(Hangup())
        # H, L, M, W, B, N, P, T, ... are accepted as no-ops

    def _ampersand(self, code, num):
        num = num or 0
        if code == "&F":
            self.p = Profile()
        elif code == "&W":
            self.stored = self.p.copy()
        elif code == "&V":
            self._info(self._profile_text())
        elif code == "&C":
            self.p.amp_c = num
        elif code == "&D":
            self.p.amp_d = min(num, 2)
        elif code == "&K":
            self.p.amp_k = num

    def _sreg(self, u, i):
        i, reg = self._number(u, i)
        if reg is None or reg > 255:
            raise _Abort("ERROR")
        if i < len(u) and u[i] == "?":
            self._info(f"{self.p.sregs.get(reg, 0):03d}")
            return i + 1
        if i < len(u) and u[i] == "=":
            i, val = self._number(u, i + 1)
            if val is None or val > 255:
                raise _Abort("ERROR")
            self.p.sregs[reg] = val
        return i

    def _extended(self, s):
        if s.startswith("+FCLASS=?"):
            self._info("0")
        elif s.startswith("+GMI"):
            self._info("USRobotics")
        elif s.startswith("+GMM"):
            self._info(self.model)
        elif s.startswith("+GMR"):
            self._info("V1.0")
        elif "?" in s or (s.startswith("+FCLASS=") and not s.startswith("+FCLASS=0")):
            raise _Abort("ERROR")
        # other extended setters (+MS=, +ES=, +IFC=, ...) are accepted so init strings never fail
        self._result("OK")

    def _dial(self, rest):
        # never log the dialled number itself
        self.commands[-1] = self.commands[-1][:len(self.commands[-1]) - len(rest)] + \
            f"<{sum(ch.isdigit() for ch in rest)} digits>"
        number = rest.strip()
        mod = number[:1].upper()
        if mod in ("T", "P"):
            number = number[1:].strip()
        elif mod == "L" or mod == "S":
            number = self.last_number
        if number.endswith(";"):
            self._result("OK")
            return None
        self.last_number = number
        return Dial(number)

    # output
    def _crlf(self):
        return chr(self.p.sregs[3]) + chr(self.p.sregs[4])

    def _info(self, text):
        nl = self._crlf()
        self.out += f"{nl}{text.replace(chr(13) + chr(10), nl)}{nl}".encode("latin-1")

    def _result(self, text):
        if self.p.quiet:
            return
        if self.p.verbose:
            nl = self._crlf()
            self.out += f"{nl}{text}{nl}".encode("latin-1")
        else:
            code = RESULT_CODES["CONNECT" if text.startswith("CONNECT") else text]
            self.out += f"{code}{chr(self.p.sregs[3])}".encode("latin-1")

    def _profile_text(self):
        p = self.p
        regs = " ".join(f"S{k:02d}:{v:03d}" for k, v in sorted(p.sregs.items()))
        return (f"ACTIVE PROFILE:\r\nE{int(p.echo)} Q{int(p.quiet)} V{int(p.verbose)} X{p.x} "
                f"&C{p.amp_c} &D{p.amp_d} &K{p.amp_k}\r\n{regs}")
