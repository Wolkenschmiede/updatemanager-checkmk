"""Gemeinsame Helfer: Pfade, Farben, Logging, Prozesse, Zustand, Versionen, Zeitfenster."""
import datetime as _dt
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

# ---------------------------------------------------------------- Pfade
HOME = Path(os.environ.get("UMC_HOME", "/opt/updatemanager-checkmk"))
CONF_FILE = HOME / "conf" / "config.json"
LOG_DIR = HOME / "logs"
STATE_DIR = HOME / "state"
DOCS_DIR = HOME / "docs"
PREP_DIR = HOME / "vorbereitung"
SECRETS_DIR = Path(os.environ.get("UMC_SECRETS", "~/.config/updatemanager-checkmk")).expanduser()


def expand(p):
    return str(Path(os.path.expandvars(str(p))).expanduser())


# ---------------------------------------------------------------- Farben
COLOR = (sys.stdout.isatty() or bool(os.environ.get("UMC_FORCE_COLOR"))) and "NO_COLOR" not in os.environ
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if COLOR else str(s)


def bold(s): return _c("1", s)
def red(s): return _c("31", s)
def green(s): return _c("32", s)
def yellow(s): return _c("33", s)
def cyan(s): return _c("36", s)
def dim(s): return _c("2", s)
def strip_ansi(s): return _ANSI.sub("", str(s))


# ---------------------------------------------------------------- Zeit
def now():
    return _dt.datetime.now().astimezone()


def stamp():
    return now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------- Logging
class Log:
    """Ausgabe auf dem Bildschirm und (optional) in logs/<art>-<zeit>.log."""

    def __init__(self, kind=None, echo=True):
        self.echo = echo
        self.lock = threading.Lock()
        self.path = None
        self.fh = None
        self.errors = 0
        if kind:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self.path = LOG_DIR / f"{kind}-{stamp()}.log"
            self.fh = open(self.path, "a", encoding="utf-8")

    def _write(self, msg, color=None):
        ts = now().strftime("%H:%M:%S")
        with self.lock:
            if self.echo:
                print(color(msg) if color else msg, flush=True)
            if self.fh:
                self.fh.write(f"{ts} {strip_ansi(msg)}\n")
                self.fh.flush()

    def info(self, msg): self._write(msg)
    def ok(self, msg): self._write(msg, green)
    def warn(self, msg): self._write(msg, yellow)

    def err(self, msg):
        self.errors += 1
        self._write(msg, red)

    def step(self, msg): self._write(f"=== {msg} ===", lambda s: bold(cyan(s)))

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None


# ---------------------------------------------------------------- Prozesse
def run(args, log=None, *, mutating=True, dry_run=False, quiet=False, prefix="",
        timeout=None, interactive=False, on_line=None, cwd=None):
    """Fuehrt einen Befehl aus. Liefert (returncode, ausgabe).

    mutating=True + dry_run  -> Befehl wird nur angezeigt.
    interactive=True         -> Ein-/Ausgabe direkt am Terminal (z. B. sudo-Passwort).
    """
    if dry_run and mutating:
        if log:
            log.info(dim(f"[DRY-RUN] {prefix}{' '.join(shlex.quote(a) for a in args)}"))
        return 0, ""
    if interactive:
        try:
            return subprocess.call(args), ""
        except FileNotFoundError:
            return 127, f"{args[0]}: Programm nicht gefunden"
    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, text=True, errors="replace", cwd=cwd)
    except FileNotFoundError:
        msg = f"{args[0]}: Programm nicht gefunden"
        if log and not quiet:
            log.err(msg)
        return 127, msg
    timer = threading.Timer(timeout, p.kill) if timeout else None
    if timer:
        timer.start()
    out = []
    for line in p.stdout:
        out.append(line)
        if on_line:
            on_line(line)
        if log and not quiet:
            log.info(f"{prefix}{line.rstrip()}")
    rc = p.wait()
    if timer:
        timer.cancel()
    return rc, "".join(out)


def sudo_write(path, text):
    """Schreibt eine Datei mit Root-Rechten (sudo tee)."""
    r = subprocess.run(["sudo", "tee", str(path)], input=text, text=True,
                       stdout=subprocess.DEVNULL)
    return r.returncode


# ---------------------------------------------------------------- Zustand
STATE_FILE = STATE_DIR / "state.json"
_STATE_LOCK = threading.Lock()


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def update_state(fn):
    with _STATE_LOCK:
        st = load_state()
        fn(st)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False))
        os.replace(tmp, STATE_FILE)


def record(host, kind, ok, msg, **extra):
    """Merkt sich das Ergebnis eines Laufs pro Host (fuer die Status-Uebersicht)."""
    entry = {"time": now().isoformat(timespec="seconds"), "ok": ok, "msg": msg, **extra}
    update_state(lambda st: st.setdefault("hosts", {}).setdefault(host, {}).update({kind: entry}))


@contextmanager
def run_lock(name="run", wait=0, log=None):
    """Verhindert, dass zwei Update-/Sync-Laeufe gleichzeitig laufen.
    wait > 0: bis zu so viele Sekunden warten, bis der andere Lauf fertig ist (Zeitplan)."""
    import time as _t
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(STATE_DIR / f"{name}.lock", "w")
    deadline, told = _t.time() + wait, False
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if _t.time() >= deadline:
                fh.close()
                raise RuntimeError("Es laeuft bereits ein anderer Update- oder Sync-Vorgang.")
            if log and not told:
                log.info("Ein anderer Lauf (z. B. Repo-Sync) ist noch aktiv – warte, bis er fertig ist ...")
                told = True
            _t.sleep(15)
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# ---------------------------------------------------------------- Versionen
_VRE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:p(\d+))?")


def vparse(s):
    m = _VRE.match(s or "")
    return (int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0)) if m else None


def vbase(s):
    m = _VRE.match(s or "")
    return m.group(0) if m else None


def vfmt(t):
    return f"{t[0]}.{t[1]}.{t[2]}" + (f"p{t[3]}" if t[3] else "")


def same_branch(a, b):
    pa, pb = vparse(a), vparse(b)
    return bool(pa and pb and pa[:3] == pb[:3])


# ---------------------------------------------------------------- Wartungsfenster
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_ALIASES = {
    "mo": "Mon", "mon": "Mon", "montag": "Mon", "monday": "Mon",
    "di": "Tue", "tue": "Tue", "dienstag": "Tue", "tuesday": "Tue",
    "mi": "Wed", "wed": "Wed", "mittwoch": "Wed", "wednesday": "Wed",
    "do": "Thu", "thu": "Thu", "donnerstag": "Thu", "thursday": "Thu",
    "fr": "Fri", "fri": "Fri", "freitag": "Fri", "friday": "Fri",
    "sa": "Sat", "sat": "Sat", "samstag": "Sat", "saturday": "Sat",
    "so": "Sun", "sun": "Sun", "sonntag": "Sun", "sunday": "Sun",
}
DAY_DE = {"Mon": "Mo", "Tue": "Di", "Wed": "Mi", "Thu": "Do", "Fri": "Fr", "Sat": "Sa", "Sun": "So"}


def parse_weekdays(s):
    days = []
    for part in re.split(r"[,\s]+", str(s).strip()):
        if not part:
            continue
        d = _DAY_ALIASES.get(part.lower())
        if not d:
            raise ValueError(f"Unbekannter Wochentag: {part}")
        if d not in days:
            days.append(d)
    if not days:
        raise ValueError("Mindestens ein Wochentag noetig")
    return sorted(days, key=WEEKDAYS.index)


def weekdays_de(s):
    try:
        return ",".join(DAY_DE[d] for d in parse_weekdays(s))
    except ValueError:
        return str(s)


def tznow(tz):
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo(tz))
    except Exception:
        return now()


def window_info(sched, at=None):
    """(im_fenster, minuten_bis_ende) fuer den aktuellen Zeitpunkt."""
    at = at or tznow(sched.get("timezone", "Europe/Berlin"))
    days = parse_weekdays(sched["weekdays"])
    sh, sm = map(int, sched["start"].split(":"))
    eh, em = map(int, sched["end"].split(":"))
    start = at.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = at.replace(hour=eh, minute=em, second=0, microsecond=0)
    inside = WEEKDAYS[at.weekday()] in days and start <= at <= end
    return inside, (end - at).total_seconds() / 60
