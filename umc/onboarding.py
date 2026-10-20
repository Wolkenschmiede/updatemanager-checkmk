"""Einrichtung abschliessen: Checkliste und gefuehrter Durchgang.

Nach dem Assistenten (und jederzeit ueber Menue 'e') werden die restlichen Schritte erledigt:
Update-Server einrichten, REST-API pruefen, Master und Kunden vorbereiten, Repo synchronisieren,
Verbindung testen, Zeitplan aktivieren – optional Weboberflaeche und Teams.
Die Checkliste zeigt jederzeit, was noch fehlt (Menue, Weboberflaeche, 'updatemanager-checkmk checklist').
"""
import re

from . import config, setup
from .common import HOME, Log, bold, dim, green, load_state, red, update_state, yellow
from .ui import ask_yes, header


def _targets(cfg):
    out = []
    if cfg["master"].get("host"):
        out.append(("Master", config.master_target(cfg)))
    out += [("Kunde", c) for c in cfg["customers"] if c.get("enabled", True)]
    return out


def server_ready(cfg):
    return (HOME / "conf" / "nginx-repo.conf").exists() and bool(cfg["repo"].get("gpg_key"))


def _short(msg):
    return re.sub(r"^\[[^\]]*\]\s*", "", msg or "")


def _fix_target(role, t):
    return "Menü 3 → 4" if role == "Master" else f"Menü 2 → {t['name']} → 4"


def forget(name):
    """Nach dem Aendern/Loeschen eines Systems: altes Testergebnis verwerfen."""
    update_state(lambda s: s.get("preflight", {}).pop(name, None))


def needs_prepare(p):
    """Muss das System (noch) vorbereitet werden? Ein leeres Repo zaehlt nicht dazu."""
    if not p:
        return True
    if p.get("ok"):
        return False
    msg = p.get("msg", "")
    return "SSH nicht erreichbar" in msg or "sudo-Recht" in msg


def checklist(cfg, with_timer=True, timer_state=None):
    """Liste der Einrichtungsschritte: {key, ok, text, fix, web}."""
    st = load_state()
    pre = st.get("preflight", {})
    items = []

    def add(key, ok, text, fix, web=None):
        items.append({"key": key, "ok": bool(ok), "text": text, "fix": fix, "web": web})

    add("server", server_ready(cfg), "Update-Server eingerichtet (aptly, nginx, Repo-Schlüssel)", "Menü 9 → 2")
    api = st.get("api_check") or {}
    add("api", api.get("ok"),
        "REST-API des Masters erreichbar" + ("" if api.get("ok") or not api else f" – {api.get('msg')}"),
        "Menü 3 → 2 (testen) bzw. 3 → 1 (Secret)")
    for role, t in _targets(cfg):
        p = pre.get(t["name"])
        n = f"{role} {t['name']}"
        if p and p.get("ok"):
            add(f"sys:{t['name']}", True, f"{n}: vorbereitet, Verbindung OK", "")
        elif not p:
            add(f"sys:{t['name']}", False, f"{n}: noch nicht vorbereitet bzw. getestet", _fix_target(role, t))
        elif "SSH nicht erreichbar" in p.get("msg", ""):
            add(f"sys:{t['name']}", False, f"{n}: nicht vorbereitet (Anmeldung als Service-User nicht möglich)",
                _fix_target(role, t))
        elif "Tunnel" in p.get("msg", ""):
            add(f"sys:{t['name']}", False, f"{n}: vorbereitet, aber Repo noch leer",
                "Menü 6 (Repo synchronisieren), danach Verbindung testen", web="repo-sync")
        else:
            add(f"sys:{t['name']}", False, f"{n}: {_short(p.get('msg'))}", "Verbindung testen: Menü 7 → 1",
                web="check")
    add("repo", st.get("last_sync"), "Repo synchronisiert (OS-Spiegel und Checkmk-Pakete)", "Menü 6", web="repo-sync")
    if with_timer:
        add("timer", (timer_state if timer_state is not None else setup.timer_state()) == "neu", "Zeitplan aktiv (täglicher Repo-Sync, Wartungsfenster)",
            "Menü 4 → 2")
    if not cfg["master"].get("host"):
        add("master", False, "Master eingetragen", "Menü 3 → 1")
    return items


def open_items(cfg):
    return [i for i in checklist(cfg) if not i["ok"]]


def print_checklist(cfg):
    items = checklist(cfg)
    done = sum(1 for i in items if i["ok"])
    header(f"Einrichtung: {done} von {len(items)} erledigt")
    for i in items:
        if i["ok"]:
            print(f"  {green('✓')} {i['text']}")
        else:
            print(f"  {red('○')} {i['text']}" + (dim(f"   → {i['fix']}") if i["fix"] else ""))
    if done == len(items):
        print(green("\n  Alles eingerichtet – Updates laufen ab jetzt automatisch im Wartungsfenster."))
    return done == len(items)


# ---------------------------------------------------------------- gefuehrter Durchgang
def _step(n, total, text):
    print()
    print(bold(f"[{n}/{total}] {text}"))


def onboard_target(ctx, t, role, log=None):
    """Ein System vorbereiten und testen; bei leerem Repo anbieten, es zu synchronisieren."""
    from .menu import repo_sync, run_job
    log = log or Log()
    print(dim(f"  Dafuer wird ein Admin-Login auf {t['host']} gebraucht (Benutzer mit sudo-Recht, nicht svc-update)."))
    if not setup.push_prepare(ctx.cfg, t, log):
        print(yellow(f"  {role} {t['name']} nicht vorbereitet – spaeter ueber {_fix_target(role, t)} nachholen."))
        return False
    ok = run_job(ctx, None, lambda lg, r, a, rp, u: u.check_all([t["name"]]))
    if not ok:
        p = load_state().get("preflight", {}).get(t["name"], {})
        if "Tunnel" in p.get("msg", "") and ask_yes("Das Repo hat noch keine Pakete fuer dieses System. Jetzt synchronisieren?", True):
            run_job(ctx, "repo-sync", lambda lg, r, a, rp, u: repo_sync(ctx.cfg, lg, r, a, rp, u, manual=True))
            ok = run_job(ctx, None, lambda lg, r, a, rp, u: u.check_all([t["name"]]))
    if ok:
        run_job(ctx, None, lambda lg, r, a, rp, u: u.inventory_all([t["name"]], pending=True))
    return ok


def finish(ctx):
    """Alle offenen Schritte nacheinander anbieten. Alles ist ueberspringbar und spaeter nachholbar."""
    from .menu import repo_sync, run_job
    header("Einrichtung abschliessen")
    print("Jetzt werden die restlichen Schritte erledigt, danach laufen die Updates automatisch.")
    print(dim("Jeder Schritt laesst sich mit 'n' ueberspringen und spaeter im Hauptmenue unter 'e' nachholen."))
    log = Log("einrichtung")
    total = 6

    _step(1, total, "Update-Server")
    if server_ready(ctx.cfg):
        print(green("  bereits eingerichtet"))
    elif ask_yes("Update-Server jetzt einrichten (Pakete, nginx, Repo-Schluessel – braucht sudo)?", True):
        setup.system_setup(ctx.cfg, log)
        ctx.save()

    _step(2, total, "REST-API des Masters")
    if not setup.test_api(ctx.cfg) and ask_yes("Zugangsdaten des Masters jetzt korrigieren?", True):
        setup.edit_master(ctx.cfg)
        ctx.save()

    _step(3, total, "Master und Kunden vorbereiten")
    pre = load_state().get("preflight", {})
    unknown = [t["name"] for _, t in _targets(ctx.cfg) if t["name"] not in pre]
    if unknown and server_ready(ctx.cfg):
        print(dim("  Pruefe zuerst, welche Systeme schon vorbereitet sind ..."))
        run_job(ctx, None, lambda lg, r, a, rp, u: u.check_all(unknown))
        pre = load_state().get("preflight", {})
    todo = [(r, t) for r, t in _targets(ctx.cfg) if needs_prepare(pre.get(t["name"]))]
    if not todo:
        print(green("  alle Systeme sind vorbereitet"))
    for role, t in todo:
        if ask_yes(f"{role} {t['name']} ({t['host']}) jetzt vorbereiten?", True):
            print(dim(f"  Dafuer wird ein Admin-Login auf {t['host']} gebraucht (Benutzer mit sudo-Recht, nicht svc-update)."))
            setup.push_prepare(ctx.cfg, t, log)

    _step(4, total, "Repo synchronisieren")
    first = not load_state().get("last_sync")
    if ask_yes("Repo jetzt synchronisieren?" + (" (beim ersten Mal einige Minuten)" if first else ""), True):
        run_job(ctx, "repo-sync", lambda lg, r, a, rp, u: repo_sync(ctx.cfg, lg, r, a, rp, u, manual=True))

    _step(5, total, "Verbindung zu allen Systemen testen")
    if _targets(ctx.cfg):
        run_job(ctx, None, lambda lg, r, a, rp, u: u.check_all())
    else:
        print(yellow("  noch keine Systeme eingetragen"))

    _step(6, total, "Zeitplan")
    if setup.timer_state() == "neu":
        print(green("  bereits aktiv"))
    elif ask_yes("Zeitplan jetzt aktivieren (taeglicher Repo-Sync, Updates im Wartungsfenster – braucht sudo)?", True):
        setup.install_timers(ctx.cfg, log)

    print()
    print(bold("Optional"))
    from . import webui
    if not webui._timer_active(webui.UNIT) and ask_yes("Weboberflaeche einschalten?", False):
        webui.web_on(ctx.cfg)
    if not ctx.cfg["notify"].get("teams") and ask_yes("Teams-Benachrichtigung einrichten?", False):
        from .menu import teams_setup
        teams_setup(ctx)
    log.close()
    print_checklist(ctx.cfg)
    print(dim("\n  Später weitere Kunden: Menü 2 → n (führt durch Anlegen, Vorbereiten und Testen)."))
    print(dim("  Kurzanleitung: updatemanager-checkmk -h   ausfuehrlich: updatemanager-checkmk --help"))
