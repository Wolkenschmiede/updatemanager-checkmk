"""Weboberflaeche: kleiner HTTP-Server (nur Python-Standardbibliothek) mit Login, JSON-API und Laeufen.

Alle Aktionen, die etwas veraendern (Updates, Repo, Downloads), laufen als eigener Prozess
'updatemanager-checkmk <befehl>' – also exakt wie auf der Kommandozeile bzw. im Zeitplan.
"""
import datetime as _dt
import fcntl
import getpass
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import VERSION, config, onboarding, setup
from .cmkapi import CheckmkAPI
from .common import (DOCS_DIR, LOG_DIR, SECRETS_DIR, STATE_DIR, WEEKDAYS, Log, expand, green, load_state,
                     now, parse_weekdays, red, run, sudo_write, tznow, update_state, vbase, weekdays_de, yellow)
from .config import v_host, v_name, v_site_opt, v_time, v_weekdays
from .osprofiles import EDITION_RANK, edition_change_allowed
from .repo import Repo
from .updates import Updater

STATIC = Path(__file__).resolve().parent / "web"
EXE = str(Path(__file__).resolve().parent.parent / "updatemanager-checkmk")
USERS_FILE = SECRETS_DIR / "web-users.json"
JOB_DIR = STATE_DIR / "web-jobs"
UNIT = "updatemanager-checkmk-web.service"
COOKIE = "umc_session"
PBKDF2_ROUNDS = 310_000
MAX_BODY = 1 << 20


# ================================================================ Benutzer
def _load_users():
    try:
        return json.loads(USERS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_users(users):
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(users, f, indent=2)
    os.replace(tmp, USERS_FILE)


def _hash(pw, salt=None, rounds=PBKDF2_ROUNDS):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${dk.hex()}"


def _verify(pw, stored):
    try:
        algo, rounds, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(rounds))
        return algo == "pbkdf2_sha256" and hmac.compare_digest(calc.hex(), dk)
    except (ValueError, AttributeError):
        return False


def set_password(user, pw):
    users = _load_users()
    users[user] = {"hash": _hash(pw), "changed": now().isoformat(timespec="seconds")}
    _save_users(users)


def _pw_problem(pw):
    if len(pw) < 10:
        return "Mindestens 10 Zeichen"
    return None


# ================================================================ Sitzungen / Login-Bremse
class Sessions:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}
        self.fails = {}

    def create(self, user, hours):
        tok, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.lock:
            self.items[tok] = {"user": user, "csrf": csrf, "exp": time.time() + hours * 3600}
        return tok

    def get(self, tok):
        with self.lock:
            s = self.items.get(tok or "")
            if s and s["exp"] < time.time():
                self.items.pop(tok, None)
                return None
            return s

    def drop(self, tok):
        with self.lock:
            self.items.pop(tok or "", None)

    def drop_user(self, user):
        with self.lock:
            for k in [k for k, v in self.items.items() if v["user"] == user]:
                self.items.pop(k)

    def blocked(self, ip):
        with self.lock:
            f = [t for t in self.fails.get(ip, []) if t > time.time() - 600]
            self.fails[ip] = f
            return len(f) >= 6

    def fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())


# ================================================================ Laeufe (Jobs)
_V_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:p\d+)?$")
_V_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")

# Zeitlimit je Lauf (Minuten); danach wird der Lauf beendet
JOB_TIMEOUT = {"check": 30, "inventory": 30, "notify-test": 5, "repo-sync": 480, "update-os": 720,
               "update-checkmk": 720, "maintenance": 720, "edition-switch": 720}
JOB_TIMEOUT_DEFAULT = 180

# befehl: (titel, argumente, erlaubte optionen, veraendert etwas)
JOB_TYPES = {
    "check": ("Vorpruefung", [], {"only"}, False),
    "inventory": ("Systeme erfassen", [], {"only"}, False),
    "repo-sync": ("Repo aktualisieren", [], set(), True),
    "update-os": ("OS-Update", [], {"only", "no_master", "dry_run"}, True),
    "update-checkmk": ("Checkmk-Update", [], {"only", "no_master", "dry_run"}, True),
    "maintenance": ("Wartungslauf", [], {"dry_run", "force"}, True),
    "edition-switch": ("Edition wechseln", ["edition"], {"dry_run"}, True),
    "checkmk-check": ("Checkmk-Versionen suchen", [], {"dry_run"}, True),
    "checkmk-fetch": ("Checkmk-Version laden", ["version"], {"dry_run"}, True),
    "checkmk-delete": ("Checkmk-Paket loeschen", ["version"], {"profile", "edition", "dry_run"}, True),
    "checkmk-cleanup": ("Checkmk-Pakete bereinigen", [], {"dry_run"}, True),
    "repo-cleanup": ("Alte Repo-Staende aufraeumen", [], {"dry_run"}, True),
    "repo-rollback": ("Repo-Rollback", ["profile_id", "snapshot"], {"dry_run"}, True),
    "notify-test": ("Teams-Testnachricht", [], set(), False),
    "lab-rollback": ("Labor: Testsysteme zuruecksetzen", [], {"only", "checkmk", "os", "clear_repo", "dry_run"}, True),
}


_PROBE_LOCK = threading.Lock()


def _ext_lock_held():
    """True, wenn gerade ein Lauf die Sperre haelt (Zeitplan / Terminal / Weboberflaeche)."""
    path = STATE_DIR / "run.lock"
    if not path.exists():
        return False
    # nur ein Thread prueft gleichzeitig – sonst sieht die eigene Pruefung des Nachbar-Threads wie ein Lauf aus
    with _PROBE_LOCK, open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.current = None
        self.proc = None
        JOB_DIR.mkdir(parents=True, exist_ok=True)
        self.index_file = JOB_DIR / "index.json"
        try:
            self.history = json.loads(self.index_file.read_text())
        except (OSError, json.JSONDecodeError):
            self.history = []
        stale = False
        for j in self.history:                 # Server wurde waehrend eines Laufs beendet
            if j.get("status") == "running":
                j["status"], j["rc"] = "unknown", None
                stale = True
        if stale:
            self._persist()

    def _persist(self):
        tmp = self.index_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.history[-60:], indent=1, ensure_ascii=False))
        os.replace(tmp, self.index_file)

    def build_args(self, cfg, body):
        cmd = body.get("cmd")
        if cmd not in JOB_TYPES:
            raise ValueError("Unbekannte Aktion")
        title, needs, opts, _ = JOB_TYPES[cmd]
        args = [cmd]
        for n in needs:
            v = str(body.get(n, "")).strip()
            if n == "version" and not _V_VERSION.match(v):
                raise ValueError("Version im Format 2.5.0p15 angeben")
            if n == "edition" and v not in EDITION_RANK:
                raise ValueError("Unbekannte Edition")
            if n in ("profile_id", "snapshot") and not _V_TOKEN.match(v):
                raise ValueError(f"Ungueltiger Wert fuer {n}")
            args.append(v)
        only = body.get("only") or []
        if only and "only" in opts:
            known = {c["name"] for c in cfg["customers"]} | {cfg["master"].get("name")}
            bad = [x for x in only if x not in known]
            if bad:
                raise ValueError(f"Unbekannte Systeme: {', '.join(bad)}")
            args += ["--only", ",".join(only)]
            title += " – " + ", ".join(only)
        if cmd == "lab-rollback":
            from .lab import enabled
            if not enabled(cfg):
                raise ValueError("Labormodus ist aus (Terminal: updatemanager-checkmk lab on)")
            v = str(body.get("checkmk") or "").strip()
            if v:
                if not _V_VERSION.match(v):
                    raise ValueError("Version im Format 2.5.0p12 angeben")
                args += ["--checkmk", v]
                title += f" – Checkmk {v}"
            if not v and not body.get("os") and not body.get("clear_repo"):
                raise ValueError("Bitte Checkmk-Version und/oder OS-Pakete waehlen")
        for key, flag in (("no_master", "--no-master"), ("dry_run", "--dry-run"), ("force", "--force"),
                          ("os", "--os"), ("clear_repo", "--clear-repo")):
            if body.get(key) and key in opts:
                args.append(flag)
        for key, flag in (("profile", "--profile"), ("edition", "--edition")):
            if body.get(key) and key in opts:
                v = str(body[key])
                if not _V_TOKEN.match(v):
                    raise ValueError(f"Ungueltiger Wert fuer {key}")
                args += [flag, v]
        if len(args) > 1 and needs:
            title += " " + " ".join(args[1:1 + len(needs)])
        if body.get("dry_run") and "dry_run" in opts:
            title += " (Testmodus)"
        return title, args

    def running(self):
        return bool(self.current and self.current["status"] == "running")

    def cancel(self, jid, user):
        with self.lock:
            job = self.get(jid)
            if not job or job["status"] != "running" or job is not self.current or not self.proc:
                raise RuntimeError("Dieser Lauf laeuft nicht (mehr).")
            job["cancelled_by"] = user
            proc = self.proc
        threading.Thread(target=self._kill, args=(proc,), daemon=True).start()
        return job

    @staticmethod
    def _kill(p):
        import signal
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def start(self, cfg, body, user):
        title, args = self.build_args(cfg, body)
        with self.lock:
            if self.running():
                raise RuntimeError("Es laeuft bereits eine Aktion – bitte warten, bis sie fertig ist.")
            if JOB_TYPES[args[0]][3] and _ext_lock_held():
                raise RuntimeError("Gerade laeuft ein Vorgang aus dem Zeitplan oder im Terminal.")
            jid = f"{now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
            job = {"id": jid, "cmd": args[0], "title": title, "args": args[1:], "user": user,
                   "start": now().isoformat(timespec="seconds"), "end": None, "rc": None, "status": "running"}
            self.current = job
            self.history.append(job)
            self._persist()
        path = JOB_DIR / f"{jid}.log"
        env = dict(os.environ, UMC_FORCE_COLOR="1", PYTHONUNBUFFERED="1", COLUMNS="140", UMC_MANUAL="1")
        env.pop("NO_COLOR", None)

        limit = JOB_TIMEOUT.get(args[0], JOB_TIMEOUT_DEFAULT) * 60

        def worker():
            rc, status = None, "failed"
            try:
                # Ausgabe direkt in die Datei: kein Pipe, das an Hintergrundprozessen haengen bleiben kann
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(f"$ updatemanager-checkmk {' '.join(args)}\n")
                    fh.flush()
                    p = subprocess.Popen([sys.executable, EXE, *args], stdout=fh, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, env=env, start_new_session=True)
                self.proc = p
                job["pid"] = p.pid
                try:
                    rc = p.wait(timeout=limit)
                    status = "ok" if rc == 0 else "failed"
                except subprocess.TimeoutExpired:
                    self._kill(p)
                    rc, status = p.returncode, "timeout"
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write(f"\nAbgebrochen: Zeitlimit von {limit // 60} Minuten ueberschritten.\n")
                if job.get("cancelled_by"):
                    status = "cancelled"
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write(f"\nAbgebrochen von {job['cancelled_by']}.\n")
            except Exception as e:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(f"\nFehler beim Start: {e}\n")
            with self.lock:
                self.proc = None
                job.update(end=now().isoformat(timespec="seconds"), rc=rc, status=status)
                self._persist()
        threading.Thread(target=worker, daemon=True).start()
        return job

    def get(self, jid):
        return next((j for j in self.history if j["id"] == jid), None)

    def output(self, jid, offset):
        path = JOB_DIR / f"{jid}.log"
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read(512 * 1024)
            return data.decode("utf-8", errors="replace"), offset + len(data)
        except OSError:
            return "", offset


# ================================================================ Daten fuer die Oberflaeche
def _repo(cfg):
    r = Repo(cfg, Log(echo=False))
    r.read_timeout = 6           # lesende aptly-Aufrufe duerfen die Oberflaeche nicht blockieren
    return r


def _next_window(sched):
    """Beginn des naechsten Wartungsfensters (oder des laufenden) als ISO-Zeit."""
    try:
        days = parse_weekdays(sched["weekdays"])
        at = tznow(sched.get("timezone", "Europe/Berlin"))
        sh, sm = map(int, sched["start"].split(":"))
        eh, em = map(int, sched["end"].split(":"))
    except (ValueError, KeyError):
        return None, False
    for add in range(0, 8):
        d = at + _dt.timedelta(days=add)
        start = d.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = d.replace(hour=eh, minute=em, second=0, microsecond=0)
        if WEEKDAYS[d.weekday()] in days and end > at:
            return start.isoformat(timespec="minutes"), start <= at <= end
    return None, False


def _timer_active(name):
    rc, out = run(["systemctl", "is-active", name], mutating=False, quiet=True, timeout=5)
    return out.strip() == "active"


def _system_view(cfg, t, role, inv, hosts, plan):
    name = t["name"]
    info = inv.get(name) or {}
    h = hosts.get(name, {})
    base = vbase(info.get("checkmk") or "") or None
    target, kind = None, None
    if name in plan.get("patches", {}):
        target, kind = plan["patches"][name], "patch"
    elif name in plan.get("catchup", {}):
        target, kind = plan["catchup"][name], "catchup"
    elif plan.get("major") and name in plan["major"]["targets"] and plan["major"]["targets"][name] != base:
        target, kind = plan["major"]["targets"][name], "major"
    blocked = [b for b in plan.get("blocked", []) if b.startswith(f"{name}:") or f"– {name}:" in b]
    pending = info.get("pending")
    last_err = next((e for e in h.values() if isinstance(e, dict) and e.get("ok") is False), None)
    if not t.get("enabled", True):
        status = "disabled"
    elif not info:
        status = "unknown"
    elif last_err:
        status = "error"
    elif target or (pending or 0) > 0:
        status = "pending"
    elif blocked:
        status = "blocked"
    else:
        status = "ok"
    prof = cfg["os_profiles"].get(info.get("profile")) if info else None
    return {
        "name": name, "role": role, "host": t.get("host", ""), "site": t.get("site", ""),
        "enabled": t.get("enabled", True), "notes": t.get("notes", ""), "ssh_port": t.get("ssh_port", 22),
        "jumphost": t.get("jumphost", ""), "checkmk_host": t.get("checkmk_host", ""),
        "status": status,
        "os": {"label": info.get("label"), "id": info.get("os_id"), "version": info.get("os_version"),
               "codename": info.get("codename"), "kernel": info.get("kernel"), "arch": info.get("arch"),
               "profile": info.get("profile"), "family": info.get("family"), "pending": pending,
               "seen": info.get("time"), "profile_label": prof.get("label") if prof else None},
        "checkmk": {"full": info.get("checkmk"), "version": base, "edition": info.get("edition"),
                    "installed": info.get("omd_versions", []), "target": target, "kind": kind},
        "blocked": blocked,
        "last": {k: v for k, v in h.items() if isinstance(v, dict)},
    }


def overview(cfg):
    st = load_state()
    inv, hosts = st.get("inventory", {}), st.get("hosts", {})
    repo = _repo(cfg)
    upd = Updater(cfg, Log(echo=False), None, None, repo)
    m = config.master_target(cfg)
    plan = {"patches": {}, "catchup": {}, "major": None, "blocked": []}
    if m.get("host"):
        try:
            p = upd.plan({k: v for k, v in inv.items()})
            plan = {k: p.get(k) for k in ("patches", "catchup", "major", "blocked", "error")}
        except Exception as e:      # Anzeige darf nie an der Planung scheitern
            plan["error"] = str(e)
    master = _system_view(cfg, m, "master", inv, hosts, plan) if m.get("host") else None
    customers = [_system_view(cfg, c, "kunde", inv, hosts, plan) for c in cfg["customers"]]
    allv = ([master] if master else []) + customers
    win, inside = _next_window(cfg["schedule"])
    chk = st.get("checkmk_check") or {}
    ts = setup.timer_state()
    return {
        "version": VERSION, "host": socket.gethostname(),
        "master": master, "customers": customers,
        "plan": plan,
        "summary": {
            "systems": len(allv),
            "unreachable": sum(1 for s in allv if s["status"] == "unknown"),
            "errors": sum(1 for s in allv if s["status"] == "error"),
            "os_pending_hosts": sum(1 for s in allv if (s["os"]["pending"] or 0) > 0),
            "os_pending_total": sum(s["os"]["pending"] or 0 for s in allv),
            "cmk_pending": sum(1 for s in allv if s["checkmk"]["target"]),
            "cmk_targets": sorted({s["checkmk"]["target"] for s in allv if s["checkmk"]["target"]}),
            "blocked": len(plan.get("blocked") or []),
        },
        "schedule": {"next_window": win, "inside": inside, "weekdays": weekdays_de(cfg["schedule"]["weekdays"]),
                     "start": cfg["schedule"]["start"], "end": cfg["schedule"]["end"],
                     "timezone": cfg["schedule"]["timezone"], "repo_sync": cfg["schedule"]["repo_sync"]},
        "checkmk": {"edition": cfg["checkmk"]["edition"], "majors": chk.get("majors", []),
                    "newest_patch": chk.get("patch"), "unsupported": chk.get("unsupported", {}),
                    "checked": chk.get("time"), "major_auto": cfg["checkmk"].get("major_auto", True)},
        "runs": {"repo_sync": st.get("repo_sync_result"), "maintenance": st.get("last_maintenance"),
                 "last_sync": st.get("last_sync")},
        "external_run": _ext_lock_held(),
        "lab": bool(cfg.get("lab", {}).get("enabled")),
        "timers": {"state": ts},
        "setup": onboarding.checklist(cfg, timer_state=ts or ""),
    }


_REPO_CACHE = {"t": 0, "data": None}


def repo_view(cfg, fresh=False, busy=False):
    if not fresh and _REPO_CACHE["data"] and time.time() - _REPO_CACHE["t"] < 15:
        return _REPO_CACHE["data"]
    repo = _repo(cfg)
    st0 = load_state()
    inv = st0.get("inventory", {})
    cached = st0.get("repo_snapshots") or {}
    live = not busy                 # waehrend ein Lauf aptly sperrt: gemerkten Stand zeigen
    profiles = []
    for p in cfg["os_profiles"].values():
        users = sorted(n for n, i in inv.items() if i.get("profile") == p["id"])
        snaps, cur = None, None
        if live:
            try:
                snaps, cur = repo.snapshots(p), repo.current_snapshot(p)
            except Exception:
                snaps = None
            if p["family"] == "apt" and not snaps and cached.get(p["id"], {}).get("snapshots"):
                live = False        # aptly antwortet nicht (gesperrt/Zeitlimit) – nicht weiter warten
                snaps = None
        if snaps is None:
            c = cached.get(p["id"], {})
            snaps, cur = c.get("snapshots", []), c.get("current")
        profiles.append({
            "id": p["id"], "label": p.get("label", p["id"]), "family": p["family"], "enabled": p.get("enabled", True),
            "distro": p.get("distro"), "codename": p.get("codename"), "version": p.get("version"),
            "cmk_tag": p.get("cmk_tag"), "note": p.get("note", ""), "used_by": users,
            "mirrors": p.get("mirrors", []), "repos": p.get("repos", []),
            "snapshots": snaps, "current": cur,
        })
    usage = repo.checkmk_usage()
    try:
        du = shutil.disk_usage(repo.root if repo.root.exists() else "/")
        disk = {"total": du.total, "used": du.used, "free": du.free, "path": str(repo.root)}
    except OSError:
        disk = None
    st = load_state()
    data = {"profiles": profiles, "checkmk": usage, "disk": disk,
            "snapshots_live": live, "snapshots_time": cached.get("time"),
            "checkmk_bytes": sum(u["size"] for u in usage),
            "settings": {"edition": cfg["checkmk"]["edition"], "repo_cleanup": cfg["checkmk"].get("repo_cleanup", True),
                         "filter_mode": cfg["repo"]["filter_mode"], "keep_snapshots": cfg["repo"]["keep_snapshots"],
                         "major_download": cfg["checkmk"].get("major_download", True),
                         "auto_patch": cfg["checkmk"].get("auto_patch", True)},
            "last_sync": st.get("last_sync"), "repo_sync": st.get("repo_sync_result"),
            "check": st.get("checkmk_check")}
    _REPO_CACHE.update(t=time.time(), data=data)
    return data


def _secret_exists(path):
    try:
        return Path(expand(path)).stat().st_size > 0
    except OSError:
        return False


def settings_view(cfg):
    pub = setup.pubkey(cfg) if hasattr(setup, "pubkey") else None
    return {
        "checkmk": cfg["checkmk"], "schedule": cfg["schedule"], "updates": cfg["updates"],
        "ssh": cfg["ssh"], "repo": cfg["repo"], "notify": cfg["notify"], "web": cfg["web"],
        "ntp_server": cfg.get("ntp_server", ""),
        "secrets": {"api": _secret_exists(cfg["master"]["api_secret_file"]),
                    "download": _secret_exists(cfg["checkmk"]["credentials_file"]),
                    "teams": _secret_exists(cfg["notify"]["teams_webhook_file"])},
        "pubkey": pub,
        "timers": {"state": setup.timer_state()},
        "next_runs": setup.next_runs(cfg),
        "editions": sorted(EDITION_RANK, key=lambda e: (EDITION_RANK[e], e)),
    }


# ================================================================ Einstellungen pruefen und speichern
def _v_minpatch_dict(d):
    if not isinstance(d, dict):
        return "Mindest-Patchstaende als Zuordnung Zweig -> Version"
    for k, v in d.items():
        if not re.fullmatch(r"\d+\.\d+\.\d+", k) or not _V_VERSION.match(v):
            return f"Ungueltig: {k}={v} (Beispiel 2.6.0=2.5.0p20)"
    return None


CHOICES = {
    ("checkmk", "backup"): ("full", "no-past", "none"),
    ("repo", "filter_mode"): ("packages", "full"),
    ("notify", "mode"): ("always", "problems"),
}
EXTRA = {
    ("schedule", "start"): v_time, ("schedule", "end"): v_time, ("schedule", "repo_sync"): v_time,
    ("schedule", "weekdays"): v_weekdays,
    ("checkmk", "edition"): lambda s: None if re.fullmatch(r"[a-z]+", s) else "Edition: Kleinbuchstaben",
    ("checkmk", "min_patch"): _v_minpatch_dict,
    ("ssh", "user"): v_name,
    ("repo", "listen_host"): v_host,
    ("schedule", "timezone"): lambda s: None if re.fullmatch(r"[A-Za-z_+\-/0-9]+", s) else "Zeitzone, z. B. Europe/Berlin",
}
EDITABLE = {"checkmk", "schedule", "updates", "ssh", "repo", "notify"}
READONLY = {("notify", "teams_webhook_file"), ("checkmk", "credentials_file"), ("ssh", "key"), ("repo", "root"),
            ("notify", "checkmk_localcheck")}


def apply_settings(cfg, section, data):
    if section == "ntp_server":
        v = str(data.get("value", "")).strip()
        if v and v_host(v):
            raise ValueError(v_host(v))
        cfg["ntp_server"] = v
        return []
    if section not in EDITABLE or not isinstance(data, dict):
        raise ValueError("Dieser Bereich kann hier nicht geaendert werden")
    target = cfg[section]
    defaults = config.DEFAULTS[section]
    changed = []
    for k, v in data.items():
        if k not in defaults or (section, k) in READONLY:
            continue
        ref = defaults[k]
        if isinstance(ref, bool):
            if not isinstance(v, bool):
                raise ValueError(f"{k}: ja/nein erwartet")
        elif isinstance(ref, int):
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k}: Zahl erwartet")
            if v < 0 or v > 100000:
                raise ValueError(f"{k}: Wert ausserhalb des erlaubten Bereichs")
        elif isinstance(ref, str):
            v = str(v).strip()
        if (section, k) in CHOICES and v not in CHOICES[(section, k)]:
            raise ValueError(f"{k}: erlaubt sind {', '.join(CHOICES[(section, k)])}")
        chk = EXTRA.get((section, k))
        if chk:
            err = chk(v)
            if err:
                raise ValueError(f"{k}: {err}")
        if section == "schedule" and k == "weekdays":
            v = ",".join(parse_weekdays(v))
        if section == "schedule" and k in ("start", "end", "repo_sync"):
            v = config.norm_time(v)
        if target.get(k) != v:
            changed.append(k)
        target[k] = v
    s = cfg["schedule"]
    if s["end"] <= s["start"]:
        raise ValueError("Das Wartungsfenster muss nach dem Beginn enden")
    return changed


def _system_from(data, cfg, current=None, master=False):
    name = str(data.get("name", "")).strip()
    host = str(data.get("host", "")).strip()
    site = str(data.get("site", "")).strip()
    for err in (v_name(name), v_host(host) if host else "Adresse erforderlich", v_site_opt(site)):
        if err:
            raise ValueError(err)
    if master and not site:
        raise ValueError("Site-Name des Masters erforderlich")
    others = {c["name"] for c in cfg["customers"] if c is not current}
    if not master:
        others.add(cfg["master"].get("name"))
    if name in others:
        raise ValueError("Name existiert bereits")
    try:
        port = int(data.get("ssh_port") or 22)
    except ValueError:
        raise ValueError("SSH-Port: Zahl erwartet")
    if not 1 <= port <= 65535:
        raise ValueError("SSH-Port 1–65535")
    jump = str(data.get("jumphost", "")).strip()
    if jump and not re.fullmatch(r"[A-Za-z0-9_.@:\-\[\]]+", jump):
        raise ValueError("Jumphost im Format user@host[:port]")
    out = {"name": name, "host": host, "site": site, "ssh_port": port, "jumphost": jump,
           "checkmk_host": str(data.get("checkmk_host", "")).strip()}
    if out["checkmk_host"] and not re.fullmatch(r"[A-Za-z0-9_.\-]+", out["checkmk_host"]):
        raise ValueError("Hostname in Checkmk: Buchstaben, Ziffern, _ . -")
    if master:
        url = str(data.get("api_url", "")).strip() or config.api_url_default(host, site)
        if not re.fullmatch(r"https?://[^\s]+", url):
            raise ValueError("REST-API-URL beginnt mit http:// oder https://")
        out.update(api_url=url, api_user=str(data.get("api_user") or "svc-update").strip(),
                   api_verify_tls=bool(data.get("api_verify_tls", True)))
        if v_name(out["api_user"]):
            raise ValueError("Automation-User: " + v_name(out["api_user"]))
    else:
        out["notes"] = str(data.get("notes", ""))[:500]
        out["enabled"] = bool(data.get("enabled", True))
    return out


def _v_url(u):
    return re.fullmatch(r"https?://[^\s'\"]+", u or "") is not None


def apply_profile(cfg, pid, data):
    p = cfg["os_profiles"].get(pid)
    if not p:
        raise ValueError("Unbekanntes Betriebssystem-Profil")
    if "enabled" in data:
        p["enabled"] = bool(data["enabled"])
    if "mirrors" in data and p["family"] == "apt":
        ms = []
        for m in data["mirrors"]:
            comps = [c for c in re.split(r"[,\s]+", str(m.get("components", ""))) if c] \
                if not isinstance(m.get("components"), list) else m["components"]
            if not _v_url(m.get("url")) or not _V_TOKEN.match(str(m.get("dist", ""))) or not comps:
                raise ValueError("Spiegel: Distribution, URL (http/https) und Komponenten angeben")
            ms.append({"dist": m["dist"], "url": m["url"].strip(), "components": comps})
        p["mirrors"] = ms
    if "repos" in data and p["family"] != "apt":
        rs = []
        for r in data["repos"]:
            if not _v_url(r.get("url")) or not _V_TOKEN.match(str(r.get("name", ""))):
                raise ValueError("Repo: Name und URL (http/https) angeben")
            rs.append({"name": r["name"], "url": r["url"].strip(), "enabled": bool(r.get("enabled", True))})
        p["repos"] = rs


def write_secret(path, value):
    p = Path(expand(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value.strip() + "\n")


# ================================================================ HTTP
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".woff2": "font/woff2",
        ".png": "image/png", ".ico": "image/x-icon"}
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; font-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class App:
    def __init__(self):
        self.sessions = Sessions()
        self.jobs = Jobs()
        self.cfg_lock = threading.Lock()

    def cfg(self):
        c = config.load()
        if c is None:
            raise RuntimeError("Noch nicht eingerichtet – bitte einmal 'updatemanager-checkmk' im Terminal starten.")
        return c


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "updatemanager-checkmk"
    sys_version = ""
    app: App = None
    secure = False

    def log_message(self, fmt, *a):
        if os.environ.get("UMC_WEB_DEBUG"):
            sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % a))

    # ------------------------------------------------------------ Antworten
    def _headers(self, code, ctype, length, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        if self.secure:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def json(self, data, code=200, extra=None):
        body = json.dumps(data, ensure_ascii=False, default=str).encode()
        self._headers(code, "application/json; charset=utf-8", len(body),
                      dict({"Cache-Control": "no-store"}, **(extra or {})))
        self.wfile.write(body)

    def error(self, code, msg):
        self.json({"error": msg}, code)

    def text(self, text, ctype="text/plain; charset=utf-8", extra=None):
        body = text.encode()
        self._headers(200, ctype, len(body), dict({"Cache-Control": "no-store"}, **(extra or {})))
        self.wfile.write(body)

    # ------------------------------------------------------------ Hilfen
    def _cookie(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def _session(self):
        return self.app.sessions.get(self._cookie())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("Anfrage zu gross")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ValueError("Ungueltige Anfrage")
        if not isinstance(data, dict):
            raise ValueError("Ungueltige Anfrage")
        return data

    def _ip(self):
        return self.client_address[0]

    # ------------------------------------------------------------ Verteilung
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        url = urlparse(self.path)
        path = unquote(url.path)
        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    return self.error(405, "Nicht erlaubt")
                return self._static(path)
            if path == "/api/login" and method == "POST":
                return self._login()
            sess = self._session()
            if path == "/api/session":
                if not sess:
                    return self.json({"user": None, "version": VERSION, "host": socket.gethostname()})
                return self.json({"user": sess["user"], "csrf": sess["csrf"], "version": VERSION,
                                  "host": socket.gethostname()})
            if not sess:
                return self.error(401, "Bitte anmelden")
            if method != "GET":
                tok = self.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(tok, sess["csrf"]):
                    return self.error(403, "Sitzung ungueltig – Seite neu laden")
            self.user = sess["user"]
            for m, pat, fn in ROUTES:
                if m == method:
                    mm = re.fullmatch(pat, path)
                    if mm:
                        return fn(self, *mm.groups(), query=parse_qs(url.query))
            return self.error(404, "Nicht gefunden")
        except ValueError as e:
            return self.error(400, str(e))
        except RuntimeError as e:
            return self.error(409, str(e))
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:  # pragma: no cover - Schutz gegen unerwartete Fehler
            return self.error(500, f"Interner Fehler: {e}")

    def _static(self, path):
        if path in ("/", "/index.html") or not Path(path).suffix:
            path = "/index.html"
        f = (STATIC / path.lstrip("/")).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self.error(404, "Nicht gefunden")
        st = f.stat()
        etag = f'"{int(st.st_mtime)}-{st.st_size}-{VERSION}"'
        cache = "no-cache" if f.suffix in (".html", ".js", ".css") else "max-age=604800"
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        data = f.read_bytes()
        self._headers(200, MIME.get(f.suffix, "application/octet-stream"), len(data),
                      {"Cache-Control": cache, "ETag": etag})
        self.wfile.write(data)

    def _login(self):
        ip = self._ip()
        if self.app.sessions.blocked(ip):
            return self.error(429, "Zu viele Fehlversuche – bitte 10 Minuten warten.")
        d = self._body()
        user, pw = str(d.get("user", "")), str(d.get("password", ""))
        u = _load_users().get(user)
        if not u or not _verify(pw, u["hash"]):
            self.app.sessions.fail(ip)
            time.sleep(1.0)
            return self.error(401, "Benutzername oder Passwort falsch")
        cfg = self.app.cfg()
        tok = self.app.sessions.create(user, int(cfg["web"].get("session_hours", 12)))
        flags = "HttpOnly; SameSite=Strict; Path=/" + ("; Secure" if self.secure else "")
        self.json({"ok": True}, extra={"Set-Cookie": f"{COOKIE}={tok}; {flags}"})


# ================================================================ API-Funktionen
def api_logout(h, query):
    h.app.sessions.drop(h._cookie())
    h.json({"ok": True}, extra={"Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"})


def api_password(h, query):
    d = h._body()
    users = _load_users()
    if not _verify(str(d.get("old", "")), users.get(h.user, {}).get("hash", "")):
        raise ValueError("Bisheriges Passwort stimmt nicht")
    new = str(d.get("new", ""))
    err = _pw_problem(new)
    if err:
        raise ValueError(err)
    set_password(h.user, new)
    h.json({"ok": True})


def api_overview(h, query):
    data = overview(h.app.cfg())
    data["external_run"] = data["external_run"] and not h.app.jobs.running()
    h.json(data)


def api_system(h, name, query):
    cfg = h.app.cfg()
    ov = overview(cfg)
    s = next((x for x in ([ov["master"]] if ov["master"] else []) + ov["customers"] if x["name"] == name), None)
    if not s:
        return h.error(404, "Unbekanntes System")
    if s["role"] == "master":
        m = cfg["master"]
        s["api"] = {"url": m.get("api_url"), "user": m.get("api_user"), "verify_tls": m.get("api_verify_tls", True),
                    "secret": _secret_exists(m["api_secret_file"])}
    logs = sorted(LOG_DIR.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:60]
    rel = []
    needles = (f"[{name}]".encode(), f" {name}:".encode())
    for f in logs:
        try:
            with open(f, "rb") as fh:
                head = fh.read(200_000)
            if any(n in head for n in needles):
                rel.append({"name": f.name, "mtime": f.stat().st_mtime})
        except OSError:
            pass
        if len(rel) >= 8:
            break
    s["logs"] = rel
    h.json(s)


def api_customer_create(h, query):
    d = h._body()
    with h.app.cfg_lock:
        cfg = h.app.cfg()
        c = config.new_customer()
        c.update(_system_from(d, cfg))
        cfg["customers"].append(c)
        config.save(cfg)
    h.json({"ok": True, "name": c["name"]})


def api_customer_update(h, name, query):
    d = h._body()
    with h.app.cfg_lock:
        cfg = h.app.cfg()
        c = config.find_customer(cfg, name)
        if not c:
            return h.error(404, "Unbekannter Kunde")
        c.update(_system_from(d, cfg, current=c))
        config.save(cfg)
    h.json({"ok": True, "name": c["name"]})


def api_customer_delete(h, name, query):
    with h.app.cfg_lock:
        cfg = h.app.cfg()
        c = config.find_customer(cfg, name)
        if not c:
            return h.error(404, "Unbekannter Kunde")
        cfg["customers"].remove(c)
        config.save(cfg)
    update_state(lambda st: [st.get(k, {}).pop(name, None) for k in ("inventory", "hosts")])
    h.json({"ok": True})


def api_master_update(h, query):
    d = h._body()
    with h.app.cfg_lock:
        cfg = h.app.cfg()
        cfg["master"].update(_system_from(d, cfg, master=True))
        config.save(cfg)
    h.json({"ok": True})


def api_master_test(h, query):
    cfg = h.app.cfg()
    v, err = CheckmkAPI(cfg["master"], Log(echo=False)).version()
    h.json({"ok": bool(v), "version": v, "error": err})


def api_prepare(h, name, query):
    cfg = h.app.cfg()
    t = config.find_customer(cfg, name) or (config.master_target(cfg) if cfg["master"].get("name") == name else None)
    if not t:
        return h.error(404, "Unbekanntes System")
    log = Log(echo=False)
    path = setup.prepare_script(cfg, t, log)
    if not path:
        raise ValueError("Skript konnte nicht erzeugt werden – SSH-Schluessel und Repo-Signierschluessel pruefen "
                         "(Terminal: Menue 9 -> System pruefen).")
    h.text(path.read_text(), "text/x-shellscript; charset=utf-8",
           {"Content-Disposition": f'attachment; filename="{path.name}"'})


def api_repo(h, query):
    busy = h.app.jobs.running() or _ext_lock_held()
    h.json(repo_view(h.app.cfg(), fresh="fresh" in query, busy=busy))


def api_profile_update(h, pid, query):
    d = h._body()
    with h.app.cfg_lock:
        cfg = h.app.cfg()
        apply_profile(cfg, pid, d)
        config.save(cfg)
    _REPO_CACHE["t"] = 0
    h.json({"ok": True})


def api_settings(h, query):
    h.json(settings_view(h.app.cfg()))


def api_settings_update(h, section, query):
    d = h._body()
    with h.app.cfg_lock:
        cfg = h.app.cfg()
        if section == "checkmk" and "edition" in d and d["edition"] != cfg["checkmk"]["edition"] \
                and not d.get("confirm_edition"):
            raise ValueError("Die Edition hier zu aendern stellt nur die Suche um. Fuer den Wechsel der Sites "
                             "bitte 'Edition wechseln' unter Laeufe nutzen.")
        changed = apply_settings(cfg, section, d)
        config.save(cfg)
    msg = "Gespeichert."
    if section == "schedule":
        from .scheduler import mark_past
        mark_past(cfg)
        st_ = setup.timer_state()
        msg = {"neu": "Gespeichert – gilt sofort.",
               "alt": "Gespeichert. Einmalig im Terminal umstellen: updatemanager-checkmk → 4 → 2 – danach gelten "
                      "Änderungen hier sofort.",
               None: "Gespeichert. Der Zeitplan ist noch nicht aktiv: im Terminal updatemanager-checkmk → 4 → 2."}[st_]
    _REPO_CACHE["t"] = 0
    h.json({"ok": True, "changed": changed, "message": msg})


def api_secret(h, kind, query):
    d = h._body()
    cfg = h.app.cfg()
    path = {"api": cfg["master"]["api_secret_file"], "download": cfg["checkmk"]["credentials_file"],
            "teams": cfg["notify"]["teams_webhook_file"]}.get(kind)
    if not path:
        return h.error(404, "Unbekannt")
    v = str(d.get("value", "")).strip()
    if not v or "\n" in v or len(v) > 4000:
        raise ValueError("Wert fehlt oder ist ungueltig")
    if kind == "download" and ":" not in v:
        raise ValueError("Format user:passwort")
    if kind == "teams" and not v.startswith("https://"):
        raise ValueError("Die Webhook-Adresse beginnt mit https://")
    write_secret(path, v)
    if kind == "teams":
        with h.app.cfg_lock:
            cfg = h.app.cfg()
            cfg["notify"]["teams"] = True
            config.save(cfg)
    h.json({"ok": True})


def api_jobs(h, query):
    j = h.app.jobs
    h.json({"current": j.current, "history": list(reversed(j.history[-40:])),
            "external_run": _ext_lock_held() and not j.running(),
            "types": {k: v[0] for k, v in JOB_TYPES.items()}})


def api_job_start(h, query):
    d = h._body()
    job = h.app.jobs.start(h.app.cfg(), d, h.user)
    _REPO_CACHE["t"] = 0
    h.json(job)


def api_job(h, jid, query):
    job = h.app.jobs.get(jid)
    if not job:
        return h.error(404, "Unbekannter Lauf")
    try:
        off = int((query.get("offset") or ["0"])[0])
    except ValueError:
        off = 0
    text, new = h.app.jobs.output(jid, off)
    h.json({"job": job, "text": text, "offset": new})


def api_job_cancel(h, jid, query):
    h.app.jobs.cancel(jid, h.user)
    h.json({"ok": True})


def api_lab(h, query):
    from .lab import Lab, enabled
    cfg = h.app.cfg()
    if not enabled(cfg):
        return h.json({"enabled": False})
    inv = load_state().get("inventory", {})
    cur = vbase((inv.get(cfg["master"].get("name")) or {}).get("checkmk") or "")
    sites = sorted({vbase(i.get("checkmk") or "") for i in inv.values() if i.get("checkmk")}, key=lambda v: v)
    upd = Updater(cfg, Log(echo=False), None, None, _repo(cfg))
    newest = max(sites, key=lambda v: tuple(int(x) for x in re.findall(r"\d+", v))) if sites else cur
    versions = Lab(upd).older_versions(newest) if newest else []
    h.json({"enabled": True, "master": cur, "sites": sites, "newest": newest, "versions": versions,
            "edition": cfg["checkmk"]["edition"]})


def api_logs(h, query):
    files = sorted(LOG_DIR.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:300]
    h.json([{"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime,
             "kind": re.sub(r"-\d{8}-\d{6}\.log$", "", f.name)} for f in files])


def api_log(h, name, query):
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.log", name):
        return h.error(400, "Ungueltiger Name")
    f = LOG_DIR / name
    if not f.is_file():
        return h.error(404, "Nicht gefunden")
    size = f.stat().st_size
    with open(f, "rb") as fh:
        if size > 600_000:
            fh.seek(size - 600_000)
        data = fh.read().decode("utf-8", errors="replace")
    h.json({"name": name, "size": size, "truncated": size > 600_000, "text": data})


def api_help(h, query):
    f = DOCS_DIR / "HILFE.md"
    if not f.exists():
        f = Path(__file__).resolve().parent.parent / "docs" / "HILFE.md"
    h.json({"text": f.read_text(encoding="utf-8") if f.exists() else "# Hilfe fehlt"})


def api_edition_check(h, query):
    cfg = h.app.cfg()
    new = (query.get("edition") or [""])[0]
    st = load_state().get("inventory", {})
    cur = {i.get("edition") for i in st.values() if i.get("edition")}
    res = []
    for old in sorted(cur):
        ok, why = edition_change_allowed(old, new)
        res.append({"from": old, "allowed": ok, "reason": why})
    h.json({"edition": new, "configured": cfg["checkmk"]["edition"], "checks": res})


ROUTES = [
    ("POST", r"/api/logout", api_logout),
    ("POST", r"/api/password", api_password),
    ("GET", r"/api/overview", api_overview),
    ("GET", r"/api/systems/([A-Za-z0-9_.-]+)", api_system),
    ("GET", r"/api/systems/([A-Za-z0-9_.-]+)/prepare", api_prepare),
    ("POST", r"/api/customers", api_customer_create),
    ("PUT", r"/api/customers/([A-Za-z0-9_.-]+)", api_customer_update),
    ("DELETE", r"/api/customers/([A-Za-z0-9_.-]+)", api_customer_delete),
    ("PUT", r"/api/master", api_master_update),
    ("POST", r"/api/master/test", api_master_test),
    ("GET", r"/api/repo", api_repo),
    ("PUT", r"/api/profiles/([A-Za-z0-9_.-]+)", api_profile_update),
    ("GET", r"/api/settings", api_settings),
    ("PUT", r"/api/settings/([a-z_]+)", api_settings_update),
    ("POST", r"/api/secrets/([a-z]+)", api_secret),
    ("GET", r"/api/jobs", api_jobs),
    ("POST", r"/api/jobs", api_job_start),
    ("GET", r"/api/jobs/([0-9a-f-]+)", api_job),
    ("POST", r"/api/jobs/([0-9a-f-]+)/cancel", api_job_cancel),
    ("GET", r"/api/lab", api_lab),
    ("GET", r"/api/logs", api_logs),
    ("GET", r"/api/logs/([^/]+)", api_log),
    ("GET", r"/api/help", api_help),
    ("GET", r"/api/edition-check", api_edition_check),
]


# ================================================================ Server
class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)


def _is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def url_of(cfg):
    w = cfg["web"]
    host = w["listen"]
    if host in ("0.0.0.0", "::"):
        host = socket.gethostname()
    if ":" in host:
        host = f"[{host}]"
    return f"{'https' if w.get('tls') else 'http'}://{host}:{w['port']}/"


def serve(cfg):
    w = cfg["web"]
    if not _load_users():
        print(red("Noch kein Benutzer angelegt – zuerst 'updatemanager-checkmk web passwd' ausfuehren."))
        return 2
    Handler.app = App()
    Handler.secure = bool(w.get("tls"))
    httpd = Server((w["listen"], int(w["port"])), Handler)
    if w.get("tls"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(expand(w["cert"]), expand(w["key"]))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"updatemanager-checkmk {VERSION} – Weboberflaeche auf {url_of(cfg)}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# ================================================================ Kommandozeile: web ...
def unit_text(cfg):
    user = getpass.getuser()
    return f"""[Unit]
Description=updatemanager-checkmk Weboberflaeche
After=network-online.target
Wants=network-online.target

[Service]
User={user}
Environment=UMC_HOME={os.environ.get('UMC_HOME', '/opt/updatemanager-checkmk')}
ExecStart={setup.EXE} web serve
Restart=on-failure
RestartSec=5
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
"""


def _ask_password(user):
    while True:
        pw = getpass.getpass(f"Neues Passwort fuer {user}: ")
        err = _pw_problem(pw)
        if err:
            print(red(f"  {err}"))
            continue
        if getpass.getpass("Wiederholen: ") != pw:
            print(red("  Stimmt nicht ueberein"))
            continue
        return pw


def web_on(cfg):
    if not _load_users():
        print("Fuer die Weboberflaeche wird ein Benutzer gebraucht.")
        user = input(f"Benutzername [{getpass.getuser()}]: ").strip() or getpass.getuser()
        if v_name(user):
            print(red(v_name(user)))
            return 2
        set_password(user, _ask_password(user))
    w = cfg["web"]
    if not _is_loopback(w["listen"]) and not w.get("tls"):
        print(yellow("Achtung: Die Oberflaeche ist im Netz erreichbar, aber ohne HTTPS. Passwoerter gingen im "
                     "Klartext ueber das Netz. Empfehlung: 'updatemanager-checkmk web tls'."))
    if sudo_write(f"/etc/systemd/system/{UNIT}", unit_text(cfg)):
        print(red("Dienst konnte nicht eingerichtet werden (sudo)."))
        return 1
    run(["sudo", "systemctl", "daemon-reload"], interactive=True)
    rc = run(["sudo", "systemctl", "enable", "--now", UNIT], interactive=True)[0]
    if rc == 0:
        run(["sudo", "systemctl", "restart", UNIT], interactive=True)
        print(green(f"Weboberflaeche eingeschaltet: {url_of(cfg)}"))
        _access_hint(cfg)
    return rc


def _access_hint(cfg):
    w = cfg["web"]
    if _is_loopback(w["listen"]):
        print("Sie lauscht nur auf diesem Server. Vom Arbeitsplatz aus per SSH-Tunnel oeffnen:")
        print(f"  ssh -L {w['port']}:127.0.0.1:{w['port']} {getpass.getuser()}@{socket.gethostname()}")
        print(f"  dann im Browser: http://localhost:{w['port']}/")
        print("Im Netz freigeben: Einstellung web.listen auf 0.0.0.0 (Terminal: web listen 0.0.0.0) und HTTPS mit 'web tls'.")


def web_off():
    run(["sudo", "systemctl", "disable", "--now", UNIT], interactive=True)
    print(green("Weboberflaeche ausgeschaltet."))
    return 0


def web_status(cfg):
    active = _timer_active(UNIT)
    print(f"Weboberflaeche: {green('eingeschaltet') if active else yellow('ausgeschaltet')}")
    print(f"Adresse:        {url_of(cfg)}{'' if cfg['web'].get('tls') else '  (ohne HTTPS)'}")
    users = _load_users()
    print(f"Benutzer:       {', '.join(sorted(users)) or red('keiner – web passwd ausfuehren')}")
    if active:
        _access_hint(cfg)
    return 0


def web_tls(cfg):
    w = cfg["web"]
    host = socket.gethostname()
    sans = [f"DNS:{host}", "DNS:localhost", "IP:127.0.0.1"]
    try:
        for ip in {a[4][0] for a in socket.getaddrinfo(host, None, socket.AF_INET)}:
            if f"IP:{ip}" not in sans:
                sans.append(f"IP:{ip}")
    except OSError:
        pass
    extra = input("Weitere Namen/IP-Adressen fuer das Zertifikat (Komma, leer = keine): ").strip()
    for x in [x.strip() for x in extra.split(",") if x.strip()]:
        try:
            ipaddress.ip_address(x)
            sans.append(f"IP:{x}")
        except ValueError:
            if re.fullmatch(r"[A-Za-z0-9.-]+", x):
                sans.append(f"DNS:{x}")
    cert, key = expand(w["cert"]), expand(w["key"])
    Path(cert).parent.mkdir(parents=True, exist_ok=True)
    rc = run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "825", "-subj", f"/CN={host}",
              "-addext", "subjectAltName=" + ",".join(sans), "-keyout", key, "-out", cert], interactive=True)[0]
    if rc:
        print(red("Zertifikat konnte nicht erzeugt werden (openssl)."))
        return 1
    os.chmod(key, 0o600)
    w["tls"] = True
    config.save(cfg)
    print(green(f"Zertifikat erzeugt ({', '.join(sans)}), HTTPS eingeschaltet."))
    print("Es ist selbst signiert: Der Browser warnt beim ersten Aufruf. Eigenes Zertifikat: Dateien ersetzen")
    print(f"  {cert}\n  {key}")
    if _timer_active(UNIT):
        run(["sudo", "systemctl", "restart", UNIT], interactive=True)
    return 0


def cli(cfg, args):
    sub = args[0] if args else "status"
    if sub == "serve":
        return serve(cfg)
    if sub == "on":
        return web_on(cfg)
    if sub == "off":
        return web_off()
    if sub == "status":
        return web_status(cfg)
    if sub == "restart":
        return run(["sudo", "systemctl", "restart", UNIT], interactive=True)[0]
    if sub == "tls":
        return web_tls(cfg)
    if sub == "passwd":
        user = args[1] if len(args) > 1 else (input(f"Benutzername [{getpass.getuser()}]: ").strip()
                                               or getpass.getuser())
        if v_name(user):
            print(red(v_name(user)))
            return 2
        set_password(user, _ask_password(user))
        print(green(f"Passwort fuer {user} gespeichert."))
        return 0
    if sub == "deluser" and len(args) > 1:
        users = _load_users()
        if users.pop(args[1], None) is None:
            print(red("Unbekannter Benutzer"))
            return 1
        _save_users(users)
        print(green(f"{args[1]} entfernt."))
        return 0
    if sub in ("listen", "port") and len(args) > 1:
        v = args[1]
        if sub == "port":
            if not v.isdigit() or not 1 <= int(v) <= 65535:
                print(red("Port 1–65535"))
                return 2
            cfg["web"]["port"] = int(v)
        else:
            try:
                ipaddress.ip_address(v)
            except ValueError:
                print(red("IP-Adresse angeben, z. B. 127.0.0.1 (nur lokal) oder 0.0.0.0 (alle Netze)"))
                return 2
            cfg["web"]["listen"] = v
        config.save(cfg)
        print(green(f"Gespeichert: {url_of(cfg)}"))
        if _timer_active(UNIT):
            run(["sudo", "systemctl", "restart", UNIT], interactive=True)
        return 0
    print("Aufruf: updatemanager-checkmk web on|off|status|restart|tls|passwd [BENUTZER]|deluser BENUTZER|"
          "listen IP|port PORT")
    return 2
