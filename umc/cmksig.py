"""Signaturpruefung der Checkmk-Pakete (GnuPG-Schluessel von Checkmk).

Checkmk signiert seine Pakete mit dem "Check_MK Software Release Signing Key":
  - .deb: dpkg-sig-Signatur im Archiv-Mitglied "_gpgbuilder" (klarsignierte Liste mit
    MD5/SHA1/Groesse aller Archiv-Teile). Geprueft werden die Signatur UND dass jede
    Datei im Paket genau zu dieser Liste passt.
  - .rpm: eingebettete RPM-Signatur, geprueft mit rpmkeys gegen denselben Schluessel.

Der oeffentliche Schluessel wird einmalig von download.checkmk.com geladen und nur akzeptiert,
wenn sein Fingerabdruck in der Liste erlaubter Schluessel steht (checkmk.signing_fprs).
"""
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from .common import STATE_DIR

# Laut Checkmk-Doku: "Check_MK Software Release Signing Key (2018) <feedback@check-mk.org>"
DEFAULT_FPRS = ["B1E7106575B723F00611C612434DAC48C4503261"]
KEY_FILE = "Check_MK-pubkey.gpg"


class SigError(Exception):
    pass


def _norm(fpr):
    return fpr.replace(" ", "").upper()


class Verifier:
    def __init__(self, cfg, fetch, log, home=None):
        """fetch(url) -> bytes; log wie ueberall."""
        self.cfg, self.fetch, self.log = cfg, fetch, log
        self.home = Path(home or STATE_DIR / "checkmk-keyring")
        self.allowed = [_norm(f) for f in (cfg["checkmk"].get("signing_fprs") or DEFAULT_FPRS)]

    # ------------------------------------------------------------ Schluessel
    def _gpg(self, *args, data=None):
        return subprocess.run(["gpg", "--homedir", str(self.home), "--batch", "--no-tty", *args],
                              input=data, capture_output=True, timeout=120)

    def fingerprints(self):
        if not (self.home / "pubring.kbx").exists() and not (self.home / "pubring.gpg").exists():
            return []
        r = self._gpg("--with-colons", "--fingerprint")
        out, fprs, prim = r.stdout.decode(errors="replace"), [], False
        for line in out.splitlines():
            f = line.split(":")
            if f[0] == "pub":
                prim = True
            elif f[0] == "fpr" and prim:
                fprs.append(f[9].upper())
                prim = False
        return fprs

    def ensure_key(self, base_url):
        if any(f in self.allowed for f in self.fingerprints()):
            return True
        if not shutil.which("gpg"):
            raise SigError("gpg fehlt auf dem Update-Server (Paket gnupg)")
        self.home.mkdir(parents=True, exist_ok=True)
        self.home.chmod(0o700)
        url = f"{base_url}/{KEY_FILE}"
        self.log.info(f"Lade den Signaturschluessel von Checkmk: {url}")
        try:
            key = self.fetch(url)
        except Exception as e:
            raise SigError(f"Schluessel nicht ladbar: {e}")
        r = self._gpg("--import", data=key)
        if r.returncode not in (0, 2):
            raise SigError("Schluessel konnte nicht importiert werden: " + r.stderr.decode(errors="replace")[-200:])
        fprs = self.fingerprints()
        ok = [f for f in fprs if f in self.allowed]
        if not ok:
            shutil.rmtree(self.home, ignore_errors=True)
            raise SigError(f"Der geladene Schluessel hat einen unbekannten Fingerabdruck ({', '.join(fprs) or '-'}). "
                           f"Erwartet: {', '.join(self.allowed)}. Hat Checkmk den Schluessel gewechselt, den neuen "
                           "Fingerabdruck auf checkmk.com pruefen und unter checkmk.signing_fprs eintragen.")
        self.log.ok(f"  Checkmk-Schluessel {ok[0][-16:]} importiert (Fingerabdruck stimmt)")
        return True

    # ------------------------------------------------------------ .deb
    @staticmethod
    def ar_members(data):
        if not data.startswith(b"!<arch>\n"):
            raise SigError("kein gueltiges .deb-Archiv")
        pos, out = 8, []
        while pos + 60 <= len(data):
            hdr = data[pos:pos + 60]
            name = hdr[0:16].decode(errors="replace").strip()
            if name.endswith("/"):
                name = name[:-1]
            size = int(hdr[48:58].decode().strip())
            start = pos + 60
            out.append((name, data[start:start + size]))
            pos = start + size + (size % 2)
        return out

    def _verify_clearsigned(self, text):
        r = self._gpg("--status-fd", "2", "--decrypt", data=text)
        status = r.stderr.decode(errors="replace")
        valid = [ln.split() for ln in status.splitlines() if ln.startswith("[GNUPG:] VALIDSIG")]
        if r.returncode != 0 or not valid:
            raise SigError("Signatur ungueltig oder Schluessel unbekannt")
        prim = valid[0][-1].upper()
        if prim not in self.allowed and valid[0][2].upper() not in self.allowed:
            raise SigError(f"signiert mit fremdem Schluessel {prim}")
        return r.stdout.decode(errors="replace"), prim

    def verify_deb(self, path):
        members = self.ar_members(Path(path).read_bytes())
        sig = [d for n, d in members if n == "_gpgbuilder"]
        if not sig:
            raise SigError("Paket enthaelt keine Checkmk-Signatur (_gpgbuilder)")
        plain, prim = self._verify_clearsigned(sig[0])
        listed, in_files = {}, False
        for line in plain.splitlines():
            if line.startswith("Files:"):
                in_files = True
                continue
            if in_files:
                if not line[:1].isspace():
                    break
                parts = line.split()
                if len(parts) == 4:
                    listed[parts[3]] = (parts[0].lower(), parts[1].lower(), int(parts[2]))
        if not listed:
            raise SigError("signierte Dateiliste ist leer")
        payload = [(n, d) for n, d in members if not n.startswith("_gpg")]
        for n, d in payload:
            if n not in listed:
                raise SigError(f"{n} ist nicht durch die Signatur abgedeckt")
            md5, sha1, size = listed[n]
            if len(d) != size or hashlib.md5(d).hexdigest() != md5 or hashlib.sha1(d).hexdigest() != sha1:
                raise SigError(f"{n} passt nicht zur Signatur (Paket veraendert?)")
        if not any(n.startswith("data.tar") for n, _ in payload):
            raise SigError("Paket ohne data.tar")
        return prim

    # ------------------------------------------------------------ .rpm
    def verify_rpm(self, path):
        exe = shutil.which("rpmkeys")
        if not exe:
            raise SigError("rpmkeys fehlt – RPM-Werkzeuge installieren (Menue 9 -> 2)")
        key = self._gpg("--armor", "--export", *self.allowed).stdout
        with tempfile.TemporaryDirectory() as d:
            kf = Path(d) / "key.asc"
            kf.write_bytes(key)
            db = Path(d) / "db"
            db.mkdir()
            subprocess.run([exe, "--dbpath", str(db), "--import", str(kf)], capture_output=True, timeout=60)
            r = subprocess.run([exe, "--dbpath", str(db), "--checksig", "-v", str(path)],
                               capture_output=True, timeout=300)
        out = (r.stdout + r.stderr).decode(errors="replace")
        sig_ok = [ln for ln in out.splitlines() if "Signature" in ln and "OK" in ln]
        if r.returncode != 0 or not sig_ok or "NOKEY" in out or "BAD" in out:
            raise SigError("RPM-Signatur fehlt oder ist ungueltig")
        return self.allowed[0]

    def verify(self, path, base_url):
        self.ensure_key(base_url)
        p = str(path)
        if p.endswith(".deb") or p.endswith(".deb.part"):
            return self.verify_deb(path)
        if p.endswith(".rpm") or p.endswith(".rpm.part"):
            return self.verify_rpm(path)
        raise SigError("unbekannter Pakettyp")
