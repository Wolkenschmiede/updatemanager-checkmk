"""Labormodus: Testsysteme gezielt auf einen alten Stand zuruecksetzen, um die Automatik zu pruefen.

Nur fuer Testumgebungen! Muss im Terminal eingeschaltet werden:  updatemanager-checkmk lab on

Checkmk:  Site auf eine aeltere Version desselben Zweigs bringen.
          1. Backup des aktuellen Stands (Rueckweg)
          2. Zielversion ins Repo laden und auf dem System installieren
          3. vorhandenes Backup dieser Version zuruecksetzen – sonst omd update auf die aeltere Version
             (von Checkmk offiziell nicht unterstuetzt; misslingt es, wird das Backup aus 1. zurueckgespielt)
          4. Zielversion als Standard setzen, neuere Versionen entfernen (omd cleanup)
OS:       harmlose Pakete (nano, curl, wget ...) auf die Release-Version der Distribution zuruecksetzen.
          Die Pakete laedt der Update-Server aus dem Internet (apt-get download) und kopiert sie per scp.
          Danach zeigen die Systeme echte offene Updates, die der naechste Lauf einspielen muss.
"""
import os
import re
import shlex
import shutil
from pathlib import Path

from .common import STATE_DIR, now, record, run, stamp, update_state, vbase, vparse
from .osprofiles import edition_of_site_version, parse_os_release

# Gruppen, die nur gemeinsam zurueckgesetzt werden koennen (gleiche Quellversion)
OS_GROUPS = [
    ["nano"],
    ["curl", "libcurl4t64"],
    ["wget"],
    ["less"],
    ["rsync"],
    ["jq", "libjq1"],
    ["tzdata"],
    ["vim-tiny", "vim-common"],
]
DEB_DIR = STATE_DIR / "labor-pakete"


def enabled(cfg):
    return bool(cfg.get("lab", {}).get("enabled"))


class Lab:
    def __init__(self, upd):
        self.upd, self.cfg, self.log = upd, upd.cfg, upd.log
        self.remote, self.repo, self.dry = upd.remote, upd.repo, upd.dry

    # ============================================================ Versionen
    def older_versions(self, current, limit=40):
        """Aeltere Versionen desselben Zweigs beim Hersteller (neueste zuerst)."""
        t = vparse(current or "")
        if not t:
            return []
        res = []
        for p in range(t[3] - 1, -1, -1):
            cand = f"{t[0]}.{t[1]}.{t[2]}" + (f"p{p}" if p else "")
            try:
                if self.repo.remote_exists(cand):
                    res.append(cand)
            except ConnectionError as e:
                self.log.err(f"Download-Server nicht erreichbar: {e}")
                break
            if len(res) >= limit:
                break
        return res

    def backups(self, t):
        """Backups dieser Site auf dem System: {version: dateiname} (neuestes je Version)."""
        rc, out = self.remote.out(t, f"ls -1t ~/updatemanager-backups/{t['site']}_*.tar.gz 2>/dev/null")
        res = {}
        for line in out.splitlines() if rc == 0 else []:
            m = re.search(rf"/{re.escape(t['site'])}_(\d+\.\d+\.\d+(?:p\d+)?)_\d{{8}}-\d{{6}}\.tar\.gz$", line)
            if m and m.group(1) not in res:
                res[m.group(1)] = line.strip()
        return res

    # ============================================================ Checkmk zuruecksetzen
    def checkmk_back(self, t, target):
        name, site = t["name"], t["site"]
        errs, warns, info, prof = self.upd.preflight(t, need_site=True)
        if errs:
            return self.upd._fail(name, "lab", "Vorpruefung: " + "; ".join(errs))
        cur = info["checkmk"]
        cur_base = vbase(cur)
        ed = self.cfg["checkmk"]["edition"]
        if vparse(target) >= vparse(cur_base):
            return None, f"Checkmk {cur_base} ist nicht neuer als {target} – uebersprungen"
        if vparse(target)[:3] != vparse(cur_base)[:3]:
            return self.upd._fail(name, "lab", f"{target} ist ein anderer Zweig als {cur_base} – nur innerhalb eines "
                                               "Zweigs moeglich")
        if info.get("edition") != ed:
            return self.upd._fail(name, "lab", f"Edition der Site ({info.get('edition')}) passt nicht zu {ed}")
        self.log.step(f"Labor: {name} / Site {site}  Checkmk {cur_base} -> {target}")

        # 1. Rueckweg sichern
        safety = f"~/updatemanager-backups/{site}_{cur_base}_{stamp()}.tar.gz"
        self.log.info(f"{name}: Sicherung des aktuellen Stands -> {safety}")
        rc, _ = self.remote.run(t, f"mkdir -p ~/updatemanager-backups && sudo omd backup -N {site} - > {safety}",
                                timeout=3600)
        if rc:
            return self.upd._fail(name, "lab", "Sicherung fehlgeschlagen – nichts veraendert")

        # 2. Zielversion bereitstellen
        res = self.repo.fetch_version(target, [prof])
        if res.get(prof["id"]) != "ok":
            return self.upd._fail(name, "lab", f"Checkmk {target} konnte nicht ins Repo geladen werden")
        pkg = f"check-mk-{ed}-{target}"
        from .osprofiles import adapter
        rc, _ = self.remote.run(t, adapter(info["family"])["install"].format(pkg=pkg), tunnel=True, timeout=3600)
        if rc:
            return self.upd._fail(name, "lab", f"Paket {pkg} konnte nicht installiert werden")
        rc, out = self.remote.out(t, f"ls /opt/omd/versions | grep -E '^{re.escape(target)}\\.'")
        full = next((x for x in out.split() if edition_of_site_version(x) == ed), None) or \
            f"{target}.{cur[len(cur_base):].lstrip('.')}"

        # 3. Zuruecksetzen: vorhandenes Backup dieser Version bevorzugen
        bk = self.backups(t).get(target)
        self.remote.run(t, f"sudo omd stop {site}", timeout=600)
        if bk:
            self.log.info(f"{name}: Backup von {target} gefunden – wird zurueckgesetzt ({bk.split('/')[-1]})")
            rc, _ = self.remote.run(t, f"sudo omd restore --reuse --kill {site} {shlex.quote(bk)}", timeout=3600)
        else:
            self.log.warn(f"{name}: kein Backup von {target} vorhanden – versuche omd update auf die aeltere Version "
                          "(von Checkmk nicht offiziell unterstuetzt)")
            rc, _ = self.remote.run(t, f"sudo omd -V {full} update --pre-flight=ignore --skeleton=install "
                                       f"--confirm-version --ignore-versions-incompatible {site}", timeout=3600)
        self.remote.run(t, f"sudo omd start {site}", timeout=600)
        new = full if self.dry else self.upd.site_version(t)
        if rc or new != full:
            self.log.err(f"{name}: Zuruecksetzen fehlgeschlagen (rc {rc}, Version {new}) – spiele Sicherung zurueck")
            self.remote.run(t, f"sudo omd stop {site}", timeout=600)
            self.remote.run(t, f"sudo omd restore --reuse --kill {site} {safety}", timeout=3600)
            self.remote.run(t, f"sudo omd start {site}", timeout=600)
            return self.upd._fail(name, "lab", f"Checkmk {target} nicht moeglich – Site laeuft wieder auf {cur_base}")

        # 4. Zielversion als Standard, neuere Versionen entfernen
        self.remote.run(t, f"sudo omd setversion {full}", timeout=120)
        self.remote.run(t, "sudo omd cleanup", timeout=1800)
        if not self.dry:
            update_state(lambda s: s.setdefault("inventory", {}).setdefault(name, {}).update(
                {"checkmk": full, "edition": ed}))
        record(name, "lab", True, f"Checkmk {cur_base} -> {target} (Labor)")
        self.log.ok(f"{name}: Site laeuft jetzt auf {target}")
        return True, f"Checkmk {cur_base} -> {target}"

    # ============================================================ OS zuruecksetzen
    def _manager_os(self):
        try:
            return parse_os_release(Path(os.environ.get("UMC_OS_RELEASE", "/etc/os-release")).read_text())
        except OSError:
            return {}

    def _release_version(self, pkg):
        """Aelteste verfuegbare Version eines Pakets laut apt-Quellen des Update-Servers."""
        rc, out = run(["apt-cache", "madison", pkg], mutating=False, quiet=True, timeout=60)
        vers = [ln.split("|")[1].strip() for ln in out.splitlines() if ln.count("|") >= 2] if rc == 0 else []
        return vers[-1] if vers else None

    def os_back(self, t):
        name = t["name"]
        info = self.upd.detect(t)
        if not info:
            return self.upd._fail(name, "lab", "SSH nicht erreichbar")
        mos = self._manager_os()
        if info.get("family") != "apt" or mos.get("VERSION_CODENAME") != info.get("codename") \
                or mos.get("ID") != info.get("os_id"):
            return None, (f"OS-Rueckstellung nur moeglich, wenn der Update-Server dasselbe System hat "
                          f"({info.get('label')} vs. {mos.get('PRETTY_NAME', '?')}) – uebersprungen")
        if not shutil.which("apt-get"):
            return None, "apt-get fehlt auf dem Update-Server – uebersprungen"
        allpk = [p for g in OS_GROUPS for p in g]
        rc, out = self.remote.out(t, "dpkg-query -W -f='${Package} ${Version}\\n' " + " ".join(allpk) + " 2>/dev/null")
        installed = dict(ln.split(" ", 1) for ln in out.splitlines() if " " in ln)
        dest = DEB_DIR / info["codename"]
        dest.mkdir(parents=True, exist_ok=True)
        done, skipped = [], []
        rdir = "/tmp/updatemanager-labor"
        self.remote.run(t, f"rm -rf {rdir} && mkdir -p {rdir}", quiet=True)
        for group in OS_GROUPS:
            if not all(p in installed for p in group):
                continue
            want = {p: self._release_version(p) for p in group}
            if any(v is None for v in want.values()) or all(want[p] == installed[p] for p in group):
                skipped.append(group[0])
                continue
            files = []
            for p, v in want.items():
                if not list(dest.glob(f"{p}_*.deb")) or not any(self._deb_version(f) == v for f in dest.glob(f"{p}_*.deb")):
                    rc, o = run(["apt-get", "download", f"{p}={v}"], self.log, mutating=False, quiet=True, timeout=300,
                                cwd=str(dest))
                    if rc:
                        self.log.warn(f"  {p}={v} nicht ladbar: {o.strip()[-120:]}")
                files += [f for f in dest.glob(f"{p}_*.deb") if self._deb_version(f) == v]
            if len(files) < len(group):
                skipped.append(group[0])
                continue
            if self.remote.scp(t, files, rdir)[0]:
                skipped.append(group[0])
                continue
            debs = " ".join(f"{rdir}/{f.name}" for f in files)
            rc, _ = self.remote.run(t, "export DEBIAN_FRONTEND=noninteractive; sudo apt-get -q -y "
                                       f"--allow-downgrades install {debs}", timeout=600)
            (done if rc == 0 else skipped).append(" ".join(f"{p}={want[p]}" for p in group))
        self.remote.run(t, f"rm -rf {rdir}", quiet=True)
        if not done:
            return None, "keine Pakete zurueckgesetzt (" + (", ".join(skipped) or "nichts passend") + ")"
        record(name, "lab", True, f"OS: {len(done)} Paketgruppen auf Release-Stand")
        self.log.ok(f"{name}: zurueckgesetzt: {', '.join(done)}")
        return True, f"OS: {len(done)} Paketgruppen auf Release-Stand"

    @staticmethod
    def _deb_version(f):
        # Dateiname: paket_version_arch.deb (Doppelpunkt der Epoche als %3a)
        parts = f.name[:-4].split("_")
        return parts[1].replace("%3a", ":") if len(parts) >= 3 else None

    # ============================================================ Ablauf
    def rollback(self, only, checkmk=None, os_pkgs=False, clear_repo=False):
        targets = [c for c in self.upd.customers(only)]
        m = self.upd.master()
        if m.get("host") and (not only or m["name"] in only):
            targets.append(m)                       # Master zuletzt
        if not targets:
            self.log.err("Keine Systeme ausgewaehlt")
            return False
        if checkmk and not re.fullmatch(r"\d+\.\d+\.\d+(?:p\d+)?", checkmk):
            self.log.err("Version im Format 2.5.0p12")
            return False
        self.log.step("Labor-Rollback: " + ", ".join(t["name"] for t in targets)
                      + (f"  Checkmk -> {checkmk}" if checkmk else "") + ("  OS-Pakete -> Release-Stand" if os_pkgs else ""))
        results = {}
        for t in targets:
            msgs, state = [], True
            if os_pkgs:
                r = self.upd._safe(self.os_back, t)
                msgs.append(r[1])
                state = state if r[0] is not False else False
            if checkmk and t.get("site"):
                r = self.upd._safe(self.checkmk_back, t, checkmk)
                msgs.append(r[1])
                state = state if r[0] is not False else False
            results[t["name"]] = (state, "; ".join(msgs) or "nichts zu tun")
        if clear_repo:
            self.log.step("Checkmk-Pakete aus dem Repo entfernen (der naechste Repo-Sync laedt neu)")
            for u in self.repo.checkmk_usage():
                p = self.repo.profile(u["profile"])
                if p:
                    self.repo.delete_checkmk(p, u["edition"], u["version"])
        self.log.step("Systeme neu erfassen")
        self.upd.inventory_all(only, pending=True, packages=False)
        self.upd.summary("Labor-Rollback", results)
        self.log.info(f"Fertig {now():%H:%M}. Der naechste Repo-Sync und das Wartungsfenster sollten alles wieder "
                      "aktualisieren.")
        return all(r[0] is not False for r in results.values())
