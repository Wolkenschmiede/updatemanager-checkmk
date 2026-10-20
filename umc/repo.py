"""Offline-Repository: OS-Spiegel je Betriebssystem (aptly / reposync) und Checkmk-Pakete.

Aufbau unter <root>/aptly/public (per nginx ausgeliefert):
  /<distro>/dists/<codename>/...          apt-Spiegel (Ubuntu, Debian) – aptly, Snapshots
  /checkmk/dists/<codename>/...           Checkmk-Pakete fuer apt-Systeme – aptly
  /rpm/<profil>/current/<repo>/           rpm-Spiegel (EL, SUSE) – Hardlink-Snapshots, 'current' = Symlink
  /rpm/<profil>/checkmk/                  Checkmk-Pakete fuer rpm-Systeme – createrepo_c, signiert
"""
import base64
import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import (HOME, STATE_DIR, dim, expand, load_state, now, run, stamp,
                     update_state, vbase, vfmt, vparse)
from .osprofiles import checkmk_file_fallback, checkmk_file_regex

PKG_DIR = STATE_DIR / "pakete"
DL_BASE = os.environ.get("UMC_DL_BASE", "https://download.checkmk.com/checkmk")
STAMP_RE = r"(\d{8}-\d{4,6})"
DEB_RE = re.compile(r"^check-mk-([a-z]+)-(\d+\.\d+\.\d+(?:p\d+)?)_0\.([a-z0-9]+)_amd64\.deb$")
RPM_RE = re.compile(r"^check-mk-([a-z]+)-(\d+\.\d+\.\d+(?:p\d+)?)-([a-z0-9]+)-\d+\.x86_64\.rpm$")


def inventory():
    return load_state().get("inventory", {})


class Repo:
    def __init__(self, cfg, log, dry_run=False):
        self.cfg, self.log, self.dry = cfg, log, dry_run
        self.r = cfg["repo"]
        self._index_cache = {}
        self.read_timeout = None        # Weboberflaeche: lesende aptly-Aufrufe begrenzen

    # ============================================================ Pfade / Werkzeuge
    @property
    def root(self):
        return Path(expand(self.r["root"]))

    @property
    def cmk_dir(self):
        return self.root / "checkmk"

    @property
    def public_dir(self):
        return self.root / "aptly" / "public"

    @property
    def aptly_conf(self):
        return HOME / "conf" / "aptly.conf"

    def write_aptly_conf(self):
        self.aptly_conf.parent.mkdir(parents=True, exist_ok=True)
        self.aptly_conf.write_text(json.dumps({
            "rootDir": str(self.root / "aptly"),
            "architectures": ["amd64"],
            "downloadConcurrency": 4,
        }, indent=2) + "\n")

    def aptly(self, *args, mutating=True, quiet=False, on_line=None, timeout=None):
        if not self.aptly_conf.exists():
            self.write_aptly_conf()
        if timeout is None and not mutating:
            timeout = self.read_timeout
        return run(["aptly", f"-config={self.aptly_conf}", *args], self.log,
                   mutating=mutating, dry_run=self.dry, quiet=quiet, prefix="  ", on_line=on_line, timeout=timeout)

    def _gpg(self):
        k = self.r.get("gpg_key")
        return [f"-gpg-key={k}", "-batch"] if k else []

    def _progress(self, pattern=r"Success downloading \S+\.deb"):
        """Fortschrittsanzeige statt tausender Einzelzeilen."""
        st = {"n": 0, "total": 0}

        def cb(line):
            m = re.search(r"Download queue: (\d+) items \(([^)]*)\)", line)
            if m:
                st["total"] = int(m.group(1))
                self.log.info(f"  {m.group(1)} Pakete zu laden ({m.group(2)})")
            elif re.search(pattern, line):
                st["n"] += 1
                step = max(25, st["total"] // 10) if st["total"] else 100
                if st["n"] % step == 0:
                    self.log.info(f"  ... {st['n']}" + (f"/{st['total']}" if st["total"] else "") + " geladen")
        return cb

    # ============================================================ Profile
    def profiles(self, only_used=True):
        """Aktive OS-Profile; standardmaessig nur die, die ein erfasstes System nutzt."""
        profs = [p for p in self.cfg["os_profiles"].values() if p.get("enabled", True)]
        if not only_used:
            return profs
        used = {i.get("profile") for i in inventory().values()}
        if not used:
            return profs
        return [p for p in profs if p["id"] in used]

    def profile(self, pid):
        return self.cfg["os_profiles"].get(pid)

    def hosts_of(self, pid):
        return [n for n, i in inventory().items() if i.get("profile") == pid]

    # ============================================================ Paketlisten
    def refresh_package_lists(self, remote, targets):
        """Installierte Pakete jedes Systems (Grundlage fuer den apt-Filter)."""
        PKG_DIR.mkdir(parents=True, exist_ok=True)
        ok = True
        cmd = ("if command -v dpkg-query >/dev/null 2>&1; then dpkg-query -W -f='${Package}\\n'; "
               "else rpm -qa --qf '%{NAME}\\n'; fi")
        for t in targets:
            rc, out = remote.out(t, cmd, timeout=120)
            if rc != 0 or not out:
                self.log.err(f"  {t['name']}: Paketliste nicht abrufbar")
                ok = False
                continue
            names = sorted(set(out.split()))
            (PKG_DIR / f"{t['name']}.txt").write_text("\n".join(names) + "\n")
            self.log.ok(f"  {t['name']}: {len(names)} Pakete")
        return ok

    def package_names(self, p):
        names = set()
        files = [PKG_DIR / f"{h}.txt" for h in self.hosts_of(p["id"])]
        files.append(PKG_DIR / "import.txt")           # Uebernahme aus der Skript-Variante
        for f in files:
            if f.exists():
                names.update(x.strip() for x in f.read_text().splitlines() if x.strip() and not x.startswith("#"))
        names.update(self.checkmk_deps(p))
        return names

    def checkmk_deps(self, p):
        deps = set()
        for deb in (self.cmk_dir / p["id"]).glob("*.deb"):
            rc, out = run(["dpkg-deb", "-f", str(deb), "Depends"], mutating=False, quiet=True)
            if rc:
                continue
            for part in out.replace("\n", " ").split(","):
                for alt in part.split("|"):
                    n = alt.strip().split(" ")[0].split(":")[0].strip()
                    if n:
                        deps.add(n)
        return deps

    def build_filter(self, p):
        if self.r.get("filter_mode") == "full":
            return ""
        return " | ".join(sorted(self.package_names(p)))

    # ============================================================ Gesamt-Sync
    def sync(self):
        profs = self.profiles()
        if not profs:
            self.log.err("Keine Betriebssysteme bekannt – zuerst 'Systeme erfassen' (Menue Repository).")
            return False
        ok = True
        for p in profs:
            self.log.step(f"Spiegel {p['label']} ({p['id']})")
            try:
                good = self.sync_apt(p) if p["family"] == "apt" else self.sync_rpm(p)
            except Exception as e:      # ein Profil darf die anderen nicht aufhalten
                self.log.err(f"{p['id']}: unerwarteter Fehler: {e}")
                good = False
            ok = good and ok
            self.ensure_checkmk_repo(p)
        self.export_key()
        if self.aptly_conf.exists() or self.dry:
            self.aptly("db", "cleanup", quiet=True)
        self.remember_snapshots()
        if ok and not self.dry:
            update_state(lambda s: s.update({"last_sync": {"time": now().isoformat(timespec="seconds"),
                                                           "profiles": [p["id"] for p in profs]}}))
        return ok

    # ============================================================ apt (aptly)
    def mirror_exists(self, name):
        return self.aptly("mirror", "show", name, mutating=False, quiet=True)[0] == 0

    def sync_apt(self, p):
        filt = self.build_filter(p)
        if self.r.get("filter_mode") != "full" and not filt:
            self.log.err(f"{p['id']}: keine Paketlisten – zuerst 'Systeme erfassen'.")
            return False
        if not p.get("mirrors"):
            self.log.err(f"{p['id']}: keine Spiegel-Adressen eingetragen")
            return False
        fhash = hashlib.sha1(filt.encode()).hexdigest()
        known = load_state().get("mirror_filter", {})
        ts = stamp()
        sources, done = [], {}
        retries = max(1, int(self.r["sync_retries"]))
        for m in p["mirrors"]:
            name = f"{p['distro']}-{m['dist']}"
            fargs = [f"-filter={filt}", "-filter-with-deps"] if filt else []
            if not self.mirror_exists(name):
                self.log.info(f"Mirror {name} anlegen")
                if self.aptly("mirror", "create", "-architectures=amd64", *fargs, name,
                              m["url"], m["dist"], *m.get("components", ["main"]))[0]:
                    self.log.err(f"Mirror {name} konnte nicht angelegt werden (Signaturschluessel importiert?)")
                    return False
            elif known.get(name) != fhash:
                self.log.info(f"Paketfilter fuer {name} aktualisieren")
                if self.aptly("mirror", "edit", *(fargs or ["-filter="]), name)[0]:
                    return False
            for attempt in range(1, retries + 1):
                self.log.info(f"Mirror {name} aktualisieren (Versuch {attempt}/{retries})")
                rc, out = self.aptly("mirror", "update", name, quiet=True, on_line=self._progress(),
                                     timeout=int(self.r.get("sync_timeout_min", 240)) * 60)
                if rc == -9:
                    out += f"\nAbgebrochen: Zeitlimit von {self.r.get('sync_timeout_min', 240)} Minuten ueberschritten"
                if rc == 0:
                    n_new = len(re.findall(r"Success downloading \S+\.deb", out))
                    self.log.info(f"  {name}: {n_new} Pakete neu geladen")
                    break
                for line in out.strip().splitlines()[-8:]:
                    self.log.info(f"  {line}")
                if attempt < retries:
                    self.log.warn(f"Fehlgeschlagen – neuer Versuch in {self.r['sync_retry_wait_s']} s")
                    if not self.dry:
                        time.sleep(int(self.r["sync_retry_wait_s"]))
            else:
                self.log.err(f"Mirror {name} konnte nicht aktualisiert werden")
                return False
            snap = f"src-{name}-{ts}"
            if self.aptly("snapshot", "create", snap, "from", "mirror", name)[0]:
                return False
            sources.append(snap)
            done[name] = fhash
        merged = f"pub-{p['id']}-{ts}"
        if self.aptly("snapshot", "merge", "-latest", merged, *sources)[0]:
            return False
        if not self.publish_snapshot(p, merged):
            return False
        if not self.dry:
            update_state(lambda s: s.setdefault("mirror_filter", {}).update(done))
        self.cleanup_snapshots(p)
        self.log.ok(f"{p['id']}: veroeffentlicht ({merged})")
        return True

    def published(self):
        rc, out = self.aptly("publish", "list", "-raw", mutating=False, quiet=True)
        if rc:
            return set()
        return {tuple(x.split()[:2]) for x in out.splitlines() if len(x.split()) >= 2}

    def publish_snapshot(self, p, snap):
        if (p["distro"], p["codename"]) in self.published():
            rc, _ = self.aptly("publish", "switch", *self._gpg(), p["codename"], p["distro"], snap)
        else:
            rc, _ = self.aptly("publish", "snapshot", *self._gpg(), f"-distribution={p['codename']}",
                               "-component=main", snap, p["distro"])
        if rc:
            self.log.err("Veroeffentlichen fehlgeschlagen")
        return rc == 0

    def _snap_stamp(self, p, name):
        for pat in (rf"^pub-{re.escape(p['id'])}-{STAMP_RE}$", rf"^{re.escape(p['distro'])}-{STAMP_RE}$"):
            m = re.match(pat, name)
            if m:
                return m.group(1)
        return None

    def snapshots(self, p):
        """Veroeffentlichbare Staende eines Profils, aelteste zuerst."""
        if p["family"] != "apt":
            d = self.public_dir / "rpm" / p["id"] / "snapshots"
            return sorted(x.name for x in d.iterdir()) if d.exists() else []
        rc, out = self.aptly("snapshot", "list", "-raw", mutating=False, quiet=True)
        names = [n for n in out.split() if self._snap_stamp(p, n)] if rc == 0 else []
        return sorted(names, key=lambda n: self._snap_stamp(p, n))

    def remember_snapshots(self):
        """Repo-Staende fuer die Weboberflaeche merken (dort ohne aptly-Aufruf anzeigbar)."""
        if self.dry:
            return
        data = {}
        for p in self.cfg["os_profiles"].values():
            try:
                data[p["id"]] = {"snapshots": self.snapshots(p), "current": self.current_snapshot(p)}
            except Exception:
                continue
        update_state(lambda s: s.update({"repo_snapshots": {"time": now().isoformat(timespec="seconds"), **data}}))

    def current_snapshot(self, p):
        if p["family"] != "apt":
            cur = self.public_dir / "rpm" / p["id"] / "current"
            return os.readlink(cur).split("/")[-1] if cur.is_symlink() else None
        rc, out = self.aptly("publish", "show", p["codename"], p["distro"], mutating=False, quiet=True)
        m = re.search(r"main:\s+(\S+)", out) if rc == 0 else None
        return m.group(1) if m else None

    def rollback(self, p, snap):
        self.log.info(f"{p['id']}: veroeffentliche aelteren Stand {snap}")
        ok = self.publish_snapshot(p, snap) if p["family"] == "apt" else self._rpm_switch(p, snap)
        self.remember_snapshots()
        return ok

    def cleanup_snapshots(self, p, keep=None):
        keep = int(keep or self.r["keep_snapshots"])
        snaps = self.snapshots(p)
        current = self.current_snapshot(p)
        keep_set = set(snaps[-keep:]) | ({current} if current else set())
        dropped = 0
        if p["family"] != "apt":
            for s in snaps:
                if s not in keep_set:
                    if not self.dry:
                        shutil.rmtree(self.public_dir / "rpm" / p["id"] / "snapshots" / s, ignore_errors=True)
                    dropped += 1
        else:
            stamps = {self._snap_stamp(p, n) for n in keep_set if self._snap_stamp(p, n)}
            for n in snaps:
                if n not in keep_set and self.aptly("snapshot", "drop", n, quiet=True)[0] == 0:
                    dropped += 1
            rc, out = self.aptly("snapshot", "list", "-raw", mutating=False, quiet=True)
            for n in out.split():
                for m in p["mirrors"]:
                    base = re.escape(f"{p['distro']}-{m['dist']}")
                    mm = re.match(rf"^(?:src-)?{base}-{STAMP_RE}$", n)
                    if mm and mm.group(1) not in stamps and self.aptly("snapshot", "drop", n, quiet=True)[0] == 0:
                        dropped += 1
        if dropped:
            self.log.info(f"{p['id']}: {dropped} alte Staende entfernt (behalten: {keep})")
        return dropped

    # ============================================================ rpm (reposync)
    def _rpm_paths(self, p):
        base = self.root / "rpm-mirror" / p["id"]
        pub = self.public_dir / "rpm" / p["id"]
        return base, pub

    def sync_rpm(self, p):
        repos = [r for r in p.get("repos", []) if r.get("enabled", True) and r.get("url")]
        if not repos:
            self.log.err(f"{p['id']}: keine Repo-URLs – im Menue Repository -> Betriebssysteme eintragen. {p.get('note', '')}")
            return False
        if not shutil.which("dnf") and not self.dry:
            self.log.err("dnf fehlt auf dem Update-Server – Menue 9 -> 2 (RPM-Werkzeuge installieren)")
            return False
        base, pub = self._rpm_paths(p)
        conf = base / "dnf.conf"
        if not self.dry:
            base.mkdir(parents=True, exist_ok=True)
            lines = ["[main]", "reposdir=/dev/null", "gpgcheck=0", f"cachedir={base / '.cache'}",
                     "keepcache=0", "skip_if_unavailable=False", ""]
            for r in repos:
                lines += [f"[{r['name']}]", f"name={r['name']}", f"baseurl={r['url']}", "enabled=1", ""]
            conf.write_text("\n".join(lines))
        retries = max(1, int(self.r["sync_retries"]))
        rel = (p.get("version") or "").split(".")[0] or "9"
        for r in repos:
            for attempt in range(1, retries + 1):
                self.log.info(f"Repo {r['name']} spiegeln (Versuch {attempt}/{retries})")
                rc, out = run(["dnf", "-c", str(conf), f"--releasever={rel}", "-q", "reposync",
                               f"--repoid={r['name']}", "--download-metadata", "--newest-only", "--delete",
                               "--norepopath", f"--download-path={base / r['name']}"],
                              self.log, dry_run=self.dry, quiet=True,
                              timeout=int(self.r.get("sync_timeout_min", 240)) * 60)
                if rc == 0:
                    break
                for line in out.strip().splitlines()[-8:]:
                    self.log.info(f"  {line}")
                if attempt < retries and not self.dry:
                    time.sleep(int(self.r["sync_retry_wait_s"]))
            else:
                self.log.err(f"{p['id']}/{r['name']}: Spiegeln fehlgeschlagen")
                return False
        ts = stamp()
        if not self.dry:
            dest = pub / "snapshots" / ts
            dest.mkdir(parents=True, exist_ok=True)
            for r in repos:
                if run(["cp", "-al", str(base / r["name"]), str(dest / r["name"])], self.log)[0]:
                    self.log.err("Snapshot konnte nicht angelegt werden")
                    return False
        if not self._rpm_switch(p, ts):
            return False
        self.cleanup_snapshots(p)
        self.log.ok(f"{p['id']}: veroeffentlicht ({ts})")
        return True

    def _rpm_switch(self, p, ts):
        _, pub = self._rpm_paths(p)
        if self.dry:
            self.log.info(dim(f"[DRY-RUN] {pub}/current -> snapshots/{ts}"))
            return True
        if not (pub / "snapshots" / ts).exists():
            self.log.err(f"Stand {ts} existiert nicht")
            return False
        tmp = pub / ".current.new"
        if tmp.is_symlink():
            tmp.unlink()
        tmp.symlink_to(f"snapshots/{ts}")
        os.replace(tmp, pub / "current")
        return True

    # ============================================================ Schluessel
    def export_key(self):
        k = self.r.get("gpg_key")
        if not k or self.dry:
            return None
        rc, out = run(["gpg", "--armor", "--export", k], mutating=False, quiet=True)
        if rc or "BEGIN PGP PUBLIC KEY" not in out:
            return None
        self.public_dir.mkdir(parents=True, exist_ok=True)
        (self.public_dir / "repo-key.asc").write_text(out)
        return out

    # ============================================================ Checkmk-Pakete
    def migrate_checkmk_layout(self):
        """Version 0.1: Pakete lagen direkt in checkmk/, Repo hiess 'checkmk' (nur Ubuntu)."""
        for f in list(self.cmk_dir.glob("check-mk-*.deb")):
            m = DEB_RE.match(f.name)
            prof = next((p for p in self.cfg["os_profiles"].values()
                         if p["family"] == "apt" and m and p["cmk_tag"] == m[3]), None)
            if prof and not self.dry:
                (self.cmk_dir / prof["id"]).mkdir(parents=True, exist_ok=True)
                f.rename(self.cmk_dir / prof["id"] / f.name)

    def _apt_cmk_repo(self, p):
        return f"checkmk-{p['codename']}"

    def ensure_checkmk_repo(self, p):
        """Checkmk-Repo eines Profils anlegen/aktualisieren und veroeffentlichen (auch leer)."""
        self.migrate_checkmk_layout()
        if p["family"] == "apt":
            name, cn = self._apt_cmk_repo(p), p["codename"]
            if self.aptly("repo", "show", name, mutating=False, quiet=True)[0] != 0:
                if self.aptly("repo", "show", "checkmk", mutating=False, quiet=True)[0] == 0 and \
                        ("checkmk", cn) in self.published():
                    self.aptly("repo", "rename", "checkmk", name, quiet=True)     # Uebernahme aus 0.1
                else:
                    self.aptly("repo", "create", f"-distribution={cn}", "-component=main", name, quiet=True)
            if ("checkmk", cn) not in self.published():
                return self.aptly("publish", "repo", *self._gpg(), "-architectures=amd64",
                                  f"-distribution={cn}", name, "checkmk", quiet=True)[0] == 0
            return self.aptly("publish", "update", *self._gpg(), cn, "checkmk", quiet=True)[0] == 0
        # rpm: Verzeichnis + createrepo_c + signierte Metadaten
        d = self.public_dir / "rpm" / p["id"] / "checkmk"
        if self.dry:
            self.log.info(dim(f"[DRY-RUN] createrepo_c {d}"))
            return True
        if not shutil.which("createrepo_c"):
            return False
        d.mkdir(parents=True, exist_ok=True)
        for f in (self.cmk_dir / p["id"]).glob("*.rpm"):
            if not (d / f.name).exists():
                os.link(f, d / f.name)
        if run(["createrepo_c", "-q", "--update", str(d)], self.log)[0]:
            return False
        k = self.r.get("gpg_key")
        if k:
            run(["gpg", "--batch", "--yes", "-u", k, "--detach-sign", "--armor",
                 str(d / "repodata" / "repomd.xml")], self.log, quiet=True)
        return True

    def checkmk_files(self, p):
        res = []
        for f in (self.cmk_dir / p["id"]).glob("check-mk-*"):
            m = (DEB_RE if p["family"] == "apt" else RPM_RE).match(f.name)
            if m and m[3] == p["cmk_tag"]:
                res.append({"edition": m[1], "version": m[2], "file": f})
        return sorted(res, key=lambda d: vparse(d["version"]))

    def repo_versions(self, p):
        ed = self.cfg["checkmk"]["edition"]
        return [d["version"] for d in self.checkmk_files(p) if d["edition"] == ed]

    def all_versions(self):
        vs = set()
        for p in self.cfg["os_profiles"].values():
            vs.update(self.repo_versions(p))
        return sorted(vs, key=vparse)

    def in_repo(self, p, ver):
        if ver not in self.repo_versions(p):
            return False
        if p["family"] == "apt":
            rc, out = self.aptly("repo", "show", "-with-packages", self._apt_cmk_repo(p), mutating=False, quiet=True)
            return rc == 0 and f"check-mk-{self.cfg['checkmk']['edition']}-{ver}_" in out
        return (self.public_dir / "rpm" / p["id"] / "checkmk").exists()

    # ------------------------------------------------------------ Download
    # ============================================================ Checkmk-Pakete: Nutzung und Bereinigung
    def checkmk_usage(self):
        """Alle Checkmk-Pakete im Repo mit Nutzung und Loesch-Empfehlung.

        Ein Paket gilt als entbehrlich, wenn es keine Site mehr braucht:
          - andere Edition als eingestellt und von keiner Site genutzt, oder
          - alle Sites auf diesem Betriebssystem laufen bereits auf einer neueren Version und
            die Version ist aelter als die des Masters (die bleibt fuer neue Kunden im Repo).
        Ist von einem System mit Site die Version unbekannt, wird auf dessen OS nichts als entbehrlich markiert.
        """
        inv = inventory()
        ck = self.cfg["checkmk"]
        ed = ck["edition"]
        m = self.cfg["master"]
        systems = [dict(c, role="kunde") for c in self.cfg["customers"] if c.get("enabled", True)]
        if m.get("host"):
            systems.append(dict(m, role="master"))
        master_base = vbase((inv.get(m.get("name"), {}) or {}).get("checkmk") or "")
        unknown_profile = [s["name"] for s in systems if s.get("site") and not (inv.get(s["name"]) or {}).get("profile")]
        out = []
        for p in self.cfg["os_profiles"].values():
            sites = []
            for s in systems:
                i = inv.get(s["name"]) or {}
                if s.get("site") and i.get("profile") == p["id"]:
                    sites.append((s["name"], vbase(i.get("checkmk") or ""), i.get("edition")))
            for d in self.checkmk_files(p):
                v, e = d["version"], d["edition"]
                users = [n for n, sv, se in sites if sv == v and se == e]
                removable, reason = False, ""
                if users:
                    reason = "installiert auf " + ", ".join(users)
                elif unknown_profile:
                    reason = "Betriebssystem unbekannt bei: " + ", ".join(unknown_profile)
                elif any(not sv for _, sv, _ in sites):
                    reason = "Checkmk-Version unbekannt bei: " + ", ".join(n for n, sv, _ in sites if not sv)
                elif e != ed and master_base and vparse(v) < vparse(master_base):
                    removable, reason = True, f"Edition {e}, von keiner Site genutzt und aelter als der Master"
                elif e != ed:
                    reason = f"andere Edition ({e}) – bleibt fuer einen Editionswechsel"
                elif master_base and vparse(v) >= vparse(master_base):
                    reason = ("Version des Masters (bleibt fuer neue Kunden)" if v == master_base
                              else "neuer als der Master (Update steht an)")
                elif not master_base:
                    reason = "Version des Masters unbekannt"
                elif any(vparse(sv) <= vparse(v) for _, sv, _ in sites):
                    reason = "wird noch fuer ein Update gebraucht"
                else:
                    removable = True
                    reason = ("alle Sites auf neuerer Version" if sites else "aelter als der Master, keine Site auf diesem OS")
                f = d["file"]
                out.append({"profile": p["id"], "label": p.get("label", p["id"]), "edition": e, "version": v,
                            "file": f.name, "size": f.stat().st_size if f.exists() else 0,
                            "used_by": users, "removable": removable, "reason": reason})
        return sorted(out, key=lambda x: (x["profile"], vparse(x["version"]), x["edition"]))

    def delete_checkmk(self, p, edition, ver):
        """Ein Checkmk-Paket aus dem Repo eines Betriebssystems entfernen."""
        files = [d["file"] for d in self.checkmk_files(p) if d["edition"] == edition and d["version"] == ver]
        if not files:
            self.log.warn(f"{p['id']}: check-mk-{edition}-{ver} nicht im Repo")
            return False
        self.log.info(f"{p['id']}: entferne check-mk-{edition}-{ver}")
        if self.dry:
            self.log.info(dim(f"[DRY-RUN] {', '.join(f.name for f in files)} loeschen"))
            return True
        if p["family"] == "apt":
            name = self._apt_cmk_repo(p)
            if self.aptly("repo", "show", name, mutating=False, quiet=True)[0] == 0:
                self.aptly("repo", "remove", name, f"check-mk-{edition}-{ver}", quiet=True)
        else:
            d = self.public_dir / "rpm" / p["id"] / "checkmk"
            for f in files:
                (d / f.name).unlink(missing_ok=True)
        for f in files:
            f.unlink(missing_ok=True)
        ok = self.ensure_checkmk_repo(p)
        if p["family"] == "apt":
            self.aptly("db", "cleanup", quiet=True)
        return ok

    def cleanup_checkmk(self):
        """Alle entbehrlichen Checkmk-Pakete entfernen. Liefert die Anzahl."""
        todo = [u for u in self.checkmk_usage() if u["removable"]]
        if not todo:
            self.log.info("Checkmk-Pakete: nichts zu bereinigen")
            return 0
        n = 0
        for u in todo:
            p = self.profile(u["profile"])
            if p and self.delete_checkmk(p, u["edition"], u["version"]):
                self.log.ok(f"  entfernt: {u['version']} ({u['edition']}) fuer {u['profile']} – {u['reason']}")
                n += 1
        return n

    def _credentials(self):
        if self.cfg["checkmk"]["edition"] in ("community", "raw"):
            return None
        try:
            return open(expand(self.cfg["checkmk"]["credentials_file"])).read().strip() or None
        except OSError:
            return None

    def _open(self, url, method="GET", timeout=30):
        req = urllib.request.Request(url, method=method)
        cred = self._credentials()
        if cred:
            req.add_header("Authorization", "Basic " + base64.b64encode(cred.encode()).decode())
        return urllib.request.urlopen(req, timeout=timeout)

    def _index(self, ver):
        """Verzeichnisliste einer Version auf dem Download-Server (None = gibt es nicht)."""
        if ver in self._index_cache:
            return self._index_cache[ver]
        try:
            with self._open(f"{DL_BASE}/{ver}/", timeout=20) as r:
                text = r.read().decode(errors="replace")
        except urllib.error.HTTPError:
            text = None
        except (urllib.error.URLError, OSError) as e:
            raise ConnectionError(str(getattr(e, "reason", e)))
        self._index_cache[ver] = text
        return text

    def _head(self, url):
        try:
            with self._open(url, "HEAD", 20) as r:
                return r.status == 200
        except urllib.error.HTTPError:
            return False
        except (urllib.error.URLError, OSError) as e:
            raise ConnectionError(str(getattr(e, "reason", e)))

    def remote_exists(self, ver):
        if self._index(ver) is not None:
            return True
        return any(self._head(f"{DL_BASE}/{ver}/{checkmk_file_fallback(self.cfg['checkmk']['edition'], ver, p)}")
                   for p in self.profiles())

    def find_file(self, ver, p):
        """Dateiname des Pakets fuer dieses OS – oder None, wenn Checkmk es fuer das OS nicht gibt."""
        ed = self.cfg["checkmk"]["edition"]
        idx = self._index(ver)
        if idx:
            m = checkmk_file_regex(ed, ver, p).search(idx)
            return m.group(0) if m else None
        fb = checkmk_file_fallback(ed, ver, p)
        return fb if self._head(f"{DL_BASE}/{ver}/{fb}") else None

    def newest_patch(self, current):
        t = vparse(current)
        if not t:
            return None
        best, misses, pnum = None, 0, t[3]
        while misses < 3 and pnum < t[3] + 60:
            pnum += 1
            cand = vfmt((t[0], t[1], t[2], pnum))
            if self.remote_exists(cand):
                best, misses = cand, 0
            else:
                misses += 1
        return best

    def newer_branches(self, current, limit=5):
        """Neueste Version jedes neueren Major-Zweigs, in Reihenfolge (z. B. ['2.6.0p4', '2.7.0p1'])."""
        t = vparse(current)
        res = []
        while t and len(res) < limit:
            found = None
            for maj, mi in ((t[0], t[1] + 1), (t[0] + 1, 0)):
                ga = f"{maj}.{mi}.0"
                if self.remote_exists(ga) or self.remote_exists(ga + "p1"):
                    found = self.newest_patch(ga) or ga
                    break
            if not found:
                break
            res.append(found)
            t = vparse(found)
        return res

    def _expected_hash(self, ver, name):
        try:
            with self._open(f"{DL_BASE}/{ver}/HASHES", timeout=20) as r:
                text = r.read().decode(errors="replace")
        except (urllib.error.URLError, OSError):
            return None
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip("*") == name and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                return parts[0]
        return None

    def download(self, ver, name, p):
        dest = self.cmk_dir / p["id"] / name
        if dest.exists():
            return dest
        url = f"{DL_BASE}/{ver}/{name}"
        if self.dry:
            self.log.info(dim(f"[DRY-RUN] Download {url}"))
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        expected = self._expected_hash(ver, name)
        if not expected and self.cfg["checkmk"].get("require_hash"):
            self.log.err("Keine Pruefsumme gefunden – Download abgebrochen (Pruefsumme ist Pflicht)")
            return None
        part = dest.with_name(name + ".part")
        h = hashlib.sha256()
        self.log.info(f"Lade {url}")
        try:
            with self._open(url, timeout=60) as r, open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done, nextp = 0, 25
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if total and done * 100 // total >= nextp:
                        self.log.info(f"  {nextp} %")
                        nextp += 25
        except (urllib.error.URLError, OSError) as e:
            part.unlink(missing_ok=True)
            self.log.err(f"Download fehlgeschlagen: {e}")
            return None
        if expected and h.hexdigest() != expected:
            part.unlink()
            self.log.err("Pruefsumme stimmt nicht – Datei verworfen")
            return None
        part.rename(dest)
        self.log.ok("  fertig, Pruefsumme OK" if expected else "  fertig (keine Pruefsumme vom Hersteller)")
        return dest

    def add_package(self, p, path):
        self.ensure_checkmk_repo(p)
        if p["family"] == "apt":
            if self.aptly("repo", "add", self._apt_cmk_repo(p), str(path), quiet=True)[0]:
                self.log.err("Paket konnte nicht ins Repo aufgenommen werden")
                return False
        return self.ensure_checkmk_repo(p)

    def fetch_version(self, ver, profiles=None):
        """Version fuer alle (bzw. die angegebenen) Betriebssysteme laden und veroeffentlichen.
        Liefert {profil: 'ok' | 'fehlt' | 'fehler'}."""
        res = {}
        for p in profiles or self.profiles():
            self.ensure_checkmk_repo(p)              # legt Repo an bzw. uebernimmt alte Struktur
            if self.in_repo(p, ver):
                self.log.info(f"  {ver} fuer {p['id']}: bereits im Repo")
                res[p["id"]] = "ok"
                continue
            try:
                name = self.find_file(ver, p)
            except ConnectionError as e:
                self.log.err(f"download.checkmk.com nicht erreichbar: {e}")
                res[p["id"]] = "fehler"
                continue
            if not name:
                self.log.warn(f"  Checkmk {ver} gibt es nicht fuer {p['label']} ({p['cmk_tag']})")
                res[p["id"]] = "fehlt"
                continue
            path = self.download(ver, name, p)
            if self.dry:
                res[p["id"]] = "ok"
            elif path and self.add_package(p, path):
                self.log.ok(f"  {ver} fuer {p['id']} im Repo veroeffentlicht")
                res[p["id"]] = "ok"
            else:
                res[p["id"]] = "fehler"
        return res

    def check_checkmk(self, master_version, download=True):
        """Neue Patch- und Major-Versionen suchen und (je nach Einstellung) ins Repo laden."""
        ck = self.cfg["checkmk"]
        res = {"current": master_version, "patch": None, "majors": [], "unsupported": {}}
        base = vbase(master_version or "")
        if not base:
            self.log.warn("Checkmk-Version des Masters unbekannt – Pruefung uebersprungen")
            return res
        self.log.info(f"Suche neue Checkmk-Versionen (Master: {base}, Edition {ck['edition']})")
        try:
            res["patch"] = self.newest_patch(base)
            res["majors"] = self.newer_branches(res["patch"] or base)
        except ConnectionError as e:
            self.log.err(f"download.checkmk.com nicht erreichbar: {e}")
            return res
        todo = []
        if res["patch"]:
            self.log.info(f"Neuer Patch: {res['patch']}")
            if ck.get("auto_patch", True):
                todo.append(res["patch"])
        else:
            self.log.info(f"{base} ist der neueste Patch seines Zweigs")
            todo.append(base)        # Master-Version fuer jedes OS bereithalten (neue Kunden, Nachholen)
        for mv in res["majors"]:
            self.log.info(f"Neue Major-Version: {mv}")
            if ck.get("major_download", True):
                todo.append(mv)
        if download:
            for v in todo:
                out = self.fetch_version(v)
                missing = [pid for pid, s in out.items() if s == "fehlt"]
                if missing:
                    res["unsupported"][v] = missing
        if not self.dry:
            update_state(lambda s: s.update({"checkmk_check": {
                "time": now().isoformat(timespec="seconds"), **res}}))
        return res
