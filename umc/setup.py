"""Einrichtung: Assistent, Bearbeiten-Dialoge, Systemeinrichtung, Vorbereitungs-Skripte, Zeitplan."""
import getpass
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

from . import config
from .cmkapi import CheckmkAPI
from .common import (HOME, PREP_DIR, bold, dim, expand, green, now, parse_weekdays,
                     red, run, sudo_write, vbase, weekdays_de, yellow)
from .config import (api_url_default, norm_time, v_host, v_name, v_site, v_site_opt, v_time,
                     v_weekdays)
from .osprofiles import default_profile, edition_of_site_version
from .repo import PKG_DIR, Repo
from .ui import ask, ask_choice, ask_int, ask_yes, header

EXE = "/usr/local/bin/updatemanager-checkmk"


# ================================================================ Geheimnisse / Schluessel
def store_secret(path, label):
    p = Path(expand(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    value = ask(label, secret=True)
    old = os.umask(0o077)
    try:
        p.write_text(value + "\n")
    finally:
        os.umask(old)
    os.chmod(p, 0o600)
    print(green(f"  gespeichert in {p} (nur fuer {getpass.getuser()} lesbar)"))


def ensure_ssh_key(cfg, ask_first=True):
    key = Path(expand(cfg["ssh"]["key"]))
    if key.exists():
        return True
    if ask_first and not ask_yes(f"SSH-Schluessel {key} existiert nicht. Jetzt erzeugen?", True):
        return False
    key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rc = subprocess.call(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                          "-C", f"updatemanager-checkmk@{socket.gethostname()}", "-f", str(key)])
    if rc == 0:
        print(green(f"  Schluessel erzeugt: {key}"))
    return rc == 0


def pubkey(cfg):
    try:
        return open(expand(cfg["ssh"]["key"]) + ".pub").read().strip()
    except OSError:
        return None


def gpg_secret_keys():
    rc, out = run(["gpg", "--batch", "--list-secret-keys", "--with-colons"], mutating=False, quiet=True)
    keys = []
    for line in out.splitlines() if rc == 0 else []:
        f = line.split(":")
        if f[0] == "sec":
            keys.append({"fpr": None, "uid": ""})
        elif f[0] == "fpr" and keys and keys[-1]["fpr"] is None:
            keys[-1]["fpr"] = f[9]
        elif f[0] == "uid" and keys and not keys[-1]["uid"]:
            keys[-1]["uid"] = f[9]
    return [k for k in keys if k["fpr"]]


def gpg_setup(cfg, log):
    keys = gpg_secret_keys()
    cur = cfg["repo"].get("gpg_key")
    if cur and any(k["fpr"] == cur for k in keys):
        log.ok("Signierschluessel fuer das Repo vorhanden")
        return True
    if keys:
        print("Vorhandene GPG-Schluessel auf diesem Server:")
        for i, k in enumerate(keys, 1):
            print(f"  {i}) {k['uid']}  ({k['fpr'][-16:]})")
        v = ask("Nummer als Repo-Signierschluessel verwenden oder 'n' fuer einen neuen", "1",
                validate=lambda s: None if s == "n" or (s.isdigit() and 1 <= int(s) <= len(keys)) else "Nummer oder n")
        if v != "n":
            cfg["repo"]["gpg_key"] = keys[int(v) - 1]["fpr"]
            log.ok(f"Verwende {keys[int(v) - 1]['uid']}")
            return True
    uid = ask("Name/E-Mail fuer den neuen Repo-Schluessel",
              f"Checkmk Update Repo <repo@{socket.gethostname()}>")
    rc, _ = run(["gpg", "--batch", "--passphrase", "", "--quick-gen-key", uid, "rsa4096", "sign", "never"], log)
    match = [k for k in gpg_secret_keys() if k["uid"] == uid]
    if rc or not match:
        log.err("GPG-Schluessel konnte nicht erzeugt werden")
        return False
    cfg["repo"]["gpg_key"] = match[-1]["fpr"]
    log.ok("Neuer Signierschluessel erzeugt. Satelliten brauchen danach das neue Vorbereitungs-Skript.")
    return True


# ================================================================ Bearbeiten-Dialoge
def edit_master(cfg, log=None):
    m = cfg["master"]
    old_default = api_url_default(m.get("host"), m.get("site"))
    print(dim("Der Master ist der zentrale Checkmk-Server im NOC. Er wird als Letztes aktualisiert."))
    m["name"] = ask("Anzeigename des Masters", m.get("name") or "zentrale", validate=v_name)
    m["host"] = ask("IP/Hostname des Masters (vom Update-Server aus)", m.get("host") or None, validate=v_host)
    m["site"] = ask("Checkmk-Site-Name auf dem Master", m.get("site") or None, validate=v_site)
    m["ssh_port"] = ask_int("SSH-Port", m.get("ssh_port") or 22, 1, 65535)
    m["jumphost"] = ask("Jumphost (leer = direkte Verbindung)", m.get("jumphost", ""), required=False)
    m["checkmk_host"] = ask("Hostname des Masters IN Checkmk (fuer Downtime, leer = keine)",
                            m.get("checkmk_host", ""), required=False)
    api_default = m.get("api_url") if m.get("api_url") and m.get("api_url") != old_default \
        else api_url_default(m["host"], m["site"])
    m["api_url"] = ask("REST-API-URL des Masters", api_default)
    if m["api_url"].startswith("https"):
        m["api_verify_tls"] = ask_yes("TLS-Zertifikat pruefen?", m.get("api_verify_tls", True))
    m["api_user"] = ask("Automation-User im Master", m.get("api_user") or "svc-update", validate=v_name)
    m["api_secret_file"] = ask("Datei fuer das Automation-Secret", m.get("api_secret_file"))
    if not Path(expand(m["api_secret_file"])).exists() or ask_yes("Secret neu eingeben?", False):
        store_secret(m["api_secret_file"], "Automation-Secret (Eingabe unsichtbar)")
    return test_api(cfg)


def test_api(cfg):
    from .common import Log
    v, err = CheckmkAPI(cfg["master"], Log()).version()
    if v:
        print(green(f"  REST-API erreichbar – Master laeuft mit Checkmk {v}"))
    else:
        print(red(f"  REST-API nicht erreichbar: {err}"))
        print(dim("  Pruefen: URL, Automation-User/Secret im Master, Activate changes."))
    return v


def edit_customer(cfg, c=None):
    new = c is None
    c = config.new_customer() if new else c
    others = {x["name"] for x in cfg["customers"] if x is not c}
    c["name"] = ask("Kundenname (eindeutig, z. B. kunde_a)", c["name"] or None,
                    validate=lambda s: v_name(s) or ("Name existiert bereits" if s in others else None))
    c["host"] = ask("IP/Hostname der Kunden-VM (vom Update-Server aus)", c["host"] or None, validate=v_host)
    site_default = c["site"] if not new else re.sub(r"[^a-z0-9_]", "_", c["name"].lower())[:16]
    c["site"] = ask("Checkmk-Remote-Site auf der VM (leer = nur OS-Updates)", site_default,
                    required=False, validate=v_site_opt)
    c["checkmk_host"] = ask("Hostname der VM IN Checkmk (fuer Downtime, leer = keine)",
                            c["checkmk_host"], required=False)
    c["ssh_port"] = ask_int("SSH-Port", c["ssh_port"] or 22, 1, 65535)
    c["jumphost"] = ask("Jumphost (leer = Standard aus SSH-Einstellungen)", c["jumphost"], required=False)
    c["notes"] = ask("Notiz (optional)", c.get("notes", ""), required=False)
    if new:
        c["enabled"] = True
        cfg["customers"].append(c)
    return c


def edit_schedule(cfg):
    s, u = cfg["schedule"], cfg["updates"]
    print(dim("Im Wartungsfenster laufen zuerst die OS-Updates, danach die Checkmk-Updates (Master jeweils zuletzt)."))
    s["weekdays"] = ",".join(parse_weekdays(ask("Wochentag(e) des Wartungsfensters (z. B. Mo oder Mo,Do)",
                                                weekdays_de(s["weekdays"]), validate=v_weekdays)))
    s["start"] = norm_time(ask("Beginn (HH:MM)", s["start"], validate=v_time))
    s["end"] = norm_time(ask("Ende (HH:MM)", s["end"], validate=lambda x: v_time(x) or (
        None if norm_time(x) > s["start"] else "Ende muss nach dem Beginn liegen")))
    s["timezone"] = ask("Zeitzone", s["timezone"])
    s["repo_sync"] = norm_time(ask("Taegliche Repo-Aktualisierung (Ubuntu + Checkmk) um (HH:MM)",
                                   s["repo_sync"], validate=v_time))
    s["os_update"] = ask_yes("OS-Updates im Wartungsfenster ausfuehren?", s["os_update"])
    if s["os_update"]:
        s["include_master_os"] = ask_yes("Auch den Master per OS-Update aktualisieren (als Letztes)?",
                                         s["include_master_os"])
    s["checkmk_update"] = ask_yes("Checkmk-Patch-Updates im Wartungsfenster ausfuehren?", s["checkmk_update"])
    u["batch_size"] = ask_int("Wie viele Kunden gleichzeitig aktualisieren (OS und Checkmk; 0 = alle, Master immer zuletzt)?",
                              u.get("batch_size", 0), 0, 500)
    s["min_minutes_left"] = ask_int("Keinen neuen Host mehr beginnen, wenn weniger als X Minuten uebrig",
                                    s["min_minutes_left"], 0, 600)
    u["downtime_min"] = ask_int("Downtime pro Host waehrend des Updates (Minuten)", u["downtime_min"], 5, 600)
    u["reboot_timeout_min"] = ask_int("Max. Wartezeit auf Neustart (Minuten)", u["reboot_timeout_min"], 2, 120)


def edit_ssh(cfg):
    s = cfg["ssh"]
    s["user"] = ask("Service-User auf Master und Satelliten", s["user"], validate=v_name)
    s["key"] = ask("Privater SSH-Schluessel des Update-Servers", s["key"])
    s["connect_timeout"] = ask_int("Verbindungs-Timeout (Sekunden)", s["connect_timeout"], 3, 120)
    s["jumphost"] = ask("Standard-Jumphost (leer = keiner, Format user@host[:port])", s["jumphost"], required=False)
    ensure_ssh_key(cfg)


def edit_tunnel(cfg):
    r = cfg["repo"]
    print(dim("Der Tunnel leitet auf dem Satelliten 127.0.0.1:<Tunnel-Port> zum Repo auf diesem Server weiter."))
    r["listen_host"] = ask("Repo-Adresse aus Sicht dieses Servers", r["listen_host"], validate=v_host)
    r["listen_port"] = ask_int("Repo-Port (nginx)", r["listen_port"], 1, 65535)
    r["tunnel_port"] = ask_int("Tunnel-Port auf den Satelliten", r["tunnel_port"], 1024, 65535)


def edit_repo(cfg):
    r = cfg["repo"]
    print(dim("Die Spiegel je Betriebssystem stehen unter Repository -> Betriebssysteme."))
    r["filter_mode"] = ask_choice("Umfang der apt-Spiegel (Ubuntu/Debian)", [
        ("packages", "Nur Pakete, die auf den Systemen installiert sind (klein, empfohlen)"),
        ("full", "Kompletter Spiegel (sehr gross, > 100 GB je Version)")], r["filter_mode"])
    r["keep_snapshots"] = ask_int("Wie viele Repo-Staende fuer Rollback behalten?", r["keep_snapshots"], 1, 100)
    r["sync_retries"] = ask_int("Wiederholungen bei Download-Fehlern", r["sync_retries"], 1, 10)
    edit_checkmk(cfg)


def edit_checkmk(cfg):
    ck = cfg["checkmk"]
    before = ck["edition"]
    ck["edition"] = ask("Checkmk-Edition (Paketname check-mk-<edition>-...)", ck["edition"],
                        validate=lambda s: None if s.isalpha() and s.islower() else "z. B. community, ultimate")
    if before and ck["edition"] != before:
        print(yellow("  Achtung: Das aendert nur, wonach gesucht wird. Die Sites selbst wechseln die Edition"))
        print(yellow("  ueber Menue 7 -> 6 (Edition wechseln). Bis dahin werden Sites mit anderer Edition uebersprungen."))
        if not ask_yes("  Trotzdem nur die Einstellung aendern?", False):
            ck["edition"] = before
    if ck["edition"] != "community":
        ck["credentials_file"] = ask("Datei mit Download-Zugangsdaten (user:passwort)", ck["credentials_file"])
        if not Path(expand(ck["credentials_file"])).exists() or ask_yes("Zugangsdaten neu eingeben?", False):
            store_secret(ck["credentials_file"], "Checkmk-Download-Zugang im Format user:passwort")
    ck["auto_patch"] = ask_yes("Neue Patch-Versionen automatisch laden und einspielen?", ck["auto_patch"])
    ck["major_download"] = ask_yes("Neue Major-Versionen sofort ins Repo laden?", ck["major_download"])
    ck["major_auto"] = ask_yes("Major-Updates automatisch im Wartungsfenster einspielen (mit Regelpruefung)?",
                               ck["major_auto"])
    if ck["major_auto"]:
        cur = ",".join(f"{k}={v}" for k, v in ck.get("min_patch", {}).items())
        print(dim("  Optional: Mindest-Patchstand vor einem Major-Schritt, z. B. 2.6.0=2.5.0p20 (mehrere mit Komma)."))
        print(dim("  Unabhaengig davon gilt immer: erst neuester Patch des alten Zweigs, nur ein Zweig pro Lauf."))
        raw = ask("Mindest-Patchstaende", cur, required=False, validate=_v_minpatch)
        ck["min_patch"] = dict(x.strip().split("=", 1) for x in raw.split(",") if "=" in x)
    ck["require_hash"] = ask_yes("Download nur mit Hersteller-Pruefsumme erlauben?", ck["require_hash"])
    ck["backup"] = ask_choice("Backup der Site vor dem Update", [
        ("no-past", "Konfiguration ohne Messdaten-Historie (schnell, empfohlen)"),
        ("full", "Komplett inkl. Historie (langsam, gross)"),
        ("none", "Kein Backup (kein automatischer Rollback moeglich)")], ck["backup"])
    if ck["backup"] != "none":
        ck["keep_backups"] = ask_int("Backups pro Site behalten", ck["keep_backups"], 1, 20)
        ck["auto_rollback"] = ask_yes("Bei Fehler automatisch auf das Backup zurueckrollen?", ck["auto_rollback"])
    ck["repo_cleanup"] = ask_yes("Checkmk-Pakete automatisch aus dem Repo loeschen, sobald keine Site sie mehr braucht?",
                                 ck.get("repo_cleanup", True))
    ck["omd_cleanup"] = ask_yes("Nach erfolgreichem Update alte Checkmk-Versionen auf den Systemen entfernen (omd cleanup)?",
                                ck.get("omd_cleanup", True))


def _v_minpatch(raw):
    for x in [x for x in raw.split(",") if x.strip()]:
        a, _, b = x.strip().partition("=")
        if vbase(a) != a or vbase(b) != b:
            return "Format: 2.6.0=2.5.0p20"
    return None


def edition_from_version(v):
    return edition_of_site_version(v)


# ================================================================ Import alter Skript-Einstellungen
def import_legacy(cfg):
    """Uebernimmt Werte aus conf/settings.conf und conf/kunden.list (Shell-Skript-Variante)."""
    found = []
    conf = HOME / "conf"
    sfile = conf / "settings.conf"
    if sfile.exists():
        vals = {}
        for line in sfile.read_text().splitlines():
            m = re.match(r'^\s*([A-Z_]+)=("?)(.*?)\2\s*(#.*)?$', line)
            if m:
                vals[m[1]] = m[3].replace("$HOME", os.path.expanduser("~"))
        r, s, mst = cfg["repo"], cfg["ssh"], cfg["master"]
        if vals.get("UBUNTU_MIRROR"): r["ubuntu_mirror"] = vals["UBUNTU_MIRROR"]
        if vals.get("UBUNTU_DISTS"): r["dists"] = vals["UBUNTU_DISTS"].split()
        if vals.get("UBUNTU_COMPONENTS"): r["components"] = vals["UBUNTU_COMPONENTS"].split()
        if vals.get("PUBLISH_DIST"): r["codename"] = vals["PUBLISH_DIST"]
        if vals.get("REPO_HOST"): r["listen_host"] = vals["REPO_HOST"]
        if vals.get("REPO_PORT", "").isdigit(): r["listen_port"] = int(vals["REPO_PORT"])
        if vals.get("TUNNEL_PORT", "").isdigit(): r["tunnel_port"] = int(vals["TUNNEL_PORT"])
        if vals.get("SSH_USER"): s["user"] = vals["SSH_USER"]
        if vals.get("SSH_KEY"): s["key"] = vals["SSH_KEY"]
        if vals.get("DOWNTIME_MIN", "").isdigit(): cfg["updates"]["downtime_min"] = int(vals["DOWNTIME_MIN"])
        mm = re.match(r"(https?)://([^/]+)/([^/]+)/check_mk", vals.get("CMK_URL", ""))
        if mm:
            mst.update(host=mm[2], site=mm[3], name=mm[3],
                       api_url=vals["CMK_URL"])
        if vals.get("CMK_USER"): mst["api_user"] = vals["CMK_USER"]
        if vals.get("CMK_SECRET_FILE"): mst["api_secret_file"] = vals["CMK_SECRET_FILE"]
        found.append(str(sfile))
    kfile = conf / "kunden.list"
    if kfile.exists():
        for line in kfile.read_text().splitlines():
            p = line.split()
            if not p or p[0].startswith("#") or len(p) < 2:
                continue
            if config.find_customer(cfg, p[0]):
                continue
            c = config.new_customer()
            c.update(name=p[0], host=p[1], checkmk_host=p[2] if len(p) > 2 else "",
                     site=p[3] if len(p) > 3 else "")
            cfg["customers"].append(c)
        found.append(str(kfile))
    pk = conf / "kunde-pakete.txt"
    if pk.exists():
        PKG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(pk, PKG_DIR / "import.txt")
        found.append(str(pk))
    config.migrate(cfg)
    if shutil.which("gpg"):
        keys = gpg_secret_keys()
        if len(keys) == 1:
            cfg["repo"]["gpg_key"] = keys[0]["fpr"]
    return found


# ================================================================ Assistent
def wizard(cfg=None):
    first = cfg is None
    cfg = cfg or config.default_config()
    header("updatemanager-checkmk – Einrichtung" if first else "Einrichtungsassistent")
    print("Der Assistent fragt alle Einstellungen ab. [Werte in Klammern] = Vorschlag, Enter uebernimmt.")
    print(dim("Abbrechen jederzeit mit Strg+C – bis zum Ende wird nichts gespeichert."))
    if first:
        found = import_legacy(cfg)
        if found:
            print(green("\nVorhandene Einstellungen gefunden und als Vorschlag uebernommen:"))
            for f in found:
                print(green(f"  - {f}"))

    header("1/6  Repository und Tunnel")
    print(dim(f"Repo-Daten liegen unter {cfg['repo']['root']}. Satelliten erreichen das Repo nur ueber den SSH-Tunnel."))
    cfg["repo"]["root"] = ask("Verzeichnis fuer die Repo-Daten", cfg["repo"]["root"])
    edit_tunnel(cfg)
    print(dim("Welche Betriebssysteme gespiegelt werden, erkennt das Programm spaeter automatisch."))
    cfg["repo"]["filter_mode"] = ask_choice("Umfang der apt-Spiegel (Ubuntu/Debian)", [
        ("packages", "Nur Pakete, die auf den Systemen installiert sind (klein, empfohlen)"),
        ("full", "Kompletter Spiegel (sehr gross, > 100 GB je Version)")], cfg["repo"]["filter_mode"])

    header("2/6  SSH-Zugang zu Master und Satelliten")
    print(dim("Der Update-Server meldet sich per Schluessel als Service-User an (kein Passwort, eingeschraenktes sudo)."))
    edit_ssh(cfg)

    header("3/6  Checkmk-Master")
    version = edit_master(cfg)

    header("4/6  Checkmk-Pakete und Backups")
    ed = edition_from_version(version)
    if ed and ed != cfg["checkmk"]["edition"]:
        print(yellow(f"  Edition laut Master: {ed}"))
        cfg["checkmk"]["edition"] = ed
    edit_checkmk(cfg)

    header("5/6  Wartungsfenster")
    edit_schedule(cfg)

    header("6/6  Kunden (Satelliten)")
    for c in cfg["customers"]:
        print(f"  - {c['name']}  {c['host']}  Site {c['site'] or '-'}  Checkmk-Host {c['checkmk_host'] or '-'}")
    while ask_yes("Kunden hinzufuegen?", not cfg["customers"]):
        edit_customer(cfg)

    config.save(cfg)
    print(green(f"\nEinstellungen gespeichert: {config.CONF_FILE}"))
    if ask_yes("Jetzt den Update-Server einrichten (Pakete, nginx, GPG, benoetigt sudo)?", first):
        from .common import Log
        system_setup(cfg, Log("einrichtung"))
        config.save(cfg)
    header("Naechste Schritte")
    print("  1. Master und Kunden vorbereiten  -> Menue 3 bzw. 2 (Vorbereitungs-Skript)")
    print("  2. Systeme erfassen + Repo synchronisieren -> Menue 6")
    print("  3. Vorpruefung aller Systeme      -> Menue 7")
    print("  4. Zeitplan aktivieren            -> Menue 4")
    print(dim("  Details: updatemanager-checkmk --help"))
    return cfg


# ================================================================ Systemeinrichtung
NGINX_TEMPLATE = """# erzeugt von updatemanager-checkmk – Aenderungen ueber das Menue
server {{
    listen {port};
    server_name _;
    root {public};
    autoindex on;
}}
"""


def _read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def nginx_conf_text(cfg):
    repo = Repo(cfg, None)
    return NGINX_TEMPLATE.format(port=cfg["repo"]["listen_port"], public=repo.public_dir)


def system_setup(cfg, log):
    """Richtet den Update-Server ein (idempotent – kann jederzeit erneut laufen)."""
    header("Update-Server einrichten")
    user = getpass.getuser()
    repo = Repo(cfg, log)
    root = str(repo.root)
    steps = [
        ("Paketquellen aktualisieren", ["sudo", "apt-get", "-q", "update"]),
        ("Pakete installieren (aptly, nginx, gnupg, chrony, Debian-Schluessel)",
         ["sudo", "apt-get", "-q", "-y", "install", "aptly", "nginx", "gnupg", "chrony", "debian-archive-keyring"]),
        ("Repo-Verzeichnisse anlegen", ["sudo", "mkdir", "-p", f"{root}/aptly/public", f"{root}/checkmk"]),
        ("Rechte setzen", ["sudo", "chown", "-R", f"{user}:", root]),
    ]
    for label, cmd in steps:
        log.info(bold(f"> {label}"))
        if run(cmd, interactive=True)[0]:
            log.err(f"Fehlgeschlagen: {label}")
            return False
    has_rpm = any(p["family"] != "apt" for p in cfg["os_profiles"].values())
    print(dim("  Fuer Kunden mit RHEL/Rocky/Alma/Oracle/SUSE braucht der Update-Server dnf und createrepo_c."))
    if ask_yes("RPM-Werkzeuge installieren?", has_rpm):
        log.info(bold("> RPM-Werkzeuge installieren (dnf, dnf-plugins-core, createrepo-c)"))
        if run(["sudo", "apt-get", "-q", "-y", "install", "dnf", "dnf-plugins-core", "createrepo-c"],
               interactive=True)[0]:
            log.warn("RPM-Werkzeuge konnten nicht installiert werden – rpm-Spiegel stehen dann nicht zur Verfuegung")

    log.info(bold("> aptly konfigurieren"))
    repo.write_aptly_conf()
    run(["sudo", "ln", "-sf", str(repo.aptly_conf), "/etc/aptly.conf"], interactive=True)

    log.info(bold("> nginx konfigurieren"))
    conf = HOME / "conf" / "nginx-repo.conf"
    conf.write_text(nginx_conf_text(cfg))
    run(["sudo", "rm", "-f", "/etc/nginx/sites-enabled/default"], interactive=True)
    run(["sudo", "ln", "-sf", str(conf), "/etc/nginx/sites-enabled/updatemanager-checkmk.conf"], interactive=True)
    others = [f for f in Path("/etc/nginx/sites-enabled").glob("*")
              if f.name != "updatemanager-checkmk.conf" and "listen" in _read(f)]
    for f in others:
        log.warn(f"Weitere nginx-Seite gefunden: {f}")
        if ask_yes(f"  {f.name} deaktivieren (Link entfernen, Datei bleibt erhalten)?", True):
            run(["sudo", "rm", "-f", str(f)], interactive=True)
    if run(["sudo", "nginx", "-t"], interactive=True)[0] or \
            run(["sudo", "systemctl", "reload-or-restart", "nginx"], interactive=True)[0]:
        log.err("nginx-Konfiguration fehlerhaft")
        return False

    log.info(bold("> Signaturschluessel von Ubuntu und Debian fuer aptly"))
    for kr in ("/usr/share/keyrings/ubuntu-archive-keyring.gpg", "/usr/share/keyrings/debian-archive-keyring.gpg"):
        if Path(kr).exists():
            run(["gpg", "--no-default-keyring", "--keyring", "trustedkeys.gpg", "--import", kr], log, quiet=True)

    log.info(bold("> Signierschluessel fuer das eigene Repo"))
    if not gpg_setup(cfg, log):
        return False

    log.info(bold("> Zeitquelle (NTP) fuer die Kunden"))
    print(dim("  Satelliten ohne Internet brauchen eine interne Zeitquelle (sonst lehnt apt das Repo ab)."))
    if ask_yes("Diesen Server per chrony als Zeitquelle anbieten?", bool(cfg.get("ntp_server"))):
        net = ask("Erlaubtes Netz der Kunden (CIDR, z. B. 192.168.10.0/24)")
        sudo_write("/etc/chrony/conf.d/updatemanager-checkmk.conf", f"# updatemanager-checkmk\nallow {net}\n")
        run(["sudo", "systemctl", "restart", "chrony"], interactive=True)
        cfg["ntp_server"] = ask("Adresse dieses Servers aus Sicht der Kunden",
                                cfg.get("ntp_server") or "", validate=v_host)
    elif cfg.get("ntp_server"):
        cfg["ntp_server"] = ask("NTP-Server fuer die Kunden (leer = keiner)", cfg["ntp_server"], required=False)

    log.info(bold("> Checkmk-Repos veroeffentlichen"))
    for p in repo.profiles(only_used=False):
        repo.ensure_checkmk_repo(p)
    repo.export_key()
    log.ok("Update-Server eingerichtet.")
    return True


def system_check(cfg, log):
    """Prueft die Einrichtung des Update-Servers."""
    header("Pruefung Update-Server")
    repo = Repo(cfg, log)
    ok = True

    def item(good, text, hint=""):
        nonlocal ok
        ok = ok and good
        print(f"  {green('OK ') if good else red('!! ')} {text}" + (dim(f"  – {hint}") if hint and not good else ""))

    for prog in ("aptly", "nginx", "gpg", "ssh", "chronyd"):
        item(bool(shutil.which(prog) or Path(f"/usr/sbin/{prog}").exists()), f"Programm {prog}", "Einrichtung ausfuehren")
    item(repo.aptly_conf.exists(), f"aptly-Konfiguration {repo.aptly_conf}")
    item(Path(expand(cfg["ssh"]["key"])).exists(), f"SSH-Schluessel {cfg['ssh']['key']}", "Menue 5")
    item(Path(expand(cfg["master"]["api_secret_file"])).exists(), "Automation-Secret des Masters", "Menue 3")
    keys = [k["fpr"] for k in gpg_secret_keys()]
    item(cfg["repo"].get("gpg_key") in keys, "Repo-Signierschluessel", "Einrichtung ausfuehren")
    item((repo.public_dir / "repo-key.asc").exists(), "Oeffentlicher Repo-Schluessel exportiert")
    from .osprofiles import repo_probe_path
    import urllib.request
    for p in repo.profiles():
        path = repo_probe_path(p)
        if not path:
            item(False, f"Repo {p['id']}", "keine Repo-URLs eingetragen")
            continue
        url = f"http://{cfg['repo']['listen_host']}:{cfg['repo']['listen_port']}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                item(r.status == 200, f"Repo {p['id']} per HTTP erreichbar")
        except Exception as e:
            item(False, f"Repo {p['id']} per HTTP erreichbar ({url})", f"{e} – erst Repo synchronisieren?")
    if any(p["family"] != "apt" for p in repo.profiles()):
        for prog in ("dnf", "createrepo_c"):
            item(bool(shutil.which(prog)), f"Programm {prog} (fuer rpm-Spiegel)", "Einrichtung ausfuehren")
    rc, out = run(["systemctl", "is-active", "updatemanager-checkmk-maintenance.timer"], mutating=False, quiet=True)
    item(out.strip() == "active", "Zeitplan aktiv", "Menue 4")
    return ok


# ================================================================ Vorbereitungs-Skript
PREP_TEMPLATE = r"""#!/bin/bash
# =====================================================================
#  updatemanager-checkmk – Vorbereitung von @@NAME@@ (@@HOST@@)
#  Erzeugt am @@DATE@@ auf @@SERVER@@
#
#  Ausfuehren AUF DEM ZIELSYSTEM als root:
#      sudo bash @@FILE@@
#  Unterstuetzt: Ubuntu, Debian, RHEL/Rocky/Alma/Oracle/CentOS Stream, SLES/openSUSE.
#  Das Skript erkennt das System selbst und kann gefahrlos mehrfach laufen.
# =====================================================================
set -euo pipefail
SVC_USER="@@SVC_USER@@"
TUNNEL_PORT="@@TUNNEL_PORT@@"
NTP_SERVER="@@NTP@@"
TZONE="@@TZ@@"
REPO="http://127.0.0.1:$TUNNEL_PORT"
BK=/root/updatemanager-backup-sources

[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausfuehren."; exit 1; }
. /etc/os-release
FAMILY=""
case " ${ID:-} ${ID_LIKE:-} " in
  *" debian "*|*" ubuntu "*) FAMILY=apt ;;
  *" rhel "*|*" fedora "*|*" centos "*|*" rocky "*|*" almalinux "*|*" ol "*) FAMILY=dnf ;;
  *" suse "*|*" sles "*|*" opensuse "*|*" opensuse-leap "*) FAMILY=zypper ;;
esac
[ -n "$FAMILY" ] || { echo "Nicht unterstuetztes System: ${PRETTY_NAME:-?}"; exit 1; }
case $FAMILY in
  apt) PROFILE="$ID-${VERSION_CODENAME:-}" ;;
  dnf) PROFILE="$ID-${VERSION_ID%%.*}" ;;
  zypper) PROFILE="$ID-$VERSION_ID" ;;
esac
echo "System: ${PRETTY_NAME:-?}   (Profil $PROFILE, Paketverwaltung $FAMILY)"

REPOS=""
case "$PROFILE" in
@@RPM_CASES@@
esac
if [ "$FAMILY" != apt ] && [ -z "$REPOS" ]; then
  echo "FEHLER: Fuer $PROFILE sind auf dem Update-Server keine Repos hinterlegt."
  echo "        Dort zuerst 'Systeme erfassen' und unter Repository -> Betriebssysteme die Spiegel pruefen."
  exit 1
fi

echo ">>> 1/7 Service-User $SVC_USER (nur Schluessel-Login, Port-Weiterleitung fuer den Tunnel)"
id "$SVC_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$SVC_USER"
passwd -l "$SVC_USER" >/dev/null 2>&1 || true
install -d -m 700 -o "$SVC_USER" -g "$(id -gn "$SVC_USER")" "/home/$SVC_USER/.ssh"
cat > "/home/$SVC_USER/.ssh/authorized_keys" <<'KEY'
restrict,port-forwarding @@PUBKEY@@
KEY
chown "$SVC_USER:$(id -gn "$SVC_USER")" "/home/$SVC_USER/.ssh/authorized_keys"
chmod 600 "/home/$SVC_USER/.ssh/authorized_keys"
command -v restorecon >/dev/null 2>&1 && restorecon -R "/home/$SVC_USER/.ssh" || true

echo ">>> 2/7 Eingeschraenkte sudo-Rechte (Paketverwaltung, omd, reboot)"
install -d -m 750 /etc/sudoers.d
cat > /etc/sudoers.d/updatemanager-checkmk <<SUDO
# updatemanager-checkmk – zentrale Update-Steuerung
Defaults:$SVC_USER env_keep += "DEBIAN_FRONTEND"
Defaults:$SVC_USER !requiretty
$SVC_USER ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/dnf, /usr/bin/zypper, /usr/bin/omd, /usr/sbin/reboot
SUDO
chmod 440 /etc/sudoers.d/updatemanager-checkmk
visudo -cf /etc/sudoers.d/updatemanager-checkmk >/dev/null

echo ">>> 3/7 Signierschluessel des Repos"
KEYFILE_APT=/etc/apt/keyrings/updatemanager-repo.asc
KEYFILE_RPM=/etc/pki/rpm-gpg/RPM-GPG-KEY-updatemanager-checkmk
if [ "$FAMILY" = apt ]; then install -d -m 755 /etc/apt/keyrings; KEYFILE=$KEYFILE_APT
else install -d -m 755 /etc/pki/rpm-gpg; KEYFILE=$KEYFILE_RPM; fi
cat > "$KEYFILE" <<'GPG'
@@REPOKEY@@
GPG
chmod 644 "$KEYFILE"
if [ "$FAMILY" != apt ]; then rpm --import "$KEYFILE" 2>/dev/null || true; fi

echo ">>> 4/7 Paketquellen auf das Repo (ueber SSH-Tunnel) umstellen"
mkdir -p "$BK"
backup() { for f in "$@"; do [ -e "$f" ] || continue; case "$(basename "$f")" in updatemanager*) continue;; esac
           mv "$f" "$BK/" && echo "    gesichert: $f -> $BK/"; done; }
case $FAMILY in
apt)
  backup /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list
  if grep -qE '^[[:space:]]*deb ' /etc/apt/sources.list 2>/dev/null; then
    cp /etc/apt/sources.list "$BK/sources.list"; sed -i -E 's/^([[:space:]]*deb )/# \1/' /etc/apt/sources.list
  fi
  cat > /etc/apt/sources.list.d/updatemanager.sources <<SRC
# updatemanager-checkmk – erreichbar nur waehrend eines Update-Laufs (SSH-Tunnel)
Types: deb
URIs: $REPO/$ID
Suites: $VERSION_CODENAME
Components: main
Signed-By: $KEYFILE_APT

Types: deb
URIs: $REPO/checkmk
Suites: $VERSION_CODENAME
Components: main
Signed-By: $KEYFILE_APT
SRC
  ;;
dnf)
  VENDOR_KEY=$(grep -h '^gpgkey=' /etc/yum.repos.d/*.repo 2>/dev/null | head -1 | cut -d= -f2- || true)
  backup /etc/yum.repos.d/*.repo
  {
    echo "# updatemanager-checkmk – erreichbar nur waehrend eines Update-Laufs (SSH-Tunnel)"
    for r in $REPOS; do
      echo "[umc-$r]"; echo "name=updatemanager-checkmk $PROFILE $r"
      echo "baseurl=$REPO/rpm/$PROFILE/current/$r/"; echo "enabled=1"; echo "skip_if_unavailable=False"
      if [ -n "$VENDOR_KEY" ]; then echo "gpgcheck=1"; echo "gpgkey=$VENDOR_KEY"; else echo "gpgcheck=1"; fi
      echo
    done
    echo "[umc-checkmk]"; echo "name=updatemanager-checkmk Checkmk"
    echo "baseurl=$REPO/rpm/$PROFILE/checkmk/"; echo "enabled=1"
    echo "gpgcheck=0"; echo "repo_gpgcheck=1"; echo "gpgkey=file://$KEYFILE_RPM"
  } > /etc/yum.repos.d/updatemanager-checkmk.repo
  ;;
zypper)
  backup /etc/zypp/repos.d/*.repo
  for r in $REPOS; do
    printf '[umc-%s]\nname=updatemanager-checkmk %s %s\nbaseurl=%s/rpm/%s/current/%s/\nenabled=1\nautorefresh=1\ntype=rpm-md\ngpgcheck=1\n' \
      "$r" "$PROFILE" "$r" "$REPO" "$PROFILE" "$r" > "/etc/zypp/repos.d/updatemanager-$r.repo"
  done
  printf '[umc-checkmk]\nname=updatemanager-checkmk Checkmk\nbaseurl=%s/rpm/%s/checkmk/\nenabled=1\nautorefresh=1\ntype=rpm-md\npkg_gpgcheck=0\nrepo_gpgcheck=1\ngpgkey=file://%s\n' \
    "$REPO" "$PROFILE" "$KEYFILE_RPM" > /etc/zypp/repos.d/updatemanager-checkmk.repo
  ;;
esac

echo ">>> 5/7 Automatische Updates abschalten (Updates kommen zentral)"
systemctl disable --now unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer \
  dnf-automatic.timer dnf-makecache.timer >/dev/null 2>&1 || true

echo ">>> 6/7 Zeit"
timedatectl set-timezone "$TZONE" 2>/dev/null || true
if [ -n "$NTP_SERVER" ]; then
  if [ -f /etc/systemd/timesyncd.conf ] && systemctl is-enabled systemd-timesyncd >/dev/null 2>&1; then
    sed -i -E "s/^#?NTP=.*/NTP=$NTP_SERVER/" /etc/systemd/timesyncd.conf
    systemctl restart systemd-timesyncd || true
  else
    for c in /etc/chrony.conf /etc/chrony/chrony.conf; do
      [ -f "$c" ] || continue
      grep -q "^server $NTP_SERVER " "$c" || { sed -i -E 's/^(pool|server) /#\1 /' "$c"; echo "server $NTP_SERVER iburst" >> "$c"; }
      systemctl restart chronyd 2>/dev/null || systemctl restart chrony 2>/dev/null || true
    done
  fi
  echo "    Zeitquelle: $NTP_SERVER"
fi

echo ">>> 7/7 SSH-Server pruefen"
if sshd -T 2>/dev/null | grep -qiE '^allowtcpforwarding (no|local)'; then
  echo "    WARNUNG: AllowTcpForwarding verhindert den Tunnel – in /etc/ssh/sshd_config auf 'remote' oder 'yes' setzen."
fi

echo
echo "Fertig. Test vom Update-Server: updatemanager-checkmk -> Kunden/Master -> Verbindung testen"
"""


def _rpm_cases(cfg):
    """bash-'case'-Zeilen: Profil -> Repo-Namen (konfigurierte Profile + bekannte Standards)."""
    lines, seen = [], set()

    def add(p):
        if p and p["family"] != "apt" and p["id"] not in seen:
            names = " ".join(r["name"] for r in p.get("repos", []) if r.get("enabled", True))
            if names:
                lines.append(f'  {p["id"]}) REPOS="{names}" ;;')
                seen.add(p["id"])
    for p in cfg["os_profiles"].values():
        if p.get("enabled", True):
            add(p)
    for did, like in (("rocky", "rhel"), ("almalinux", "rhel"), ("centos", "rhel"), ("ol", "rhel")):
        for v in ("8", "9", "10"):
            add(default_profile({"ID": did, "ID_LIKE": like, "VERSION_ID": v}))
    for v in ("15.5", "15.6"):
        add(default_profile({"ID": "opensuse-leap", "ID_LIKE": "suse opensuse", "VERSION_ID": v}))
    return "\n".join(lines) or "  *) ;;"


def prepare_script(cfg, t, log):
    """Erzeugt vorbereitung/updatemanager-vorbereiten-<name>.sh. Liefert den Pfad oder None."""
    pk = pubkey(cfg)
    if not pk:
        log.err("Kein oeffentlicher SSH-Schluessel – zuerst unter 'SSH & Tunnel' erzeugen.")
        return None
    key = Repo(cfg, log).export_key()
    if not key:
        log.err("Repo-Signierschluessel fehlt – zuerst 'System einrichten' ausfuehren.")
        return None
    PREP_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"updatemanager-vorbereiten-{t['name']}.sh"
    text = PREP_TEMPLATE
    for k, v in {
        "@@NAME@@": t["name"], "@@HOST@@": t["host"], "@@DATE@@": f"{now():%d.%m.%Y %H:%M}",
        "@@SERVER@@": socket.gethostname(), "@@FILE@@": fname,
        "@@SVC_USER@@": cfg["ssh"]["user"], "@@TUNNEL_PORT@@": str(cfg["repo"]["tunnel_port"]),
        "@@NTP@@": cfg.get("ntp_server", ""), "@@TZ@@": cfg["schedule"]["timezone"],
        "@@RPM_CASES@@": _rpm_cases(cfg), "@@PUBKEY@@": pk, "@@REPOKEY@@": key.strip(),
    }.items():
        text = text.replace(k, v)
    path = PREP_DIR / fname
    path.write_text(text)
    path.chmod(0o750)
    return path


def push_prepare(cfg, t, log):
    """Kopiert das Skript per scp auf das Zielsystem und fuehrt es mit sudo aus (Admin-Login noetig)."""
    path = prepare_script(cfg, t, log)
    if not path:
        return False
    admin = ask(f"Admin-Benutzer auf {t['host']} (mit sudo-Recht, Passwort wird abgefragt)",
                validate=v_name)
    port = str(t.get("ssh_port") or 22)
    jump = t.get("jumphost") or cfg["ssh"].get("jumphost")
    jopt = ["-J", jump] if jump else []
    dest = f"{admin}@{t['host']}"
    log.info(f"Kopiere {path.name} nach {dest}:/tmp/")
    if run(["scp", "-P", port, *jopt, str(path), f"{dest}:/tmp/{path.name}"], interactive=True)[0]:
        log.err("Kopieren fehlgeschlagen")
        return False
    log.info("Fuehre das Skript mit sudo aus")
    rc = run(["ssh", "-t", "-p", port, *jopt, dest,
              f"sudo bash /tmp/{path.name}; rc=$?; rm -f /tmp/{path.name}; exit $rc"], interactive=True)[0]
    if rc:
        log.err(f"Vorbereitung fehlgeschlagen (rc {rc})")
        return False
    log.ok(f"{t['name']} vorbereitet")
    return True


# ================================================================ Zeitplan (systemd)
# Ein einziger Timer ruft jede Minute "tick" auf; die Uhrzeiten stehen nur in der Konfiguration.
# Dadurch gelten Aenderungen am Zeitplan sofort (Menue und Weboberflaeche) – ohne sudo.
TICK = "updatemanager-checkmk-zeitplan"
OLD_UNITS = ["updatemanager-checkmk-maintenance.service", "updatemanager-checkmk-maintenance.timer",
             "updatemanager-checkmk-reposync.service", "updatemanager-checkmk-reposync.timer"]
UNITS = [f"{TICK}.service", f"{TICK}.timer"] + OLD_UNITS


def timer_units(cfg):
    user = getpass.getuser()
    home = os.environ.get("UMC_HOME", "/opt/updatemanager-checkmk")
    return {
        f"{TICK}.service": f"""[Unit]
Description=updatemanager-checkmk: Zeitplan pruefen (Repo-Sync, Wartungsfenster)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User={user}
Environment=UMC_HOME={home}
ExecStart={EXE} tick
TimeoutStartSec=24h
""",
        f"{TICK}.timer": """[Unit]
Description=updatemanager-checkmk: Zeitplan jede Minute pruefen

[Timer]
OnCalendar=*-*-* *:*:00
AccuracySec=1s
Persistent=false

[Install]
WantedBy=timers.target
""",
    }


def timer_state():
    """'neu' = Minuten-Timer aktiv, 'alt' = alte Timer mit festen Zeiten, None = kein Zeitplan."""
    def active(name):
        return run(["systemctl", "is-active", name], mutating=False, quiet=True, timeout=5)[1].strip() == "active"
    if active(f"{TICK}.timer"):
        return "neu"
    if active("updatemanager-checkmk-maintenance.timer") or active("updatemanager-checkmk-reposync.timer"):
        return "alt"
    return None


def install_timers(cfg, log):
    from .scheduler import mark_past
    if not Path(EXE).exists():
        log.warn(f"{EXE} fehlt – bitte install.sh ausfuehren.")
    # alte Timer mit festen Uhrzeiten (bis 0.4.0) entfernen
    old = [n for n in OLD_UNITS if Path(f"/etc/systemd/system/{n}").exists()]
    if old:
        log.info("Stelle von den alten Timern (feste Uhrzeiten) auf den neuen Zeitplan um ...")
        run(["sudo", "systemctl", "disable", "--now", *[n for n in old if n.endswith(".timer")]], interactive=True)
        run(["sudo", "rm", "-f", *[f"/etc/systemd/system/{n}" for n in old]], interactive=True)
    for name, text in timer_units(cfg).items():
        if sudo_write(f"/etc/systemd/system/{name}", text):
            log.err(f"{name} konnte nicht geschrieben werden")
            return False
    mark_past(cfg)              # schon vergangene Termine von heute nicht nachholen
    run(["sudo", "systemctl", "daemon-reload"], interactive=True)
    rc = run(["sudo", "systemctl", "enable", "--now", f"{TICK}.timer"], interactive=True)[0]
    if rc == 0:
        log.ok("Zeitplan aktiv. Aenderungen an den Uhrzeiten gelten ab jetzt sofort – auch aus der Weboberflaeche.")
        for line in next_runs(cfg):
            log.info(f"  {line}")
    return rc == 0


def remove_timers(log):
    run(["sudo", "systemctl", "disable", "--now", f"{TICK}.timer", "updatemanager-checkmk-maintenance.timer",
         "updatemanager-checkmk-reposync.timer"], interactive=True)
    for name in UNITS:
        run(["sudo", "rm", "-f", f"/etc/systemd/system/{name}"], interactive=True)
    run(["sudo", "systemctl", "daemon-reload"], interactive=True)
    log.ok("Zeitplan deaktiviert.")


def show_timers():
    st = timer_state()
    print({"neu": green("  Zeitplan aktiv (prueft jede Minute)."),
           "alt": yellow("  Alte Timer mit festen Zeiten aktiv – bitte 'Zeitplan aktivieren' ausfuehren (Umstellung)."),
           None: red("  Zeitplan ist NICHT aktiv – es laufen keine automatischen Updates.")}[st])
    run(["systemctl", "list-timers", "--all", "--no-pager", "updatemanager-checkmk-*"], interactive=True)


def next_runs(cfg):
    from .scheduler import next_runs as nr
    try:
        return nr(cfg)
    except (ValueError, KeyError) as e:
        return [f"Zeitplan ungueltig: {e}"]
