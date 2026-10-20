"""Kommandozeile: updatemanager-checkmk [Befehl] [Optionen]."""
import os
import sys

from . import VERSION, config, setup
from .common import Log, red, run_lock, vbase
from .logo import banner
from .menu import Ctx, main_menu, repo_sync, status_view
from .ui import Abort

USAGE = """Aufruf: updatemanager-checkmk [Befehl] [Optionen]

Ohne Befehl startet das Menue (beim ersten Start der Einrichtungsassistent).

Erste Einrichtung (alles in einem Durchgang):
  updatemanager-checkmk  ->  Assistent fragt Einstellungen ab und erledigt danach der Reihe nach:
  Update-Server einrichten, REST-API pruefen, Master und Kunden vorbereiten (Admin-Login der VMs),
  Repo synchronisieren, Verbindung testen, Zeitplan aktivieren. Was uebersprungen wurde, steht im
  Hauptmenue unter 'e' (Checkliste) und laesst sich dort nachholen.

Spaeter erweitern:
  Neuer Kunde     Menue 2 -> n   (anlegen, vorbereiten, testen – in einem Durchgang)
  Master aendern  Menue 3 -> 1   (danach 3 -> 4 vorbereiten, falls neue VM)
  Was fehlt noch? updatemanager-checkmk checklist   bzw. Menue e

Befehle:
  status                 Status-Uebersicht
  check                  Vorpruefung aller Systeme (SSH, sudo, Zeit, Tunnel, Platz)
  repo-sync              Checkmk-Versionen pruefen/laden + Ubuntu-Repo synchronisieren
  inventory              Systeme erfassen: OS, Version, offene Updates, Checkmk, Paketlisten
  update-os              OS-Updates (Kunden, danach Master)
  update-checkmk         Checkmk-Updates (Remote Sites, danach Master)
  maintenance            Wartungslauf (laeuft nur im Wartungsfenster, ausser mit --force)
  prepare NAME           Vorbereitungs-Skript fuer Kunde/Master NAME erzeugen
  edition-switch EDITION alle Sites auf eine hoehere Edition umstellen (gleiche Version)
  checkmk-check          beim Hersteller nach neuen Checkmk-Versionen suchen und laden
  checkmk-fetch VERSION  bestimmte Checkmk-Version fuer alle Betriebssysteme ins Repo laden
  checkmk-list           Checkmk-Pakete im Repo mit Nutzung anzeigen
  checkmk-delete VERSION Checkmk-Paket aus dem Repo loeschen (--profile ID, --edition NAME)
  checkmk-cleanup        Checkmk-Pakete loeschen, die keine Site mehr braucht
  repo-cleanup           alte Repo-Staende (OS-Spiegel) aufraeumen
  repo-rollback ID STAND aelteren Repo-Stand eines Betriebssystems veroeffentlichen
  lab on|off|status      Labormodus (nur Testumgebung): Systeme auf alten Stand zuruecksetzen
  lab-versions           aeltere Checkmk-Versionen des aktuellen Zweigs anzeigen
  lab-rollback           Testsysteme zuruecksetzen: --checkmk VERSION, --os, --clear-repo, --only NAME
  web on|off|status      Weboberflaeche ein-/ausschalten (siehe Hilfe)
  web passwd [BENUTZER]  Benutzer fuer die Weboberflaeche anlegen / Passwort aendern
  web tls                Zertifikat fuer HTTPS erzeugen
  setup                  Einrichtungsassistent erneut starten (danach offene Schritte erledigen)
  checklist              Einrichtungs-Checkliste anzeigen (Rueckgabewert 0 = alles erledigt)
  notify-test            Testnachricht an den Teams-Kanal senden
  checkmk-localcheck     Status als Checkmk-Local-Check ausgeben (fuer den Agenten)

Optionen:
  --only NAME[,NAME]     nur diese Kunden (bzw. den Master per Namen)
  --no-master            Master auslassen
  --dry-run              Testmodus: zeigt, was passieren wuerde, aendert nichts
  --force                maintenance auch ausserhalb des Wartungsfensters
  -h                     diese Kurzhilfe
  --help                 ausfuehrliche Hilfe (docs/HILFE.md)
  --version              Version anzeigen
"""


def show_help():
    from .helpview import HELP_FILE, show_all
    if HELP_FILE.exists():
        show_all()
    else:
        print(USAGE)


def _opt(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            val = args[i + 1]
            del args[i:i + 2]
            return val
        del args[i]
    return None


def _lab_cli(cfg, args):
    sub = args[0] if args else "status"
    if sub in ("on", "off"):
        cfg.setdefault("lab", {})["enabled"] = sub == "on"
        config.save(cfg)
        if sub == "on":
            print(red("Labormodus EINGESCHALTET – nur fuer Testumgebungen! Damit lassen sich Sites und Pakete "
                      "auf alte Staende zuruecksetzen."))
        else:
            print("Labormodus ausgeschaltet.")
        return 0
    print(f"Labormodus: {'an' if cfg.get('lab', {}).get('enabled') else 'aus'}   (updatemanager-checkmk lab on|off)")
    return 0


def _lab_rollback(cfg, upd, only, version, flags):
    from .lab import Lab, enabled
    if not enabled(cfg):
        print(red("Labormodus ist aus. Einschalten (nur Testumgebung!): updatemanager-checkmk lab on"))
        return False
    if not version and not flags["--os"] and not flags["--clear-repo"]:
        print(red("Bitte angeben, was zurueckgesetzt wird: --checkmk VERSION und/oder --os (und optional --clear-repo)"))
        return False
    return Lab(upd).rollback(only, version, flags["--os"], flags["--clear-repo"])


def _need(args, n, usage):
    if len(args) <= n:
        print(red(f"Aufruf: updatemanager-checkmk {usage}"))
        return False
    return True


def _checkmk_check(cfg, upd, api, repo):
    from .menu import master_version
    repo.check_checkmk(master_version(cfg, api, upd))
    return True


def _checkmk_delete(cfg, repo, ver, profile=None, edition=None):
    edition = edition or cfg["checkmk"]["edition"]
    profs = [repo.profile(profile)] if profile else list(cfg["os_profiles"].values())
    if profile and not profs[0]:
        print(red(f"Unbekanntes Betriebssystem-Profil: {profile}"))
        return False
    hits = [p for p in profs if any(d["version"] == ver and d["edition"] == edition for d in repo.checkmk_files(p))]
    if not hits:
        print(red(f"check-mk-{edition}-{ver} ist nicht im Repo"))
        return False
    return all(repo.delete_checkmk(p, edition, ver) for p in hits)


def _rollback(repo, pid, snap):
    p = repo.profile(pid)
    if not p or snap not in repo.snapshots(p):
        print(red(f"Unbekanntes Profil oder unbekannter Stand: {pid} {snap}"))
        return False
    return repo.rollback(p, snap)


def main(argv):
    args = list(argv)
    if any(a in ("--help", "help") for a in args):
        show_help()
        return 0
    if "-h" in args:
        print(USAGE)
        return 0
    if "--version" in args:
        print(f"updatemanager-checkmk {VERSION}")
        return 0
    flags = {f: f in args for f in ("--dry-run", "--force", "--no-master", "--os", "--clear-repo")}
    args = [a for a in args if a not in flags]
    only_raw = _opt(args, "--only")
    prof_opt = _opt(args, "--profile")
    ed_opt = _opt(args, "--edition")
    cmk_opt = _opt(args, "--checkmk")
    only = [x.strip() for x in only_raw.split(",") if x.strip()] if only_raw else None
    cmd = args[0] if args else "menu"

    try:
        cfg = config.load()
        interactive = sys.stdin.isatty()
        if cmd in ("menu", "setup") and interactive:
            banner()
        if cfg is None:
            if cmd in ("menu", "setup") and interactive:
                cfg = setup.wizard()
                from .onboarding import finish
                finish(Ctx(cfg))
                cfg = config.load() or cfg
                if cmd == "setup":
                    return 0
            else:
                print(red("updatemanager-checkmk ist noch nicht eingerichtet – bitte einmal interaktiv 'updatemanager-checkmk' starten."))
                return 2
        elif cmd == "setup":
            cfg = setup.wizard(cfg)
            from .onboarding import finish
            finish(Ctx(cfg))
            return 0
        if cmd == "checklist":
            from .onboarding import print_checklist
            return 0 if print_checklist(cfg) else 1

        if cmd == "tick":
            from .scheduler import tick
            return tick(cfg)
        if cmd == "lab":
            return _lab_cli(cfg, args[1:])
        if cmd == "lab-versions":
            from .lab import Lab
            log, remote, api, repo, upd = Ctx(cfg).tools()
            log.echo = False
            from .common import load_state
            cur = (load_state().get("inventory", {}).get(cfg["master"].get("name"), {}) or {}).get("checkmk")
            vs = Lab(upd).older_versions(vbase(cur or ""))
            print(f"Master: {vbase(cur or '') or 'unbekannt'}  –  aeltere Versionen ({cfg['checkmk']['edition']}):")
            print("  " + (", ".join(vs) or "keine gefunden"))
            return 0
        if cmd == "web":
            from . import webui
            return webui.cli(cfg, args[1:])
        if cmd == "checkmk-list":
            from .repo import Repo
            rows = Repo(cfg, Log(echo=False)).checkmk_usage()
            if not rows:
                print("Keine Checkmk-Pakete im Repo.")
            for u in rows:
                flag = "entbehrlich" if u["removable"] else "behalten"
                print(f"{u['profile']:<18} {u['version']:<12} {u['edition']:<10} {u['size'] / 1e6:7.0f} MB  "
                      f"{flag:<11} {u['reason']}")
            return 0
        if cmd == "checkmk-localcheck":
            from .notify import localcheck_lines
            print("\n".join(localcheck_lines(cfg)))
            return 0
        if cmd == "notify-test":
            from .notify import Notifier
            ok, err = Notifier(cfg, Log(), flags["--dry-run"]).send(
                "Testnachricht", "ok", ["Die Teams-Benachrichtigung von updatemanager-checkmk funktioniert."])
            print("Gesendet." if ok else red(f"Fehler: {err}"))
            return 0 if ok else 1

        ctx = Ctx(cfg, flags["--dry-run"])
        if cmd == "menu":
            if not interactive:
                print(USAGE)
                return 2
            return main_menu(ctx, show_help)
        if cmd == "status":
            status_view(ctx, wait=False)
            return 0
        if cmd == "prepare":
            if len(args) < 2:
                print(red("Aufruf: updatemanager-checkmk prepare NAME"))
                return 2
            t = config.find_customer(cfg, args[1]) or (
                config.master_target(cfg) if cfg["master"].get("name") == args[1] else None)
            if not t:
                print(red(f"Unbekannt: {args[1]}"))
                return 2
            p = setup.prepare_script(cfg, t, Log())
            print(p or "")
            return 0 if p else 1

        jobs = {
            "check": ("check", lambda u, r, a, rp, lg: u.check_all(only)),
            "inventory": ("erfassen", lambda u, r, a, rp, lg: u.inventory_all(only, pending=True)),
            "package-lists": ("erfassen", lambda u, r, a, rp, lg: u.inventory_all(only)),
            "repo-sync": ("repo-sync", lambda u, r, a, rp, lg: repo_sync(cfg, lg, r, a, rp, u)),
            "update-os": ("update-os", lambda u, r, a, rp, lg: u.os_update_all(only, not flags["--no-master"])),
            "update-checkmk": ("update-checkmk", lambda u, r, a, rp, lg: u.checkmk_update_all(only, not flags["--no-master"])),
            "maintenance": ("maintenance", lambda u, r, a, rp, lg: u.maintenance(force=flags["--force"])),
            "checkmk-check": ("checkmk-check", lambda u, r, a, rp, lg: _checkmk_check(cfg, u, a, rp)),
            "checkmk-fetch": ("checkmk-download", lambda u, r, a, rp, lg: _need(args, 1, "checkmk-fetch VERSION")
                              and "fehler" not in rp.fetch_version(args[1]).values()),
            "checkmk-delete": ("repo-bereinigen", lambda u, r, a, rp, lg: _need(args, 1, "checkmk-delete VERSION")
                               and _checkmk_delete(cfg, rp, args[1], prof_opt, ed_opt)),
            "checkmk-cleanup": ("repo-bereinigen", lambda u, r, a, rp, lg: rp.cleanup_checkmk() >= 0),
            "repo-cleanup": ("repo-bereinigen", lambda u, r, a, rp, lg: all([rp.cleanup_snapshots(p) >= 0
                                                                            for p in rp.profiles()])
                             and (rp.remember_snapshots() or True)),
            "repo-rollback": ("repo-rollback", lambda u, r, a, rp, lg: _need(args, 2, "repo-rollback ID STAND")
                              and _rollback(rp, args[1], args[2])),
            "lab-rollback": ("labor", lambda u, r, a, rp, lg: _lab_rollback(cfg, u, only, cmk_opt, flags)),
            "edition-switch": ("edition", lambda u, r, a, rp, lg: u.edition_switch(args[1]) if len(args) > 1
                               else (print(red("Aufruf: updatemanager-checkmk edition-switch EDITION")) or False)),
        }
        if cmd not in jobs:
            print(USAGE)
            return 2
        kind, fn = jobs[cmd]
        log, remote, api, repo, upd = ctx.tools(kind)
        try:
            wait = 3 * 3600 if cmd in ("maintenance", "repo-sync") else 0   # Zeitplan: aufeinander warten
            with run_lock(wait=wait, log=log):
                ok = fn(upd, remote, api, repo, log)
            if kind in ("maintenance", "update-os", "update-checkmk", "edition") and (upd.reports or not ok):
                from .notify import Notifier
                Notifier(cfg, log, flags["--dry-run"]).report(kind, ok, upd.reports, upd.blocked,
                                                              manual=bool(os.environ.get("UMC_MANUAL")))
        finally:
            log.info(f"Log: {log.path}")
            log.close()
        return 0 if ok else 1
    except Abort:
        print("Abgebrochen.")
        return 130
    except RuntimeError as e:
        print(red(str(e)))
        return 3
