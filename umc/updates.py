"""Update-Ablaeufe: Inventar, Vorpruefung, OS-Update, Checkmk-Update (Patch + Major), Wartungslauf.

Auf den Zielsystemen laeuft kein eigenes Programm – alles wird vom Update-Server per SSH ausgefuehrt.
"""
import concurrent.futures as cf
import re
import threading
import time

from . import config
from .cmkapi import COMMENT
from .common import (green, load_state, now, red, record, stamp, update_state, vbase,
                     vparse, window_info, yellow)
from .config import master_target
from .osprofiles import (DETECT_CMD, adapter, default_profile, edition_change_allowed,
                         edition_of_site_version, family_of, os_label, parse_os_release, profile_id,
                         repo_probe_path)


_PROFILE_LOCK = threading.Lock()


def _upgraded_packages(family, out):
    """Aktualisierte Pakete aus der Ausgabe des Paketmanagers: [(name, alt, neu)]."""
    res = []
    if family == "apt":
        for m in re.finditer(r"Unpacking (\S+) \(([^)]+)\) over \(([^)]+)\)", out or ""):
            res.append((m.group(1).split(":")[0], m.group(3), m.group(2)))
    elif family == "dnf":
        sec = re.search(r"Upgraded:\s*\n(.*?)(?:\n\s*\n|\nInstalled:|\nComplete!)", out or "", re.S)
        for ln in (sec.group(1).split() if sec else []):
            m = re.match(r"(.+?)-(\d[^-]*-[^-]+)\.(?:x86_64|noarch|i686)$", ln)
            if m:
                res.append((m.group(1), "", m.group(2)))
    elif family == "zypper":
        sec = re.search(r"packages? (?:is|are) going to be upgraded:\s*\n(.*?)\n\s*\n", out or "", re.S)
        for ln in (sec.group(1).split() if sec else []):
            res.append((ln, "", ""))
    seen, uniq = set(), []
    for r in res:
        if r[0] not in seen:
            seen.add(r[0])
            uniq.append(r)
    return uniq


def _os_summary(pkgs, rebooted):
    if not pkgs:
        txt = "keine Pakete aktualisiert"
    else:
        names = ", ".join(p[0] for p in pkgs[:6]) + (" …" if len(pkgs) > 6 else "")
        txt = f"{len(pkgs)} Paket{'e' if len(pkgs) != 1 else ''} aktualisiert ({names})"
    return txt + (", neu gestartet" if rebooted else "")


def branch(v):
    t = vparse(v or "")
    return t[:3] if t else None


def bstr(b):
    return f"{b[0]}.{b[1]}.{b[2]}" if b else "?"


class Updater:
    def __init__(self, cfg, log, remote, api, repo, dry_run=False, enforce_window=False):
        self.cfg, self.log, self.remote, self.api, self.repo = cfg, log, remote, api, repo
        self.dry = dry_run
        self.enforce_window = enforce_window
        self.reports = []           # [(titel, ergebnisse)] fuer Benachrichtigungen
        self.blocked = []           # Hinweise (z. B. blockierte Major-Updates)

    # ============================================================ Ziele
    def customers(self, only=None):
        if only:
            return [c for c in self.cfg["customers"] if c["name"] in only]
        return [c for c in self.cfg["customers"] if c.get("enabled", True)]

    def master(self):
        return master_target(self.cfg)

    def _master_selected(self, only):
        m = self.master()
        return bool(m.get("host")) and (not only or m["name"] in only)

    def all_targets(self, only=None):
        ts = self.customers(only)
        if self._master_selected(only):
            ts = ts + [self.master()]
        return ts

    # ============================================================ Hilfen
    def time_left_ok(self, label):
        if not self.enforce_window:
            return True
        s = self.cfg["schedule"]
        inside, left = window_info(s)
        if inside and left >= int(s["min_minutes_left"]):
            return True
        self.log.warn(f"{label}: nicht mehr begonnen – Wartungsfenster endet ({int(left)} min uebrig)")
        return False

    def _fail(self, name, kind, msg):
        self.log.err(f"{name}: {msg}")
        record(name, kind, False, msg)
        return False, msg

    def _safe(self, fn, t, *args):
        try:
            return fn(t, *args)
        except Exception as e:  # ein Host darf den Gesamtlauf nicht abbrechen
            return self._fail(t["name"], "error", f"Unerwarteter Fehler: {e}")

    def summary(self, title, results):
        self.reports.append((title, dict(results)))
        self.log.step(f"Ergebnis {title}")
        if not results:
            self.log.info("  (keine Systeme)")
        for name, (ok, msg) in results.items():
            sym = green("OK    ") if ok is True else red("FEHLER") if ok is False else yellow("--    ")
            self.log.info(f"  {sym} {name}: {msg}")

    def _boot_id(self, t):
        rc, out = self.remote.out(t, "cat /proc/sys/kernel/random/boot_id", timeout=20)
        return out if rc == 0 and out else None

    def _wait_reboot(self, t, old):
        deadline = time.time() + int(self.cfg["updates"]["reboot_timeout_min"]) * 60
        time.sleep(20)
        while time.time() < deadline:
            b = self._boot_id(t)
            if b and b != old:
                return True
            time.sleep(10)
        return False

    def _wait_site(self, t):
        if not t.get("site"):
            return True
        deadline = time.time() + int(self.cfg["updates"]["site_start_timeout_min"]) * 60
        while True:
            rc, _ = self.remote.out(t, f"sudo omd status -b {t['site']} >/dev/null 2>&1")
            if rc == 0:
                return True
            if time.time() > deadline:
                return False
            time.sleep(10)

    # ============================================================ Inventar
    def site_version(self, t):
        if not t.get("site"):
            return None
        rc, out = self.remote.out(t, f"omd version -b {t['site']}")
        return out.splitlines()[-1].strip() if rc == 0 and out else None

    def detect(self, t, pending=False):
        """Erkennt OS, Version, Kernel, Checkmk-Version/Edition (und offene Updates) eines Systems."""
        rc, out = self.remote.out(t, DETECT_CMD, timeout=30)
        if rc != 0 or "ID=" not in out:
            return None
        osr = parse_os_release(out)
        fam = family_of(osr)
        pid = profile_id(osr)
        info = {"time": now().isoformat(timespec="seconds"), "family": fam, "profile": pid,
                "label": os_label(osr), "os_id": osr.get("ID"), "os_version": osr.get("VERSION_ID"),
                "codename": osr.get("VERSION_CODENAME", ""), "kernel": osr.get("UMC_KERNEL"),
                "arch": osr.get("UMC_ARCH")}
        with _PROFILE_LOCK:
            known = self.cfg["os_profiles"].get(pid)
            if known and not known.get("version"):          # aus Version 0.1 uebernommenes Profil ergaenzen
                known.update(version=osr.get("VERSION_ID", ""), label=os_label(osr))
                if not self.dry:
                    config.save(self.cfg)
            if pid and pid not in self.cfg["os_profiles"]:
                prof = default_profile(osr)
                if prof:
                    self.cfg["os_profiles"][pid] = prof
                    if not self.dry:
                        config.save(self.cfg)
                    self.log.info(f"  Neues Betriebssystem erkannt: {prof['label']} -> Profil {pid}")
                    if prof.get("note"):
                        self.log.warn(f"  {prof['note']}")
        v = self.site_version(t)
        info["checkmk"] = v
        info["edition"] = edition_of_site_version(v) if v else None
        if t.get("site"):
            rc, out = self.remote.out(t, "omd versions -b 2>/dev/null", timeout=30)
            info["omd_versions"] = out.split() if rc == 0 else []
        if pending and fam:
            rc, out = self.remote.out(t, adapter(fam)["pending"], tunnel=True, timeout=180)
            info["pending"] = int(out.splitlines()[-1]) if rc in (0, 1) and out and out.splitlines()[-1].isdigit() else None
            info["pending_time"] = now().isoformat(timespec="seconds")

        def save(s):
            prev = s.setdefault("inventory", {}).get(t["name"]) or {}
            if not pending:            # offene Updates nicht neu gezaehlt: letzten Stand behalten
                for k in ("pending", "pending_time"):
                    if k in prev:
                        info[k] = prev[k]
            s["inventory"][t["name"]] = info
        update_state(save)
        return info

    def inventory_all(self, only=None, pending=False, packages=True):
        targets = self.all_targets(only)
        res = {}

        def one(t):
            return t, self.detect(t, pending)
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            for t, info in ex.map(one, targets):
                if not info:
                    res[t["name"]] = (False, "nicht erreichbar")
                    continue
                if not info["family"]:
                    res[t["name"]] = (False, f"{info['label']} wird nicht unterstuetzt")
                    continue
                txt = info["label"]
                if info.get("checkmk"):
                    txt += f", Checkmk {info['checkmk']}"
                if info.get("pending") is not None:
                    txt += f", {info['pending']} OS-Updates offen"
                res[t["name"]] = (True, txt)
        if packages:
            reachable = [t for t in targets if res.get(t["name"], (False,))[0]]
            self.repo.refresh_package_lists(self.remote, reachable)
        self.summary("Systeme erfassen", res)
        return all(r[0] for r in res.values())

    def host_info(self, t):
        info = self.detect(t)
        if not info:
            return None, None
        return info, self.cfg["os_profiles"].get(info["profile"])

    # ============================================================ Vorpruefung
    def preflight(self, t, need_site=False):
        """Liefert (fehler, warnungen, info, profil)."""
        errs, warns = [], []
        info, prof = self.host_info(t)
        if not info:
            return ["SSH nicht erreichbar (Adresse, Schluessel, svc-update-User pruefen)"], warns, None, None
        if not info["family"]:
            return [f"Betriebssystem nicht unterstuetzt: {info['label']}"], warns, info, None
        if not prof or not prof.get("enabled", True):
            errs.append(f"Kein aktives Repo-Profil fuer {info['label']}")
        ad = adapter(info["family"])
        rc, _ = self.remote.out(t, f"sudo -n -l {ad['pm']} >/dev/null && sudo -n -l /usr/sbin/reboot >/dev/null")
        if rc:
            errs.append(f"sudo-Rechte fuer {ad['pm'].split('/')[-1]}/reboot fehlen")
        if need_site and t.get("site"):
            rc, _ = self.remote.out(t, "sudo -n -l /usr/bin/omd >/dev/null")
            if rc:
                errs.append("sudo-Recht fuer omd fehlt")
            if not info.get("checkmk"):
                errs.append(f"Site {t['site']} nicht gefunden")
        rc, out = self.remote.out(t, "date +%s")
        if rc == 0 and out.isdigit():
            drift = abs(int(out) - int(time.time()))
            if drift > 120:
                errs.append(f"Uhrzeit weicht um {drift} s ab (NTP pruefen)")
            elif drift > 30:
                warns.append(f"Uhrzeit weicht um {drift} s ab")
        path = repo_probe_path(prof)
        if path:
            tp = self.cfg["repo"]["tunnel_port"]
            probe = (f"exec 3<>/dev/tcp/127.0.0.1/{tp} && "
                     f"printf 'HEAD {path} HTTP/1.0\\r\\n\\r\\n' >&3 && head -1 <&3")
            rc, out = self.remote.out(t, probe, tunnel=True, timeout=60)
            if rc != 0 or " 200" not in out:
                errs.append(f"Repo ueber Tunnel nicht erreichbar ({(out.splitlines() or ['rc ' + str(rc)])[0][:60]}) "
                            "– Repo synchronisiert?")
        rc, out = self.remote.out(t, "df -Pm / | awk 'NR==2{print $4}'")
        if rc == 0 and out.isdigit() and int(out) < 2048:
            warns.append(f"Nur {out} MB frei auf /")
        return errs, warns, info, prof

    def check_all(self, only=None):
        results = {}

        def one(t):
            return t, self.preflight(t, need_site=bool(t.get("site")))
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            for t, (errs, warns, info, _) in ex.map(one, self.all_targets(only)):
                label = f"[{info['label']}] " if info else ""
                if errs:
                    results[t["name"]] = (False, label + "; ".join(errs))
                elif warns:
                    results[t["name"]] = (None, label + "; ".join(warns))
                else:
                    results[t["name"]] = (True, label + "alles in Ordnung")
        self.summary("Vorpruefung", results)
        if not self.dry:          # fuer die Checkliste "Einrichtung abschliessen"
            stamp_ = now().isoformat(timespec="seconds")
            update_state(lambda s: s.setdefault("preflight", {}).update(
                {n: {"time": stamp_, "ok": r[0] is not False, "msg": r[1]} for n, r in results.items()}))
        return all(r[0] is not False for r in results.values())

    # ============================================================ OS-Update
    def os_update_host(self, t):
        name, host = t["name"], t.get("checkmk_host")
        self.log.step(f"OS-Update {name} ({t['host']})")
        errs, warns, info, prof = self.preflight(t)
        for w in warns:
            self.log.warn(f"{name}: {w}")
        if errs:
            return self._fail(name, "os", "Vorpruefung: " + "; ".join(errs))
        ad = adapter(info["family"])
        self.log.info(f"{name}: {info['label']}, Kernel {info.get('kernel')}")
        boot = self._boot_id(t)
        self.api.set_downtime(host, int(self.cfg["updates"]["downtime_min"]), f"{COMMENT}: OS-Update")
        try:
            rc, out = self.remote.run(t, ad["upgrade"], tunnel=True, timeout=3600)
            if rc:
                return self._fail(name, "os", f"Paket-Update fehlgeschlagen (rc {rc})")
            pkgs = _upgraded_packages(info["family"], out)
            rebooted = False
            rc, _ = self.remote.out(t, ad["reboot_check"])
            if rc == 0:
                rebooted = True
                self.log.info(f"{name}: Neustart erforderlich")
                self.remote.run(t, "sudo reboot", quiet=True, timeout=30)
                if not self.dry:
                    if not self._wait_reboot(t, boot):
                        return self._fail(name, "os", "nach dem Neustart nicht wieder erreichbar")
                    self.log.info(f"{name}: neu gestartet (neue Boot-ID)")
                    if not self._wait_site(t):
                        return self._fail(name, "os", f"Site {t['site']} laeuft nach dem Neustart nicht")
            msg = _os_summary(pkgs, rebooted)
            if not self.dry:
                after = self.detect(t)          # neue OS-Version / neuer Kernel?
                if after:
                    if after.get("label") and after["label"] != info.get("label"):
                        msg += f", {info.get('label')} -> {after['label']}"
                    if rebooted and after.get("kernel") and after["kernel"] != info.get("kernel"):
                        msg += f", Kernel {info.get('kernel')} -> {after['kernel']}"
            self.log.ok(f"{name}: OS-Update erfolgreich – {msg}")
            record(name, "os", True, msg, packages=pkgs[:200])
            if not self.dry:
                update_state(lambda s: s.setdefault("inventory", {}).setdefault(name, {}).update(
                    {"pending": 0, "pending_time": now().isoformat(timespec="seconds")}))
            return True, msg
        finally:
            self.api.remove_downtimes(host)

    def _parallel(self, jobs, results):
        """jobs: [(name, funktion, args...)] – gleichzeitig ausfuehren, in Gruppen laut Einstellung
        updates.batch_size (0 = alle auf einmal). Vor jeder Gruppe wird die Restzeit im Fenster geprueft."""
        if not jobs:
            return
        bs = int(self.cfg["updates"].get("batch_size", 0) or 0)
        bs = len(jobs) if bs <= 0 else bs
        for i in range(0, len(jobs), bs):
            batch = jobs[i:i + bs]
            if not self.time_left_ok(", ".join(j[0] for j in batch)):
                for j in jobs[i:]:
                    results[j[0]] = (None, "nicht begonnen (Wartungsfenster)")
                break
            if len(batch) > 1:
                self.log.info(f"Gleichzeitig: {', '.join(j[0] for j in batch)}")
            with cf.ThreadPoolExecutor(max_workers=len(batch)) as ex:
                futs = {ex.submit(self._safe, j[1], *j[2:]): j[0] for j in batch}
                for f in cf.as_completed(futs):
                    results[futs[f]] = f.result()

    def os_update_all(self, only=None, include_master=True):
        cs = self.customers(only)
        results = {}
        self._parallel([(c["name"], self.os_update_host, c) for c in cs], results)
        if include_master and self._master_selected(only):
            m = self.master()
            if self.time_left_ok("Master"):
                results[m["name"]] = self._safe(self.os_update_host, m)
            else:
                results[m["name"]] = (None, "nicht begonnen (Wartungsfenster)")
        self.summary("OS-Update", results)
        return all(r[0] is not False for r in results.values())

    # ============================================================ Checkmk: einzelne Site
    def _omd_update(self, t, full, edition_change=False):
        """omd update ohne -f: Rueckfragen werden gezielt beantwortet, die Sicherheitspruefungen
        von Checkmk (Versionssperre, Editions-Pruefung, Vorabpruefung) bleiben aktiv."""
        opts = "--pre-flight=abort --skeleton=install --confirm-version"
        if edition_change:
            opts += " --confirm-edition"
        return self.remote.run(t, f"sudo omd -V {full} update {opts} {t['site']}", timeout=3600)

    def _after_failed_update(self, t, old_full, backup, ck):
        """Fehlgeschlagenes Update aufraeumen: unveraenderte Site nur starten, sonst Rollback."""
        site = t["site"]
        now_v = None if self.dry else self.site_version(t)
        if now_v == old_full:
            self.log.warn(f"{t['name']}: Checkmk hat das Update vorab abgebrochen – Site unveraendert, wird gestartet")
            self.remote.run(t, f"sudo omd start {site}", timeout=600)
        elif backup and ck.get("auto_rollback", True):
            self._rollback(t, backup)

    def _rollback(self, t, backup):
        site = t["site"]
        self.log.warn(f"{t['name']}: Rollback aus {backup}")
        self.remote.run(t, f"sudo omd stop {site}", timeout=600)
        rc, _ = self.remote.run(t, f"sudo omd restore --reuse --kill {site} {backup}", timeout=3600)
        self.remote.run(t, f"sudo omd start {site}", timeout=600)
        if rc == 0:
            self.log.warn(f"{t['name']}: Rollback durchgefuehrt – vorherige Version laeuft wieder")
        else:
            self.log.err(f"{t['name']}: Rollback fehlgeschlagen – bitte manuell pruefen!")

    def checkmk_update_site(self, t, target, allow_major=False):
        name, site, host = t["name"], t["site"], t.get("checkmk_host")
        kind = "Major" if allow_major else "Patch"
        self.log.step(f"Checkmk-{kind}-Update {name} / Site {site}")
        errs, warns, info, prof = self.preflight(t, need_site=True)
        for w in warns:
            self.log.warn(f"{name}: {w}")
        if errs:
            return self._fail(name, "checkmk", "Vorpruefung: " + "; ".join(errs))
        cur = info["checkmk"]
        cur_base = vbase(cur)
        suffix = cur[len(cur_base):].lstrip(".")
        ck = self.cfg["checkmk"]
        if info.get("edition") != ck["edition"]:
            return self._fail(name, "checkmk", f"Edition der Site ({info.get('edition')}) passt nicht zur "
                                               f"eingestellten Edition ({ck['edition']}) – abgebrochen")
        if branch(cur_base) != branch(target) and not allow_major:
            msg = f"{cur_base} -> {target} waere ein Major-Wechsel"
            self.log.warn(f"{name}: {msg}")
            return None, msg
        if vparse(cur_base) >= vparse(target):
            self.log.info(f"{name}: bereits auf {cur_base}")
            record(name, "checkmk", True, f"aktuell ({cur_base})", version=cur_base)
            return True, f"bereits aktuell ({cur_base})"
        if not prof or target not in self.repo.repo_versions(prof):
            return self._fail(name, "checkmk", f"Checkmk {target} fuer {info['label']} nicht im Repo")

        ad = adapter(info["family"])
        self.api.set_downtime(host, int(self.cfg["updates"]["downtime_min"]),
                              f"{COMMENT}: Checkmk {cur_base} -> {target}")
        backup = None
        try:
            if ck["backup"] != "none":
                backup = f"~/updatemanager-backups/{site}_{cur_base}_{stamp()}.tar.gz"
                self.log.info(f"{name}: Backup der Site -> {backup}")
                opt = "-N " if ck["backup"] == "no-past" else ""
                rc, _ = self.remote.run(t, f"mkdir -p ~/updatemanager-backups && sudo omd backup {opt}{site} - > {backup}",
                                        timeout=3600)
                if rc:
                    return self._fail(name, "checkmk", "Backup fehlgeschlagen – Update nicht begonnen")
                self.remote.run(t, f"ls -1t ~/updatemanager-backups/{site}_*.tar.gz 2>/dev/null | "
                                   f"tail -n +{int(ck['keep_backups']) + 1} | xargs -r rm -f", quiet=True)

            pkg = f"check-mk-{ck['edition']}-{target}"
            self.log.info(f"{name}: installiere {pkg} aus dem Repo")
            rc, _ = self.remote.run(t, ad["install"].format(pkg=pkg), tunnel=True, timeout=3600)
            if rc:
                return self._fail(name, "checkmk", f"Paket {pkg} konnte nicht installiert werden")

            # Zielverzeichnis exakt bestimmen: richtige Version UND richtige Edition
            # (auf einem System koennen mehrere Editionen derselben Version installiert sein)
            rc, out = self.remote.out(t, f"ls /opt/omd/versions | grep -E '^{re.escape(target)}\\.'")
            matches = [x for x in out.split() if x.startswith(target + ".")
                       and edition_of_site_version(x) == ck["edition"]]
            if matches:
                full = matches[0]
            elif self.dry or edition_of_site_version(f"{target}.{suffix}") == ck["edition"]:
                full = f"{target}.{suffix}"
            else:
                return self._fail(name, "checkmk", f"Installierte Version {target} der Edition {ck['edition']} "
                                                   f"nicht gefunden ({out.split() or 'nichts'})")
            self.log.info(f"{name}: Site {site} auf {full} umstellen (stoppen, updaten, starten)")
            self.remote.run(t, f"sudo omd stop {site}", timeout=600)
            rc, _ = self._omd_update(t, full)
            if rc == 0:
                self.remote.run(t, f"sudo omd start {site}", timeout=600)
            new = full if self.dry else self.site_version(t)
            if rc or new != full or not self._wait_site(t):
                msg = f"Update auf {full} fehlgeschlagen (rc {rc}, Site-Version jetzt {new})"
                self.log.err(f"{name}: {msg}")
                self._after_failed_update(t, cur, backup, ck)
                record(name, "checkmk", False, msg)
                return False, msg
            self.log.ok(f"{name}: Checkmk {cur_base} -> {target} erfolgreich")
            record(name, "checkmk", True, f"{cur_base} -> {target}", version=target)
            if not self.dry:
                update_state(lambda s: s.setdefault("inventory", {}).setdefault(name, {}).update(
                    {"checkmk": full, "edition": edition_of_site_version(full)}))
            if ck.get("omd_cleanup", True):
                self.log.info(f"{name}: ungenutzte Checkmk-Versionen entfernen (omd cleanup)")
                self.remote.run(t, "sudo omd cleanup", timeout=1800)
            return True, f"{cur_base} -> {target}"
        finally:
            self.api.remove_downtimes(host)

    # ============================================================ Checkmk: Planung
    def _site_states(self, infos=None):
        """Version, Profil und Edition aller Sites (Master + aktive Kunden mit Site)."""
        states = {}

        def one(t):
            info = infos.get(t["name"]) if infos is not None else self.detect(t)
            prof = self.cfg["os_profiles"].get(info["profile"]) if info else None
            return t, info, prof
        targets = [c for c in self.customers() if c.get("site")]
        if self.master().get("host"):
            targets.append(self.master())
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            for t, info, prof in ex.map(one, targets):
                states[t["name"]] = {"t": t, "info": info, "prof": prof,
                                     "ver": vbase(info["checkmk"]) if info and info.get("checkmk") else None}
        return states

    def _best_patch(self, st):
        """Neuester Patch im Repo fuer den Zweig dieser Site (oder None)."""
        if not st["prof"] or not st["ver"]:
            return None
        cands = [v for v in self.repo.repo_versions(st["prof"])
                 if branch(v) == branch(st["ver"]) and vparse(v) > vparse(st["ver"])]
        return cands[-1] if cands else None

    def plan(self, infos=None):
        """Ermittelt, was im naechsten Lauf passieren wuerde (fuer Status und Lauf)."""
        states = self._site_states(infos)
        m = self.master()
        mst = states.get(m["name"])
        plan = {"states": states, "patches": {}, "catchup": {}, "major": None, "blocked": [], "error": None}
        if not mst or not mst["ver"]:
            plan["error"] = "Checkmk-Version des Masters nicht ermittelbar"
            return plan
        ed = self.cfg["checkmk"]["edition"]
        for n, st in states.items():
            if st["info"] and st["info"].get("edition") and st["info"]["edition"] != ed:
                plan["blocked"].append(f"{n}: Edition {st['info']['edition']} statt {ed}")
            bp = self._best_patch(st)
            if bp:
                plan["patches"][n] = bp
        # ---------------- Remote Sites, die einen Zweig hinter dem Master stehen, nachziehen
        mb = branch(mst["ver"])
        for n, st in states.items():
            if n == m["name"] or not st["ver"] or n in plan["patches"]:
                continue
            b = branch(st["ver"])
            if b >= mb:
                continue
            if mb not in ((b[0], b[1] + 1, 0), (b[0] + 1, 0, 0)):
                plan["blocked"].append(f"{n}: steht auf {st['ver']}, mehr als ein Major-Schritt hinter dem Master "
                                       f"({mst['ver']}) – bitte manuell schrittweise aktualisieren")
                continue
            cands = [v for v in self.repo.repo_versions(st["prof"]) if branch(v) == mb] if st["prof"] else []
            if cands:
                plan["catchup"][n] = cands[-1]
            else:
                label = st["info"]["label"] if st["info"] else n
                plan["blocked"].append(f"{n}: bleibt auf {st['ver']} – Checkmk {bstr(mb)} nicht verfuegbar fuer "
                                       f"{label} (OS-Upgrade noetig)")
        if plan["patches"] or plan["catchup"] or not self.cfg["checkmk"].get("major_auto", True):
            return plan
        # ---------------- Major-Schritt pruefen (immer nur EIN Zweig weiter als der Master)
        nexts = sorted({branch(v) for v in self.repo.all_versions() if branch(v) > mb})
        if not nexts:
            return plan
        nb = nexts[0]
        reasons, targets = [], {}
        need = self.cfg["checkmk"].get("min_patch", {}).get(bstr(nb))
        for n, st in states.items():
            if not st["ver"]:
                reasons.append(f"{n}: Checkmk-Version unbekannt")
                continue
            b = branch(st["ver"])
            if b not in (mb, nb):
                reasons.append(f"{n}: steht auf {st['ver']} (weder {bstr(mb)} noch {bstr(nb)})")
                continue
            if b == mb and need and vparse(st["ver"]) < vparse(need):
                reasons.append(f"{n}: Mindest-Patchstand fuer {bstr(nb)} ist {need} (hat {st['ver']})")
            cands = [v for v in self.repo.repo_versions(st["prof"]) if branch(v) == nb] if st["prof"] else []
            if not cands:
                label = st["info"]["label"] if st["info"] else n
                reasons.append(f"{n}: Checkmk {bstr(nb)} nicht verfuegbar fuer {label} – OS-Upgrade noetig")
                continue
            targets[n] = cands[-1]
        if reasons or plan["blocked"]:
            plan["blocked"] += [f"Major {bstr(nb)} blockiert – {r}" for r in reasons]
        else:
            plan["major"] = {"branch": bstr(nb), "targets": targets}
        return plan

    def checkmk_update_all(self, only=None, include_master=True):
        m = self.master()
        if not m.get("host") or not m.get("site"):
            self.log.err("Kein Master konfiguriert")
            return False
        p = self.plan()
        if p["error"]:
            self.log.err(p["error"])
            return False
        for b in p["blocked"]:
            self.log.warn(b)
        self.blocked = list(p["blocked"])
        if not self.dry:
            update_state(lambda s: s.update({"checkmk_plan": {
                "time": now().isoformat(timespec="seconds"), "patches": p["patches"], "catchup": p["catchup"],
                "major": p["major"], "blocked": p["blocked"]}}))
        sel = {c["name"] for c in self.customers(only) if c.get("site")}
        results = {}

        def run_round(targets, allow_major):
            all_ok = True
            jobs = []
            for n, target in targets.items():
                if n == m["name"]:
                    continue
                st = p["states"][n]
                if n not in sel:
                    all_ok = False if vparse(st["ver"] or "0.0.0") < vparse(target) else all_ok
                    continue
                jobs.append((n, self.checkmk_update_site, st["t"], target, allow_major))
            # Remote Sites gleichzeitig, der Master erst danach
            self._parallel(jobs, results)
            if any(results.get(j[0], (False,))[0] is not True for j in jobs):
                all_ok = False
            mt = targets.get(m["name"])
            if mt and include_master and self._master_selected(only):
                if not all_ok:
                    msg = "bleibt auf bisheriger Version – nicht alle Remote Sites erfolgreich"
                    self.log.warn(f"Master {m['name']}: {msg}")
                    results[m["name"]] = (None, msg)
                elif self.time_left_ok("Master"):
                    results[m["name"]] = self._safe(self.checkmk_update_site, p["states"][m["name"]]["t"],
                                                    mt, allow_major)
                else:
                    results[m["name"]] = (None, "nicht begonnen (Wartungsfenster)")

        if p["catchup"]:
            self.log.info("Nachholen (Remote Site einen Zweig hinter dem Master): "
                          + ", ".join(f"{n} -> {v}" for n, v in p["catchup"].items()))
            self._parallel([(n, self.checkmk_update_site, p["states"][n]["t"], target, True)
                            for n, target in p["catchup"].items() if n in sel], results)
        if p["patches"]:
            self.log.info("Patch-Updates: " + ", ".join(f"{n} -> {v}" for n, v in p["patches"].items()))
            run_round(p["patches"], False)
            if p["major"] is None and self.repo.all_versions() and any(
                    branch(v) > branch(p["states"][m["name"]]["ver"]) for v in self.repo.all_versions()):
                self.log.info("Major-Update folgt in einem spaeteren Lauf (erst alle Sites auf neuesten Patch).")
        elif p["major"]:
            mj = p["major"]
            self.log.info(f"Major-Update auf {mj['branch']}: " + ", ".join(f"{n} -> {v}" for n, v in mj["targets"].items()))
            run_round(mj["targets"], True)
        elif not p["catchup"]:
            self.log.info("Alle Sites sind aktuell – kein Checkmk-Update noetig")
        self.summary("Checkmk-Update", results)
        if any(r[0] is True for r in results.values()) and self.cfg["checkmk"].get("repo_cleanup", True):
            self.log.step("Repo bereinigen (Checkmk-Pakete, die keine Site mehr braucht)")
            self.repo.cleanup_checkmk()
        return all(r[0] is not False for r in results.values())

    # ============================================================ Editionswechsel
    def edition_switch_check(self, new):
        """Prueft die Voraussetzungen. Liefert (ok, gruende, states, version)."""
        old = self.cfg["checkmk"]["edition"]
        ok, why = edition_change_allowed(old, new)
        if not ok:
            return False, [why], {}, None
        states = self._site_states()
        reasons, versions = [], set()
        m = self.master()
        if m["name"] not in states or not states[m["name"]]["ver"]:
            reasons.append("Master nicht erreichbar oder Checkmk-Version unbekannt")
        for n, st in states.items():
            if not st["ver"]:
                reasons.append(f"{n}: Checkmk-Version unbekannt")
                continue
            ed = st["info"].get("edition")
            if ed not in (old, new):
                reasons.append(f"{n}: hat Edition {ed} (erwartet {old})")
            versions.add(st["ver"])
        if len(versions) > 1:
            reasons.append("Nicht alle Sites auf derselben Version (" + ", ".join(sorted(versions)) +
                           ") – zuerst updaten, dann Edition wechseln")
        if new not in ("community", "raw"):
            from .common import expand
            import os
            if not os.path.exists(expand(self.cfg["checkmk"]["credentials_file"])):
                reasons.append("Download-Zugangsdaten fuer die neue Edition fehlen (user:passwort)")
        return not reasons, reasons, states, (versions.pop() if len(versions) == 1 else None)

    def _edition_switch_site(self, t, info, prof, ver, new):
        name, site, host = t["name"], t["site"], t.get("checkmk_host")
        self.log.step(f"Editionswechsel {name} / Site {site}: {info.get('edition')} -> {new}")
        if info.get("edition") == new:
            return True, "hat die neue Edition bereits"
        errs, warns, info, prof = self.preflight(t, need_site=True)
        if errs:
            return self._fail(name, "checkmk", "Vorpruefung: " + "; ".join(errs))
        cur_full = info["checkmk"]
        ck = self.cfg["checkmk"]
        self.api.set_downtime(host, int(self.cfg["updates"]["downtime_min"]), f"{COMMENT}: Edition -> {new}")
        backup = None
        try:
            if ck["backup"] != "none":
                backup = f"~/updatemanager-backups/{site}_{ver}_{info.get('edition')}_{stamp()}.tar.gz"
                self.log.info(f"{name}: Backup der Site -> {backup}")
                opt = "-N " if ck["backup"] == "no-past" else ""
                if self.remote.run(t, f"mkdir -p ~/updatemanager-backups && sudo omd backup {opt}{site} - > {backup}",
                                   timeout=3600)[0]:
                    return self._fail(name, "checkmk", "Backup fehlgeschlagen – Wechsel nicht begonnen")
            pkg = f"check-mk-{new}-{ver}"
            self.log.info(f"{name}: installiere {pkg} aus dem Repo")
            if self.remote.run(t, adapter(info["family"])["install"].format(pkg=pkg), tunnel=True, timeout=3600)[0]:
                return self._fail(name, "checkmk", f"Paket {pkg} konnte nicht installiert werden")
            rc, out = self.remote.out(t, f"ls /opt/omd/versions | grep -E '^{re.escape(ver)}\\.'")
            cands = [x for x in out.split() if x != cur_full and edition_of_site_version(x) == new]
            if not cands:
                other = [x for x in out.split() if x != cur_full]
                if len(other) == 1:
                    self.log.warn(f"{name}: Versionskennung {other[0]} – Edition nicht eindeutig erkennbar, verwende sie")
                    cands = other
            if not cands and self.dry:
                cands = [f"{ver}.{new}"]
            if not cands:
                return self._fail(name, "checkmk", f"Installierte Version {ver} der Edition {new} nicht gefunden")
            full = cands[0]
            self.log.info(f"{name}: Site {site} auf {full} umstellen")
            self.remote.run(t, f"sudo omd stop {site}", timeout=600)
            rc, _ = self._omd_update(t, full, edition_change=True)
            if rc == 0:
                self.remote.run(t, f"sudo omd start {site}", timeout=600)
            now_v = full if self.dry else self.site_version(t)
            if rc or now_v != full or not self._wait_site(t):
                msg = f"Wechsel auf {full} fehlgeschlagen (rc {rc}, Site-Version jetzt {now_v})"
                self.log.err(f"{name}: {msg}")
                self._after_failed_update(t, cur_full, backup, ck)
                record(name, "checkmk", False, msg)
                return False, msg
            self.log.ok(f"{name}: jetzt {full}")
            record(name, "checkmk", True, f"Edition -> {new}", version=ver)
            return True, f"{cur_full} -> {full}"
        finally:
            self.api.remove_downtimes(host)

    def edition_switch(self, new):
        """Wechselt alle Sites auf eine hoehere Edition (gleiche Version), Remote Sites zuerst, Master zuletzt."""
        ok, reasons, states, ver = self.edition_switch_check(new)
        old = self.cfg["checkmk"]["edition"]
        if not ok:
            for r in reasons:
                self.log.err(f"Editionswechsel nicht moeglich: {r}")
            return False
        self.log.step(f"Editionswechsel {old} -> {new} auf Version {ver}")
        # Pakete der neuen Edition fuer alle beteiligten Betriebssysteme laden
        import copy
        from .repo import Repo
        cfg_new = copy.deepcopy(self.cfg)
        cfg_new["checkmk"]["edition"] = new
        repo_new = Repo(cfg_new, self.log, self.dry)
        profs = {st["prof"]["id"]: st["prof"] for st in states.values() if st["prof"]}
        res = repo_new.fetch_version(ver, list(profs.values()))
        missing = [pid for pid, r in res.items() if r != "ok"]
        if missing:
            self.log.err(f"Checkmk {ver} ({new}) nicht ladbar fuer: {', '.join(missing)} – Zugangsdaten/Lizenz pruefen")
            return False
        repo_save, self.repo = self.repo, repo_new
        results, all_ok = {}, True
        m = self.master()
        try:
            for n, st in states.items():
                if n == m["name"]:
                    continue
                r = self._safe(self._edition_switch_site, st["t"], st["info"], st["prof"], ver, new)
                results[n] = r
                all_ok = all_ok and r[0] is True
            if all_ok:
                st = states[m["name"]]
                results[m["name"]] = self._safe(self._edition_switch_site, st["t"], st["info"], st["prof"], ver, new)
                all_ok = results[m["name"]][0] is True
            else:
                results[m["name"]] = (None, "nicht gewechselt – nicht alle Remote Sites erfolgreich")
        finally:
            self.repo = repo_save
        if all_ok and not self.dry:
            self.cfg["checkmk"]["edition"] = new
            config.save(self.cfg)
            self.log.ok(f"Edition in der Konfiguration auf {new} umgestellt")
        elif not all_ok:
            self.log.warn("Edition in der Konfiguration bleibt unveraendert – Wechsel erneut starten, "
                          "bereits gewechselte Sites werden uebersprungen.")
        self.summary("Editionswechsel", results)
        return all_ok

    # ============================================================ Wartungslauf
    def _fresh_checkmk_check(self, max_age_h=6):
        """Vor dem Checkmk-Teil: ist die Versionssuche aelter als max_age_h, jetzt nachholen.
        So findet das Wartungsfenster neue Versionen auch dann, wenn der Repo-Sync noch nicht gelaufen ist."""
        chk = load_state().get("checkmk_check") or {}
        try:
            import datetime as _d
            age = (now() - _d.datetime.fromisoformat(chk["time"])).total_seconds() / 3600
        except (KeyError, TypeError, ValueError):
            age = None
        if age is not None and age < max_age_h:
            return
        self.log.step("Checkmk-Versionen pruefen (letzte Suche " + (f"vor {age:.0f} h" if age is not None else "unbekannt") + ")")
        info = self.detect(self.master()) if self.master().get("host") else None
        try:
            self.repo.check_checkmk(info.get("checkmk") if info else None)
        except Exception as e:     # die Updates selbst sollen trotzdem laufen
            self.log.warn(f"Versionssuche fehlgeschlagen: {e}")

    def maintenance(self, force=False):
        s = self.cfg["schedule"]
        inside, _ = window_info(s)
        if not inside and not force:
            self.log.warn("Ausserhalb des Wartungsfensters – nichts zu tun (mit --force trotzdem starten).")
            return True
        self.enforce_window = not force
        self.log.step(f"Wartungslauf {now():%d.%m.%Y %H:%M}")
        ok = True
        if s.get("os_update", True):
            ok = self.os_update_all(include_master=s.get("include_master_os", True)) and ok
        if s.get("checkmk_update", True):
            self._fresh_checkmk_check()
            ok = self.checkmk_update_all() and ok
        if not self.dry:
            update_state(lambda st: st.update({"last_maintenance": {
                "time": now().isoformat(timespec="seconds"), "ok": ok,
                "log": str(self.log.path or "")}}))
        return ok


def last_inventory():
    return load_state().get("inventory", {})
