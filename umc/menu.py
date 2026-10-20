"""Interaktives Menue."""
import concurrent.futures as cf
import re

from . import config, setup
from .cmkapi import CheckmkAPI
from .common import (CONF_FILE, LOG_DIR, Log, bold, dim, green, load_state, now, red, run, run_lock, sudo_write, update_state, vbase, vparse,
                     weekdays_de, yellow)
from .remote import Remote
from .repo import Repo
from .ui import Abort, ask, ask_int, ask_yes, header, menu, page, pause, table
from .updates import Updater


class Ctx:
    """Haelt Konfiguration und Testmodus; baut bei Bedarf die Werkzeuge."""

    def __init__(self, cfg, dry=False):
        self._cfg, self.dry = cfg, dry
        self._mtime = self._stamp()

    @staticmethod
    def _stamp():
        try:
            return CONF_FILE.stat().st_mtime_ns
        except OSError:
            return None

    @property
    def cfg(self):
        """Immer der aktuelle Stand – auch wenn die Weboberflaeche die Einstellungen geaendert hat."""
        m = self._stamp()
        if m is not None and m != self._mtime:
            fresh = config.load()
            if fresh is not None:
                self._cfg = fresh
            self._mtime = m
        return self._cfg

    @cfg.setter
    def cfg(self, value):
        self._cfg = value
        self._mtime = self._stamp()

    def save(self):
        config.save(self._cfg)
        self._mtime = self._stamp()

    def tools(self, kind=None, enforce_window=False):
        log = Log(kind)
        remote = Remote(self.cfg, log, self.dry)
        api = CheckmkAPI(self.cfg["master"], log, self.dry)
        repo = Repo(self.cfg, log, self.dry)
        upd = Updater(self.cfg, log, remote, api, repo, self.dry, enforce_window)
        return log, remote, api, repo, upd


def _ts(entry):
    if not entry:
        return dim("–")
    t = entry.get("time", "")[:16].replace("T", " ")
    ok = entry.get("ok")
    sym = green("OK") if ok is True else red("FEHLER") if ok is False else yellow("–")
    return f"{t} {sym}"


def run_job(ctx, kind, fn):
    """Fuehrt einen Lauf mit Logdatei und Sperre aus. Liefert True bei Erfolg."""
    log, remote, api, repo, upd = ctx.tools(kind)
    ok = False
    try:
        with run_lock():
            ok = fn(log, remote, api, repo, upd)
        if kind in ("maintenance", "update-os", "update-checkmk", "edition") and upd.reports:
            from .notify import Notifier
            Notifier(ctx.cfg, log, ctx.dry).report(kind, ok, upd.reports, upd.blocked, manual=True)
        print()
        print(green("Fertig.") if ok else red("Mit Fehlern beendet."), dim(f"Log: {log.path}") if log.path else "")
    except RuntimeError as e:
        print(red(str(e)))
    finally:
        log.close()
    return bool(ok)


def _run_logged(ctx, kind, fn, confirm=None):
    """Wie run_job, mit optionaler Rueckfrage und Pause danach (fuer das Menue)."""
    if confirm and not ctx.dry and not ask_yes(confirm, False):
        return
    run_job(ctx, kind, fn)
    pause()


# ================================================================ Status
def status_view(ctx, wait=True):
    cfg = ctx.cfg
    log, remote, api, repo, upd = ctx.tools()
    log.echo = False
    header("Status-Uebersicht")
    st = load_state()
    hosts = st.get("hosts", {})
    targets = []
    if cfg["master"].get("host"):
        targets.append(("Master", config.master_target(cfg)))
    targets += [("Kunde", c) for c in cfg["customers"]]

    def probe(item):
        role, t = item
        if not t.get("enabled", True):
            return role, t, None, "deaktiviert"
        info = upd.detect(t, pending=True)
        return role, t, info, None if info else "nicht erreichbar"
    print(dim("  Erfasse Systeme (OS, offene Updates, Checkmk) ..."))
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        res = list(ex.map(probe, targets))
    infos = {t["name"]: info for _, t, info, _ in res if info}
    plan = upd.plan(infos) if cfg["master"].get("host") else {"patches": {}, "major": None, "blocked": []}
    rows = []
    for role, t, info, err in res:
        h = hosts.get(t["name"], {})
        if err:
            rows.append([role, t["name"], dim("–"), red(err) if err != "deaktiviert" else dim(err),
                         "", "", _ts(h.get("os")), _ts(h.get("checkmk"))])
            continue
        pend = info.get("pending")
        pend_s = dim("?") if pend is None else (green("0") if pend == 0 else yellow(str(pend)))
        cmk = vbase(info.get("checkmk")) or dim("keine Site")
        nxt = ""
        if t["name"] in plan["patches"]:
            nxt = yellow(f"-> {plan['patches'][t['name']]}")
        elif t["name"] in plan.get("catchup", {}):
            nxt = yellow(f"-> {plan['catchup'][t['name']]} (Major, nachholen)")
        elif plan.get("major") and t["name"] in plan["major"]["targets"] and \
                plan["major"]["targets"][t["name"]] != vbase(info.get("checkmk")):
            nxt = yellow(f"-> {plan['major']['targets'][t['name']]} (Major)")
        rows.append([role, t["name"], info["label"], green("OK"), pend_s, f"{cmk} {nxt}".strip(),
                     _ts(h.get("os")), _ts(h.get("checkmk"))])
    table(["Rolle", "Name", "Betriebssystem", "SSH", "OS-Upd.", "Checkmk (-> naechster Lauf)",
           "Letztes OS-Update", "Letztes CMK-Update"], rows)
    for b in plan.get("blocked", []):
        print(yellow(f"  ! {b}"))

    print()
    for p in repo.profiles():
        cur = repo.current_snapshot(p)
        vers = repo.repo_versions(p)
        print(f"  Repo {p['id']:<18} Stand: {cur or red('noch nicht veroeffentlicht')}   "
              f"Checkmk: {', '.join(vers) or dim('keine')}")
    ls = st.get("last_sync")
    if ls:
        print(f"  Letzter Repo-Sync:     {ls['time'][:16].replace('T', ' ')}")
    chk = st.get("checkmk_check") or {}
    for mv in chk.get("majors", []):
        print(yellow(f"  Neue Major-Version beim Hersteller: {mv}"))
    for v, pids in (chk.get("unsupported") or {}).items():
        print(yellow(f"  Checkmk {v} nicht verfuegbar fuer: {', '.join(pids)} (OS-Upgrade noetig)"))
    s = cfg["schedule"]
    print(f"  Wartungsfenster:       {weekdays_de(s['weekdays'])} {s['start']}-{s['end']} ({s['timezone']})"
          f"   Major automatisch: {'ja' if cfg['checkmk'].get('major_auto') else 'nein'}")
    lm = st.get("last_maintenance")
    if lm:
        print(f"  Letzter Wartungslauf:  {_ts(lm)}")
    for line in setup.next_runs(cfg):
        print(dim(f"  {line}"))
    if wait:
        pause()


# ================================================================ Kunden
def customers_menu(ctx):
    while True:
        cs = ctx.cfg["customers"]
        header("Kunden verwalten")
        table(["Nr", "Name", "Adresse", "Site", "Checkmk-Host", "Status"],
              [[i, c["name"], c["host"], c["site"] or "–", c["checkmk_host"] or "–",
                green("aktiv") if c.get("enabled", True) else yellow("deaktiviert")]
               for i, c in enumerate(cs, 1)])
        sel = ("1" if len(cs) == 1 else f"1-{len(cs)}")
        print(f"\n  {bold('n')}) Neuen Kunden anlegen" + (f"   {bold(sel)}) Kunde auswaehlen" if cs else "")
              + f"   {bold('0')}) Zurueck")
        k = ask("Auswahl", validate=lambda s: None if s.lower() in ("n", "0") or (s.isdigit() and 1 <= int(s) <= len(cs))
                else "Ungueltige Auswahl").lower()
        if k == "0":
            return
        if k == "n":
            c = setup.edit_customer(ctx.cfg)
            ctx.save()
            print(green(f"Kunde {c['name']} angelegt."))
            from . import onboarding
            onboarding.forget(c["name"])
            if ask_yes("Kunde jetzt vorbereiten und testen (svc-update, Paketquellen, Verbindung)?", True):
                if onboarding.onboard_target(ctx, c, "Kunde"):
                    print(green(f"\nKunde {c['name']} ist eingerichtet und wird ab jetzt automatisch aktualisiert."))
                pause()
            else:
                print(dim(f"  Spaeter: Menue 2 -> {c['name']} -> 4 (Vorbereiten)."))
                pause()
        else:
            customer_menu(ctx, cs[int(k) - 1])


def customer_menu(ctx, c):
    while True:
        info = [f"{bold(c['name'])}  {c['host']}  Site: {c['site'] or '–'}  Checkmk-Host: {c['checkmk_host'] or '–'}",
                f"Status: {'aktiv' if c.get('enabled', True) else 'deaktiviert'}" + (f"   Notiz: {c['notes']}" if c.get("notes") else "")]
        k = menu(f"Kunde {c['name']}", [
            ("1", "Bearbeiten"),
            ("2", "Deaktivieren" if c.get("enabled", True) else "Aktivieren"),
            ("3", "Verbindung testen (Vorpruefung)"),
            ("4", "Vorbereiten (Skript erzeugen / automatisch ausfuehren)"),
            ("5", "System erfassen (OS, Pakete, Checkmk)"),
            ("6", "OS-Update jetzt"),
            ("7", "Checkmk-Update jetzt"),
            ("9", "Loeschen"),
        ], info=info)
        if k is None:
            return
        if k == "1":
            before = (c["host"], c.get("ssh_port"), c.get("jumphost"))
            setup.edit_customer(ctx.cfg, c)
            ctx.save()
            if before != (c["host"], c.get("ssh_port"), c.get("jumphost")):
                from . import onboarding
                onboarding.forget(c["name"])
        elif k == "2":
            c["enabled"] = not c.get("enabled", True)
            ctx.save()
        elif k == "3":
            _run_logged(ctx, None, lambda log, r, a, rp, u: u.check_all([c["name"]]))
        elif k == "4":
            prepare_dialog(ctx, c)
        elif k == "5":
            _run_logged(ctx, None, lambda log, r, a, rp, u: u.inventory_all([c["name"]], pending=True))
        elif k == "6":
            _run_logged(ctx, "update-os", lambda log, r, a, rp, u: u.os_update_all([c["name"]], include_master=False),
                        f"OS-Update fuer {c['name']} jetzt ausfuehren (ggf. mit Neustart)?")
        elif k == "7":
            _run_logged(ctx, "update-checkmk", lambda log, r, a, rp, u: u.checkmk_update_all([c["name"]], include_master=False),
                        f"Checkmk-Update fuer {c['name']} jetzt ausfuehren?")
        elif k == "9":
            if ask_yes(f"Kunde {c['name']} wirklich loeschen (nur aus der Konfiguration)?", False):
                ctx.cfg["customers"].remove(c)
                ctx.save()
                from . import onboarding
                onboarding.forget(c["name"])
                print(dim("  Auf der VM bleibt die Vorbereitung bestehen. Rueckgaengig machen: tools/entfernen-system.sh "
                          "(siehe Hilfe, Kapitel 'Entfernen und komplett zuruecksetzen')."))
                pause()
                return


def prepare_dialog(ctx, t):
    log = Log()
    k = menu(f"{t['name']} vorbereiten", [
        ("1", "Skript erzeugen (zum manuellen Kopieren und Ausfuehren)"),
        ("2", "Automatisch per Admin-Login ausfuehren (scp + sudo)"),
    ], info=["Richtet auf dem Zielsystem ein: svc-update-User + Schluessel, sudo-Regeln,",
             "Repo-Schluessel, Paketquellen ueber Tunnel, NTP, Abschalten der Auto-Updates.",
             "Details: updatemanager-checkmk --help (Kapitel 'Neuen Kunden einrichten')"])
    if k == "1":
        p = setup.prepare_script(ctx.cfg, t, log)
        if p:
            print(green(f"\nSkript erzeugt: {p}"))
            print("Auf das Zielsystem kopieren und dort ausfuehren, z. B.:")
            port = t.get("ssh_port") or 22
            print(bold(f"  scp -P {port} {p} <admin>@{t['host']}:/tmp/"))
            print(bold(f"  ssh -t -p {port} <admin>@{t['host']} sudo bash /tmp/{p.name}"))
        pause()
    elif k == "2":
        if setup.push_prepare(ctx.cfg, t, log) and ask_yes("Verbindung jetzt testen?", True):
            _run_logged(ctx, None, lambda lg, r, a, rp, u: u.check_all([t["name"]]))
        else:
            pause()


# ================================================================ Master
def master_menu(ctx):
    while True:
        m = ctx.cfg["master"]
        info = [f"{bold(m.get('name') or '-')}  {m.get('host') or '-'}  Site: {m.get('site') or '-'}",
                f"REST-API: {m.get('api_url') or '-'}  (User {m.get('api_user')})",
                f"Checkmk-Host fuer Downtime: {m.get('checkmk_host') or '–'}"]
        k = menu("Master", [
            ("1", "Bearbeiten / neu anlegen"),
            ("2", "REST-API testen"),
            ("3", "Verbindung testen (Vorpruefung SSH/sudo/Tunnel)"),
            ("4", "Vorbereiten (Skript erzeugen / automatisch ausfuehren)"),
            ("5", "System erfassen (OS, Pakete, Checkmk)"),
        ], info=info)
        if k is None:
            return
        if k == "1":
            before = (m.get("host"), m.get("ssh_port"), m.get("jumphost"))
            setup.edit_master(ctx.cfg)
            ctx.save()
            if before != (m.get("host"), m.get("ssh_port"), m.get("jumphost")):
                from . import onboarding
                onboarding.forget(m.get("name"))
            pause()
        elif k == "2":
            setup.test_api(ctx.cfg)
            pause()
        elif k == "3":
            _run_logged(ctx, None, lambda log, r, a, rp, u: u.check_all([m["name"]]))
        elif k == "4":
            prepare_dialog(ctx, config.master_target(ctx.cfg))
        elif k == "5":
            _run_logged(ctx, None, lambda log, r, a, rp, u: u.inventory_all([m["name"]], pending=True))


# ================================================================ Zeitplan
def schedule_menu(ctx):
    while True:
        s, u = ctx.cfg["schedule"], ctx.cfg["updates"]
        info = [f"Wartungsfenster: {bold(weekdays_de(s['weekdays']) + ' ' + s['start'] + '-' + s['end'])} ({s['timezone']})",
                f"Repo-Sync taeglich: {s['repo_sync']}",
                f"OS-Updates: {'ja' if s['os_update'] else 'nein'} (Master: {'ja' if s['include_master_os'] else 'nein'}, "
                f"Kunden gleichzeitig: {u['batch_size'] or 'alle'})   Checkmk-Updates: {'ja' if s['checkmk_update'] else 'nein'}",
                f"Downtime pro Host: {u['downtime_min']} min   Kein neuer Host < {s['min_minutes_left']} min vor Ende"]
        info += [dim(x) for x in setup.next_runs(ctx.cfg)]
        k = menu("Wartungsfenster & Zeitplan", [
            ("1", "Bearbeiten"),
            ("2", "Zeitplan aktivieren (einmalig, danach gelten Aenderungen sofort)"),
            ("3", "Zeitplan deaktivieren"),
            ("4", "Timer-Status anzeigen"),
        ], info=info)
        if k is None:
            return
        log = Log()
        if k == "1":
            setup.edit_schedule(ctx.cfg)
            ctx.save()
            from .scheduler import mark_past
            mark_past(ctx.cfg)
            st_ = setup.timer_state()
            if st_ == "neu":
                print(green("Gespeichert – gilt sofort."))
            elif st_ == "alt":
                print(yellow("Gespeichert. Einmalig noch Punkt 2 ausfuehren (Umstellung auf den neuen Zeitplan)."))
            else:
                print(yellow("Gespeichert. Der Zeitplan ist noch nicht aktiv – Punkt 2 ausfuehren."))
            for line in setup.next_runs(ctx.cfg):
                print(dim(f"  {line}"))
            pause()
        elif k == "2":
            setup.install_timers(ctx.cfg, log)
            pause()
        elif k == "3":
            if ask_yes("Automatische Laeufe wirklich abschalten?", False):
                setup.remove_timers(log)
            pause()
        elif k == "4":
            setup.show_timers()
            pause()


# ================================================================ SSH & Tunnel
def ssh_menu(ctx):
    while True:
        s, r = ctx.cfg["ssh"], ctx.cfg["repo"]
        info = [f"Service-User: {s['user']}   Schluessel: {s['key']}",
                f"Standard-Jumphost: {s['jumphost'] or '–'}   Timeout: {s['connect_timeout']} s",
                f"Tunnel: Satellit 127.0.0.1:{r['tunnel_port']}  ->  {r['listen_host']}:{r['listen_port']} (Repo)"]
        k = menu("SSH & Tunnel", [
            ("1", "SSH-Einstellungen bearbeiten"),
            ("2", "Tunnel / Repo-Ports bearbeiten"),
            ("3", "Oeffentlichen Schluessel anzeigen"),
            ("4", "Neuen Schluessel erzeugen"),
        ], info=info)
        if k is None:
            return
        if k == "1":
            setup.edit_ssh(ctx.cfg)
            ctx.save()
        elif k == "2":
            setup.edit_tunnel(ctx.cfg)
            ctx.save()
            print(yellow("Hinweis: Bei geaendertem Tunnel-Port Master/Kunden neu vorbereiten (Paketquellen)."))
            pause()
        elif k == "3":
            print(setup.pubkey(ctx.cfg) or red("Kein Schluessel vorhanden"))
            pause()
        elif k == "4":
            print(yellow("Achtung: Danach muessen alle Systeme neu vorbereitet werden."))
            if ask_yes("Fortfahren?", False):
                ctx.cfg["ssh"]["key"] = ask("Pfad fuer den neuen Schluessel", ctx.cfg["ssh"]["key"] + "-neu")
                setup.ensure_ssh_key(ctx.cfg, ask_first=False)
                ctx.save()
            pause()


# ================================================================ Repository
def _choose_profile(repo, title="Betriebssystem"):
    profs = repo.profiles()
    if not profs:
        print(red("Noch keine Betriebssysteme erfasst."))
        return None
    if len(profs) == 1:
        return profs[0]
    header(title)
    for i, p in enumerate(profs, 1):
        print(f"  {i}) {p['id']}  ({p['label']})")
    n = ask("Nummer", validate=lambda x: None if x.isdigit() and 1 <= int(x) <= len(profs) else "Nummer")
    return profs[int(n) - 1]


def repo_menu(ctx):
    while True:
        log, remote, api, repo, upd = ctx.tools()
        log.echo = False
        info = []
        for p in repo.profiles():
            info.append(f"{p['id']:<18} {p['label']:<28} Stand: {repo.current_snapshot(p) or red('–')}  "
                        f"Checkmk: {', '.join(repo.repo_versions(p)) or '–'}")
        info = info or [red("Noch keine Betriebssysteme erfasst – Punkt 2 ausfuehren.")]
        info.append(dim(f"Checkmk-Edition: {ctx.cfg['checkmk']['edition']}   apt-Umfang: {ctx.cfg['repo']['filter_mode']}"))
        k = menu("Repository", [
            ("1", "Jetzt aktualisieren (Systeme erfassen, Checkmk pruefen, Spiegel synchronisieren)"),
            ("2", "Systeme erfassen (OS, Pakete, Checkmk-Version)"),
            ("3", "Repo-Staende anzeigen / Rollback"),
            ("4", "Alte Repo-Staende aufraeumen"),
            ("5", "Checkmk: nach neuen Versionen suchen (Patch + Major)"),
            ("6", "Checkmk: bestimmte Version laden"),
            ("7", "Betriebssysteme / Spiegel-Adressen"),
            ("8", "Einstellungen (Umfang, Checkmk-Edition, Major-Updates, Backups)"),
            ("9", "Checkmk-Pakete im Repo: anzeigen, loeschen, bereinigen"),
        ], info=info)
        if k is None:
            return
        if k == "1":
            _run_logged(ctx, "repo-sync", lambda lg, r, a, rp, u: repo_sync(ctx.cfg, lg, r, a, rp, u, manual=True))
        elif k == "2":
            _run_logged(ctx, "erfassen", lambda lg, r, a, rp, u: u.inventory_all(pending=True))
        elif k == "3":
            p = _choose_profile(repo)
            if not p:
                pause()
                continue
            snaps, cur = repo.snapshots(p), repo.current_snapshot(p)
            header(f"Repo-Staende {p['id']}")
            table(["Nr", "Stand", ""], [[i, s_, green("veroeffentlicht") if s_ == cur else ""]
                                        for i, s_ in enumerate(snaps, 1)])
            if snaps and ask_yes("Einen anderen Stand veroeffentlichen (Rollback)?", False):
                n = ask("Nummer", validate=lambda x: None if x.isdigit() and 1 <= int(x) <= len(snaps) else "Nummer")
                _run_logged(ctx, "repo-rollback", lambda lg, r, a, rp, u: rp.rollback(p, snaps[int(n) - 1]))
            else:
                pause()
        elif k == "4":
            _run_logged(ctx, None, lambda lg, r, a, rp, u: all(rp.cleanup_snapshots(p) >= 0 for p in rp.profiles()))
        elif k == "5":
            def chk(lg, r, a, rp, u):
                v = master_version(ctx.cfg, a, u)
                rp.check_checkmk(v, download=ask_yes("Gefundene Versionen gleich ins Repo laden?", True))
                return True
            _run_logged(ctx, None, chk)
        elif k == "6":
            v = ask("Version (z. B. 2.5.0p15, Enter = abbrechen)", "", required=False,
                    validate=lambda s_: None if vbase(s_) == s_ else "Format 2.5.0p15")
            if v:
                _run_logged(ctx, "checkmk-download",
                            lambda lg, r, a, rp, u: "fehler" not in rp.fetch_version(v).values())
        elif k == "7":
            profiles_menu(ctx)
        elif k == "8":
            setup.edit_repo(ctx.cfg)
            ctx.save()
        elif k == "9":
            checkmk_pkg_menu(ctx)


def checkmk_pkg_menu(ctx):
    while True:
        log, remote, api, repo, upd = ctx.tools()
        log.echo = False
        rows = repo.checkmk_usage()
        header("Checkmk-Pakete im Repo")
        table(["Nr", "Betriebssystem", "Version", "Edition", "Groesse", "Status"],
              [[i, u["profile"], u["version"], u["edition"], f"{u['size'] / 1e6:.0f} MB",
                (yellow("entbehrlich – ") if u["removable"] else green("behalten – ")) + u["reason"]]
               for i, u in enumerate(rows, 1)])
        if not rows:
            print(dim("  Keine Checkmk-Pakete im Repo."))
        print(dim(f"  Automatisch bereinigen: {'ja' if ctx.cfg['checkmk'].get('repo_cleanup', True) else 'nein'} "
                  "(nach Repo-Sync und nach Checkmk-Updates)"))
        k = menu("Aktion", [("1", "Paket loeschen"), ("2", "Alle entbehrlichen Pakete loeschen")])
        if k is None:
            return
        if k == "1" and rows:
            n = ask("Nummer (Enter = abbrechen)", "", required=False,
                    validate=lambda x: None if x.isdigit() and 1 <= int(x) <= len(rows) else "Nummer")
            if not n:
                continue
            u = rows[int(n) - 1]
            warn = "" if u["removable"] else f" ACHTUNG: {u['reason']}."
            _run_logged(ctx, "repo-bereinigen",
                        lambda lg, r, a, rp, up: rp.delete_checkmk(rp.profile(u["profile"]), u["edition"], u["version"]),
                        confirm=f"{u['version']} ({u['edition']}) fuer {u['profile']} loeschen?{warn}")
        elif k == "2":
            _run_logged(ctx, "repo-bereinigen", lambda lg, r, a, rp, up: rp.cleanup_checkmk() >= 0)


def profiles_menu(ctx):
    while True:
        profs = list(ctx.cfg["os_profiles"].values())
        inv = load_state().get("inventory", {})
        header("Betriebssysteme / Spiegel")
        print(dim("  Profile entstehen automatisch, sobald ein System erfasst wird."))
        table(["Nr", "Profil", "System", "Art", "Systeme", "Spiegel", "Aktiv"],
              [[i, p["id"], p["label"], p["family"],
                sum(1 for x in inv.values() if x.get("profile") == p["id"]),
                len(p.get("mirrors") or [r for r in p.get("repos", []) if r.get("enabled", True)]),
                green("ja") if p.get("enabled", True) else yellow("nein")]
               for i, p in enumerate(profs, 1)])
        if not profs:
            pause()
            return
        k = ask("Nummer bearbeiten (Enter = zurueck)", "", required=False,
                validate=lambda x: None if not x or (x.isdigit() and 1 <= int(x) <= len(profs)) else "Nummer")
        if not k:
            return
        profile_edit(ctx, profs[int(k) - 1])


def profile_edit(ctx, p):
    while True:
        lst = p["mirrors"] if p["family"] == "apt" else p["repos"]
        info = [f"{p['label']}  ({p['family']}, Checkmk-Kennung {p['cmk_tag']})"]
        if p.get("note"):
            info.append(yellow(p["note"]))
        for i, e in enumerate(lst, 1):
            if p["family"] == "apt":
                info.append(f"{i}) {e['dist']:<20} {e['url']}  [{' '.join(e.get('components', []))}]")
            else:
                info.append(f"{i}) {e['name']:<12} {e['url']}  {'' if e.get('enabled', True) else yellow('(aus)')}")
        k = menu(f"Profil {p['id']}", [
            ("1", "Eintrag bearbeiten"), ("2", "Eintrag hinzufuegen"), ("3", "Eintrag entfernen"),
            ("4", "Eintrag an/aus" if p["family"] != "apt" else "Komponenten aendern"),
            ("5", "Profil deaktivieren" if p.get("enabled", True) else "Profil aktivieren"),
        ], info=info)
        if k is None:
            ctx.save()
            return
        pick = None
        if k in ("1", "3", "4") and lst:
            n = ask("Nummer", validate=lambda x: None if x.isdigit() and 1 <= int(x) <= len(lst) else "Nummer")
            pick = lst[int(n) - 1]
        if k == "1" and pick:
            pick["url"] = ask("URL", pick["url"])
            if p["family"] == "apt":
                pick["dist"] = ask("Distribution (Suite)", pick["dist"])
        elif k == "2":
            if p["family"] == "apt":
                lst.append({"dist": ask("Distribution (Suite)"), "url": ask("URL"),
                            "components": ask("Komponenten", "main").split()})
            else:
                lst.append({"name": ask("Name (kurz, ohne Leerzeichen)", validate=setup.v_name),
                            "url": ask("URL (inkl. ggf. Token)"), "enabled": True})
        elif k == "3" and pick and ask_yes("Wirklich entfernen?", False):
            lst.remove(pick)
        elif k == "4" and pick:
            if p["family"] == "apt":
                pick["components"] = ask("Komponenten", " ".join(pick.get("components", []))).split()
            else:
                pick["enabled"] = not pick.get("enabled", True)
        elif k == "5":
            p["enabled"] = not p.get("enabled", True)
        ctx.save()


def master_version(cfg, api, upd):
    v, _ = api.version()
    if not v and cfg["master"].get("host"):
        v = upd.site_version(config.master_target(cfg))
    return v


def repo_sync(cfg, log, remote, api, repo, upd, manual=False):
    """Taeglicher Lauf: Systeme erfassen, neue Checkmk-Versionen laden, Spiegel synchronisieren."""
    from .notify import Notifier
    log.step("Systeme erfassen")
    upd.inventory_all()
    unreachable = [n for n, (st, _) in (upd.reports[-1][1].items() if upd.reports else []) if st is False]
    log.step("Checkmk-Versionen pruefen")
    errs_before = log.errors
    res = repo.check_checkmk(master_version(cfg, api, upd))
    cmk_failed = log.errors > errs_before
    log.step("Spiegel synchronisieren")
    ok = repo.sync()
    if cfg["checkmk"].get("repo_cleanup", True):
        log.step("Checkmk-Pakete bereinigen")
        repo.cleanup_checkmk()
    if not repo.dry:
        update_state(lambda s: s.update({"repo_sync_result": {"time": now().isoformat(timespec="seconds"), "ok": ok}}))
    if not repo.dry:
        try:
            blocked = list(upd.plan().get("blocked", []))
        except Exception:
            blocked = []
        def _save(s):
            s["checkmk_check"] = {"time": now().isoformat(timespec="seconds"), **res}
            s.setdefault("checkmk_plan", {}).update({"time": now().isoformat(timespec="seconds"), "blocked": blocked})
        update_state(_save)
    nt = Notifier(cfg, log, repo.dry)
    nt.new_majors([m for m in res.get("majors", []) if m in repo.all_versions()])
    problems = []
    if not ok:
        problems.append("OS-Spiegel konnten nicht aktualisiert werden")
    if cmk_failed:
        problems.append("Checkmk-Versionssuche bzw. -Download fehlgeschlagen (download.checkmk.com erreichbar? "
                        "Zugangsdaten?)")
    hints = [f"{n}: nicht erreichbar – Paketliste/Version nicht aktualisiert" for n in unreachable]
    if problems or hints:
        nt.report("repo-sync", not problems, [], problems + hints + [f"Log: {log.path}"], manual)
    return ok and not cmk_failed


# ================================================================ Updates
def updates_menu(ctx):
    while True:
        k = menu("Updates jetzt ausfuehren", [
            ("1", "Vorpruefung aller Systeme (SSH, sudo, Zeit, Tunnel, Platz)"),
            ("2", "OS-Update: alle Kunden, danach Master"),
            ("3", "Checkmk-Update: alle Remote Sites, danach Master"),
            ("4", "Kompletter Wartungslauf jetzt (OS + Checkmk, ohne Zeitfenster-Pruefung)"),
            ("5", "Einzelnen Kunden aktualisieren -> Menue 'Kunden verwalten'"),
            ("6", f"Edition wechseln (aktuell: {ctx.cfg['checkmk']['edition']}, z. B. auf eine gekaufte Edition)"),
        ] + ([("8", red("LABOR: Testsysteme auf alten Stand zuruecksetzen"))]
             if ctx.cfg.get("lab", {}).get("enabled") else []), info=[f"Testmodus (Dry-Run): {green('EIN') if ctx.dry else 'AUS'} – im Testmodus wird nichts veraendert."])
        if k is None:
            return
        if k == "1":
            _run_logged(ctx, None, lambda lg, r, a, rp, u: u.check_all())
        elif k == "2":
            _run_logged(ctx, "update-os", lambda lg, r, a, rp, u: u.os_update_all(
                include_master=ctx.cfg["schedule"]["include_master_os"]),
                "OS-Updates fuer alle aktiven Kunden (und Master) jetzt ausfuehren?")
        elif k == "3":
            _run_logged(ctx, "update-checkmk", lambda lg, r, a, rp, u: u.checkmk_update_all(),
                        "Checkmk-Update aller Remote Sites und danach des Masters jetzt ausfuehren?")
        elif k == "4":
            _run_logged(ctx, "maintenance", lambda lg, r, a, rp, u: u.maintenance(force=True),
                        "Kompletten Wartungslauf jetzt starten?")
        elif k == "5":
            customers_menu(ctx)
        elif k == "6":
            edition_dialog(ctx)
        elif k == "8" and ctx.cfg.get("lab", {}).get("enabled"):
            lab_dialog(ctx)


def lab_menu(ctx):
    while True:
        on = bool(ctx.cfg.get("lab", {}).get("enabled"))
        info = [f"Labormodus: {red('EINGESCHALTET') if on else 'aus'}",
                dim("Nur fuer Testumgebungen: setzt Sites und OS-Pakete absichtlich auf einen alten Stand,"),
                dim("damit Repo-Sync und Wartungsfenster zeigen koennen, dass sie alles wieder aktualisieren.")]
        items = [("1", "Labormodus ausschalten" if on else "Labormodus einschalten")]
        if on:
            items.append(("2", "Testsysteme zuruecksetzen (Systeme, Checkmk-Version, OS-Pakete waehlen)"))
        k = menu("Labor", items, info=info)
        if k is None:
            return
        if k == "1":
            if not on and not ask_yes(red("Labormodus nur in einer TESTUMGEBUNG einschalten. Wirklich einschalten?"), False):
                continue
            ctx.cfg.setdefault("lab", {})["enabled"] = not on
            ctx.save()
            print(green("  Labormodus eingeschaltet – in der Weboberflaeche erscheint jetzt 'Labor'.") if not on
                  else green("  Labormodus ausgeschaltet."))
            pause()
        elif k == "2" and on:
            lab_dialog(ctx)


def lab_dialog(ctx):
    from .lab import Lab
    log, remote, api, repo, upd = ctx.tools()
    log.echo = False
    header("LABOR: Testsysteme zuruecksetzen")
    print(red("  Nur fuer Testumgebungen! Setzt Sites/Pakete absichtlich auf einen alten Stand."))
    names = [c["name"] for c in ctx.cfg["customers"] if c.get("enabled", True)] + \
        ([ctx.cfg["master"]["name"]] if ctx.cfg["master"].get("host") else [])
    raw = ask(f"Systeme (Komma, Enter = alle: {', '.join(names)})", "", required=False)
    only = [x.strip() for x in raw.split(",") if x.strip()] or None
    if only and any(x not in names for x in only):
        print(red("  Unbekanntes System"))
        pause()
        return
    inv = load_state().get("inventory", {})
    cur = max((vbase(i.get("checkmk") or "") for i in inv.values() if i.get("checkmk")), default="", key=lambda v: vparse(v) or (0,))
    print(dim(f"  Suche aeltere Versionen von {cur or '?'} beim Hersteller ..."))
    vs = Lab(upd).older_versions(cur) if cur else []
    print(f"  Verfuegbar: {', '.join(vs) or 'keine'}")
    ver = ask("Checkmk-Zielversion (Enter = Checkmk nicht aendern)", "", required=False,
              validate=lambda s: None if not s or s in vs else "Bitte eine der angezeigten Versionen")
    os_pkgs = ask_yes("OS: harmlose Pakete auf Release-Stand zuruecksetzen (erzeugt offene Updates)?", True)
    clear = ask_yes("Danach alle Checkmk-Pakete aus dem Repo loeschen (Repo-Sync laedt neu)?", True)
    if not ver and not os_pkgs and not clear:
        return
    _run_logged(ctx, "labor", lambda lg, r, a, rp, u: Lab(u).rollback(only, ver or None, os_pkgs, clear),
                "Jetzt zuruecksetzen? Jede Site wird vorher gesichert.")


def edition_dialog(ctx):
    from .osprofiles import EDITION_RANK, edition_change_allowed
    ck = ctx.cfg["checkmk"]
    header("Edition wechseln")
    print("  Alle Sites (Remote Sites zuerst, Master zuletzt) werden auf DIESELBE Version der neuen Edition")
    print("  umgestellt – mit Backup, Downtime und automatischem Rollback. Erlaubt ist nur ein Wechsel nach oben")
    print("  (z. B. community -> pro -> ultimate). Eine gueltige Lizenz fuer die neue Edition ist Voraussetzung.")
    print(dim(f"  Aktuell: {ck['edition']}.  Bekannte Editionen: {', '.join(sorted(EDITION_RANK))}"))
    new = ask("Neue Edition (Enter = abbrechen)", "", required=False,
              validate=lambda x: None if not x or edition_change_allowed(ck["edition"], x)[0]
              else edition_change_allowed(ck["edition"], x)[1])
    if not new:
        return
    if new not in ("community", "raw"):
        p = setup.Path(setup.expand(ck["credentials_file"]))
        if not p.exists() or ask_yes("Download-Zugangsdaten (Checkmk-Kundenportal) neu eingeben?", False):
            setup.store_secret(ck["credentials_file"], "Zugang im Format user:passwort")
    log, remote, api, repo, upd = ctx.tools()
    log.echo = False
    print(dim("  Pruefe Voraussetzungen ..."))
    ok, reasons, _, ver = upd.edition_switch_check(new)
    if not ok:
        for r in reasons:
            print(red(f"  ! {r}"))
        pause()
        return
    print(green(f"  Voraussetzungen erfuellt: alle Sites auf {ver} ({ck['edition']})."))
    _run_logged(ctx, "edition", lambda lg, r, a, rp, u: u.edition_switch(new),
                f"Jetzt alle Sites von {ck['edition']} auf {new} {ver} umstellen?")


def teams_setup(ctx):
    """Teams-Kanal einrichten (auch aus 'Einrichtung abschliessen' aufgerufen)."""
    from .notify import Notifier
    n = ctx.cfg["notify"]
    print(dim("  Webhook-URL aus Teams: Kanal -> ... -> Workflows -> 'Beim Empfang einer Webhookanforderung"))
    print(dim("  in einem Kanal posten'. Details: Hilfe, Kapitel 'Benachrichtigungen'."))
    setup.store_secret(n["teams_webhook_file"], "Webhook-URL (Eingabe unsichtbar)")
    n["teams"] = True
    n["mode"] = "always" if ask_yes("Nach JEDEM Lauf melden? (nein = nur bei Fehlern/Hinweisen)",
                                    n["mode"] == "always") else "problems"
    n["manual_runs"] = ask_yes("Auch Laeufe melden, die im Menue gestartet werden?", n.get("manual_runs", False))
    ctx.save()
    if ask_yes("Jetzt eine Testnachricht senden?", True):
        ok, err = Notifier(ctx.cfg, Log(), ctx.dry).send("Testnachricht von updatemanager-checkmk", "ok",
                                                         ["Die Teams-Benachrichtigung ist eingerichtet."])
        print(green("  Gesendet – bitte im Teams-Kanal pruefen.") if ok else red(f"  Fehler: {err}"))


def notify_menu(ctx):
    from .notify import LOCAL_CHECK_PATH, LOCAL_CHECK_SCRIPT, Notifier, localcheck_lines
    while True:
        n = ctx.cfg["notify"]
        nt = Notifier(ctx.cfg, Log(), ctx.dry)
        lc = setup.Path(LOCAL_CHECK_PATH).exists()
        info = [f"Teams: {green('aktiv') if nt.enabled else yellow('aus')}"
                + (f"  ({'nach jedem Lauf' if n['mode'] == 'always' else 'nur bei Problemen'}"
                   f"{', auch manuelle Laeufe' if n.get('manual_runs') else ''})" if nt.enabled else ""),
                f"Checkmk-Services auf diesem Server: {green('eingerichtet') if lc else yellow('nicht eingerichtet')}"]
        k = menu("Benachrichtigungen", [
            ("1", "Teams-Kanal einrichten / aendern"),
            ("2", "Teams: Testnachricht senden"),
            ("3", "Teams ausschalten" if nt.enabled else "Teams einschalten"),
            ("4", "Checkmk: Status-Services einrichten (Local Check)"),
            ("5", "Checkmk: aktuelle Ausgabe anzeigen"),
            ("6", "Checkmk: Status-Services entfernen"),
        ], info=info)
        if k is None:
            return
        if k == "1":
            teams_setup(ctx)
            pause()
        elif k == "2":
            ok, err = nt.send("Testnachricht von updatemanager-checkmk", "ok", ["Die Teams-Benachrichtigung funktioniert."])
            print(green("  Gesendet – bitte im Teams-Kanal pruefen.") if ok else red(f"  Fehler: {err}"))
            pause()
        elif k == "3":
            n["teams"] = not n.get("teams", False)
            ctx.save()
        elif k == "4":
            if not setup.Path("/usr/lib/check_mk_agent").exists():
                print(red("  Auf diesem Server ist kein Checkmk-Agent installiert."))
                print("  Zuerst den Agenten installieren und den Server als Host in Checkmk anlegen –")
                print("  siehe Hilfe, Kapitel 'Benachrichtigungen'.")
                pause()
                continue
            run(["sudo", "mkdir", "-p", "/usr/lib/check_mk_agent/local"], interactive=True)
            if sudo_write(LOCAL_CHECK_PATH, LOCAL_CHECK_SCRIPT) == 0 and \
                    run(["sudo", "chmod", "755", LOCAL_CHECK_PATH], interactive=True)[0] == 0:
                n["checkmk_localcheck"] = True
                ctx.save()
                print(green(f"  Eingerichtet: {LOCAL_CHECK_PATH}"))
                print("  Jetzt im Master beim Host dieses Servers 'Service discovery' ausfuehren und die neuen")
                print("  Services 'Updatemanager ...' uebernehmen, dann 'Activate changes'.")
            else:
                print(red("  Einrichten fehlgeschlagen"))
            pause()
        elif k == "5":
            for line in localcheck_lines(ctx.cfg):
                st = line.split(" ", 1)[0]
                print({"0": green, "1": yellow, "2": red}.get(st, str)(f"  {line}"))
            pause()
        elif k == "6":
            run(["sudo", "rm", "-f", LOCAL_CHECK_PATH], interactive=True)
            n["checkmk_localcheck"] = False
            ctx.save()
            print(green("  Entfernt. Im Master die Services per Discovery entfernen."))
            pause()


# ================================================================ Logs
def logs_menu(ctx):
    files = sorted(LOG_DIR.glob("*.log"), reverse=True)[:25]
    header("Logs (neueste zuerst)")
    table(["Nr", "Datei", "Groesse"], [[i, f.name, f"{f.stat().st_size // 1024} KB"] for i, f in enumerate(files, 1)])
    if not files:
        pause()
        return
    n = ask("Nummer anzeigen (Enter = zurueck)", "", required=False,
            validate=lambda s: None if not s or (s.isdigit() and 1 <= int(s) <= len(files)) else "Nummer")
    if n:
        page(files[int(n) - 1])


# ================================================================ System
def system_menu(ctx):
    while True:
        k = menu("System einrichten / pruefen", [
            ("1", "Pruefen"),
            ("2", "Einrichtung ausfuehren (Pakete, aptly, nginx, GPG, NTP)"),
            ("3", "Einrichtungsassistent erneut starten"),
        ], info=["Update-Server = dieser Rechner (Repo + Steuerung)."])
        if k is None:
            return
        if k == "1":
            setup.system_check(ctx.cfg, Log())
            pause()
        elif k == "2":
            setup.system_setup(ctx.cfg, Log("einrichtung"))
            ctx.save()
            pause()
        elif k == "3":
            ctx.cfg = setup.wizard(ctx.cfg)


# ================================================================ Hauptmenue
def main_menu(ctx, show_help):
    while True:
        cfg = ctx.cfg
        s = cfg["schedule"]
        act = sum(1 for c in cfg["customers"] if c.get("enabled", True))
        info = [f"Master: {cfg['master'].get('name') or '–'} ({cfg['master'].get('host') or 'nicht konfiguriert'})   "
                f"Kunden: {act} aktiv / {len(cfg['customers'])}",
                f"Wartungsfenster: {weekdays_de(s['weekdays'])} {s['start']}-{s['end']}"
                + (yellow("   TESTMODUS (Dry-Run) AKTIV") if ctx.dry else "")]
        from . import onboarding
        try:
            n_open = len(onboarding.open_items(cfg))
        except Exception:
            n_open = 0
        if n_open:
            info.append(red(f"Einrichtung unvollstaendig: {n_open} Punkt{'e' if n_open > 1 else ''} offen  ->  e"))
        try:
            k = _main_choice(ctx, info, n_open)
        except Abort:
            return 0
        try:
            if k is None:
                return 0
            {"1": status_view, "2": customers_menu, "3": master_menu, "4": schedule_menu,
             "5": ssh_menu, "6": repo_menu, "7": updates_menu, "8": logs_menu,
             "9": system_menu, "b": notify_menu, "w": web_menu, "l": lab_menu}.get(k, lambda c: None)(ctx)
            if k == "e":
                setup_menu(ctx)
            elif k == "t":
                ctx.dry = not ctx.dry
            elif k == "h":
                help_menu()
        except Abort:
            continue
        except Exception as e:  # Menue soll nie mit Traceback abstuerzen
            print(red(f"\nFehler: {e}"))
            pause()


def web_menu(ctx):
    from . import webui
    while True:
        active = webui._timer_active(webui.UNIT)
        users = sorted(webui._load_users())
        w = ctx.cfg["web"]
        info = [f"Status: {green('eingeschaltet') if active else yellow('ausgeschaltet')}   Adresse: {webui.url_of(ctx.cfg)}",
                f"Benutzer: {', '.join(users) or red('keiner')}   HTTPS: {'an' if w.get('tls') else 'aus'}",
                dim("Die Oberflaeche bietet dieselben Funktionen wie dieses Menue – im Browser.")]
        k = menu("Weboberflaeche", [
            ("1", "Ausschalten" if active else "Einschalten"),
            ("2", "Benutzer anlegen / Passwort aendern"),
            ("3", "Im Netz freigeben / nur lokal (Adresse, Port)"),
            ("4", "HTTPS-Zertifikat erzeugen"),
            ("5", "Benutzer entfernen"),
        ], info=info)
        if k is None:
            return
        if k == "1":
            webui.web_off() if active else webui.web_on(ctx.cfg)
            pause()
        elif k == "2":
            user = ask("Benutzername", users[0] if users else None, validate=config.v_name)
            webui.set_password(user, webui._ask_password(user))
            print(green(f"  Passwort fuer {user} gespeichert."))
            pause()
        elif k == "3":
            print(dim("  127.0.0.1 = nur auf diesem Server (Zugriff per SSH-Tunnel), 0.0.0.0 = aus dem Netz erreichbar."))
            w["listen"] = ask("Adresse", w["listen"], validate=lambda s: None if re.fullmatch(r"[0-9a-fA-F.:]+", s) else "IP-Adresse")
            w["port"] = ask_int("Port", w["port"], 1, 65535)
            ctx.save()
            if w["listen"] not in ("127.0.0.1", "::1") and not w.get("tls"):
                print(yellow("  Ohne HTTPS gehen Passwoerter im Klartext uebers Netz – Punkt 4 empfohlen."))
            if active:
                run(["sudo", "systemctl", "restart", webui.UNIT], interactive=True)
            print(green(f"  Gespeichert: {webui.url_of(ctx.cfg)}"))
            pause()
        elif k == "4":
            webui.web_tls(ctx.cfg)
            pause()
        elif k == "5" and users:
            user = ask("Welcher Benutzer?", validate=lambda s: None if s in users else "Unbekannt")
            webui.cli(ctx.cfg, ["deluser", user])
            pause()


def help_menu():
    from . import helpview
    secs = helpview.sections()
    if not secs:
        helpview.show_all()
        pause()
        return
    while True:
        items = [(str(i), t) for i, (t, _) in enumerate(secs, 1)] + [("a", "Komplette Hilfe am Stueck")]
        k = menu("Hilfe – Kapitel waehlen", items)
        if k is None:
            return
        if k == "a":
            helpview.show_all()
        else:
            helpview.show(secs[int(k) - 1][1])


def setup_menu(ctx):
    from . import onboarding
    while True:
        complete = onboarding.print_checklist(ctx.cfg)
        k = menu("Einrichtung", [
            ("1", "Offene Schritte jetzt erledigen (gefuehrt)"),
            ("2", "Checkliste neu pruefen (Verbindung zu allen Systemen testen)"),
        ], info=[green("Alles erledigt.") if complete else "Jeder Schritt laesst sich einzeln ueberspringen."])
        if k is None:
            return
        if k == "1":
            onboarding.finish(ctx)
            pause()
        elif k == "2":
            setup.test_api(ctx.cfg)
            run_job(ctx, None, lambda lg, r, a, rp, u: u.check_all())


def _main_choice(ctx, info, n_open=0):
    return menu("updatemanager-checkmk – Hauptmenue", [
        *([("e", red(f"Einrichtung abschliessen ({n_open} offen)")), (None, None)] if n_open else []),
        ("1", "Status-Uebersicht"),
        ("2", "Kunden verwalten (neu, bearbeiten, vorbereiten, testen)"),
        ("3", "Master bearbeiten"),
        ("4", "Wartungsfenster & Zeitplan"),
        ("5", "SSH & Tunnel"),
        ("6", "Repository (Ubuntu + Checkmk)"),
        ("7", "Updates jetzt ausfuehren"),
        ("8", "Logs ansehen"),
        ("9", "System einrichten / pruefen"),
        (None, None),
        ("b", "Benachrichtigungen (Teams, Checkmk)"),
        ("w", "Weboberflaeche (ein/aus, Benutzer, HTTPS)"),
        ("l", "Labor: Testsysteme auf alten Stand zuruecksetzen" + (red("  [AN]") if ctx.cfg.get("lab", {}).get("enabled") else "")),
        ("t", f"Testmodus (Dry-Run) {'ausschalten' if ctx.dry else 'einschalten'}"),
        ("h", "Hilfe"),
        *([] if n_open else [("e", dim("Einrichtung / Checkliste"))]),
    ], back="Beenden", info=info)
