"""Benachrichtigungen: Microsoft Teams (Webhook) und Checkmk (Local Check auf dem Update-Server)."""
import datetime
import json
import socket
import urllib.error
import urllib.request

from .common import expand, load_state, now, update_state, window_info

TITLES = {
    "maintenance": "Wartungslauf", "update-os": "OS-Update", "update-checkmk": "Checkmk-Update",
    "repo-sync": "Repo-Aktualisierung", "edition": "Editionswechsel", "test": "Testnachricht",
}
LOCAL_CHECK_PATH = "/usr/lib/check_mk_agent/local/updatemanager_checkmk"
LOCAL_CHECK_SCRIPT = "#!/bin/sh\n# updatemanager-checkmk: Status als Checkmk-Services\nexec /usr/local/bin/updatemanager-checkmk checkmk-localcheck\n"


# ================================================================ Teams
class Notifier:
    def __init__(self, cfg, log, dry_run=False):
        self.cfg, self.log, self.dry = cfg, log, dry_run
        self.n = cfg["notify"]

    def _url(self):
        try:
            return open(expand(self.n["teams_webhook_file"])).read().strip() or None
        except OSError:
            return None

    @property
    def enabled(self):
        return bool(self.n.get("teams", False) and self._url())

    def _card(self, title, level, lines, facts):
        color = {"ok": "Good", "warn": "Warning", "error": "Attention"}.get(level, "Default")
        icon = {"ok": "✅", "warn": "⚠️", "error": "❌"}.get(level, "ℹ️")
        body = [{"type": "TextBlock", "text": f"{icon} {title}", "weight": "Bolder", "size": "Medium",
                 "color": color, "wrap": True}]
        if facts:
            body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts]})
        for line in lines[:40]:
            body.append({"type": "TextBlock", "text": line, "wrap": True, "spacing": "None"})
        body.append({"type": "TextBlock", "isSubtle": True, "size": "Small", "wrap": True,
                     "text": f"updatemanager-checkmk auf {socket.gethostname()} · {now():%d.%m.%Y %H:%M}"})
        return {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard", "version": "1.4", "body": body}}]}

    def send(self, title, level="ok", lines=(), facts=()):
        url = self._url()
        if not url:
            return False, "Kein Teams-Webhook hinterlegt"
        if self.dry:
            self.log.info(f"[DRY-RUN] Teams: {title}")
            return True, None
        data = json.dumps(self._card(title, level, list(lines), list(facts))).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status >= 300:
                    return False, f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}"
        except (urllib.error.URLError, OSError) as e:
            return False, str(getattr(e, "reason", e))
        return True, None

    def report(self, kind, ok, reports, extra=(), manual=False):
        """Ergebnis eines Laufs melden (je nach Einstellung immer oder nur bei Problemen)."""
        if not self.enabled or (manual and not self.n.get("manual_runs", False)):
            return
        n_ok = sum(1 for _, res in reports for r in res.values() if r[0] is True)
        n_err = sum(1 for _, res in reports for r in res.values() if r[0] is False)
        n_skip = sum(1 for _, res in reports for r in res.values() if r[0] is None)
        if ok and not extra and n_ok + n_err + n_skip == 0:
            return
        problems = (not ok) or n_err > 0 or bool(extra)
        if self.n.get("mode", "always") == "problems" and not problems:
            return
        level = "error" if (not ok or n_err) else ("warn" if (n_skip or extra) else "ok")
        lines = []
        for title, res in reports:
            if not res:
                continue
            lines.append(f"**{title}**")
            for name, (st, msg) in res.items():
                sym = "✅" if st is True else "❌" if st is False else "➖"
                lines.append(f"{sym} {name}: {msg}")
        lines += [f"⚠️ {x}" for x in extra]
        facts = [("Ergebnis", "erfolgreich" if level == "ok" else "mit Hinweisen" if level == "warn" else "FEHLER")]
        if n_ok + n_err + n_skip:
            facts.append(("Systeme", f"{n_ok} OK, {n_err} Fehler, {n_skip} übersprungen"))
        title = f"{TITLES.get(kind, kind)}: " + ("erfolgreich" if level == "ok" else "mit Hinweisen" if level == "warn"
                                               else "Fehler")
        sent, err = self.send(title, level, lines, facts)
        if not sent:
            self.log.warn(f"Teams-Benachrichtigung fehlgeschlagen: {err}")

    def new_majors(self, majors):
        """Einmalige Info, wenn eine neue Major-Version ins Repo geladen wurde."""
        if not self.enabled or not majors:
            return
        known = set(load_state().get("notified_majors", []))
        fresh = [m for m in majors if m not in known]
        if not fresh:
            return
        auto = self.cfg["checkmk"].get("major_auto", True)
        lines = [f"Neue Checkmk-Version im Repo: **{m}**" for m in fresh]
        lines.append("Wird im nächsten Wartungsfenster eingespielt, sofern alle Regeln erfüllt sind."
                     if auto else "Automatische Major-Updates sind ausgeschaltet – bitte manuell planen.")
        sent, _ = self.send("Neue Checkmk-Major-Version verfügbar", "warn", lines)
        if sent and not self.dry:
            update_state(lambda s: s.update({"notified_majors": sorted(known | set(fresh))}))


# ================================================================ Checkmk Local Check
def _age_h(iso):
    try:
        return (now() - datetime.datetime.fromisoformat(iso)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def _svc(state, name, text, perf="-"):
    return f'{state} "{name}" {perf} {text}'


def localcheck_lines(cfg):
    """Ausgabe fuer den Checkmk-Agenten (Local Check): Status des Update-Managers als Services."""
    st = load_state()
    out = []
    # --- Repo-Aktualisierung (taeglich)
    rs = st.get("repo_sync_result") or {}
    age = _age_h(rs.get("time"))
    if age is None:
        out.append(_svc(1, "Updatemanager Repo-Sync", "Noch kein Repo-Abgleich gelaufen"))
    else:
        s = 2 if not rs.get("ok") else (2 if age > 96 else 1 if age > 48 else 0)
        txt = ("Letzter Abgleich FEHLGESCHLAGEN" if not rs.get("ok") else "Letzter Abgleich OK") + \
              f" vor {age:.0f} h" + (" – zu lange her" if rs.get("ok") and age > 48 else "")
        out.append(_svc(s, "Updatemanager Repo-Sync", txt, f"age_hours={age:.1f};48;96"))
    # --- Wartungslauf
    lm = st.get("last_maintenance") or {}
    age = _age_h(lm.get("time"))
    try:
        from .common import parse_weekdays
        interval_h = 7 * 24 / max(1, len(parse_weekdays(cfg["schedule"]["weekdays"]))) + 24
    except Exception:
        interval_h = 8 * 24
    if age is None:
        out.append(_svc(1, "Updatemanager Wartungslauf", "Noch kein Wartungslauf"))
    else:
        s = 2 if not lm.get("ok") else (1 if age > interval_h else 0)
        txt = ("Letzter Lauf mit FEHLERN" if not lm.get("ok") else "Letzter Lauf OK") + f" vor {age / 24:.1f} Tagen"
        if lm.get("ok") and age > interval_h:
            txt += " – Zeitplan pruefen"
        out.append(_svc(s, "Updatemanager Wartungslauf", txt, f"age_hours={age:.1f}"))
    # --- Systeme (letztes Ergebnis je Host)
    failed = sorted({h for h, rec in st.get("hosts", {}).items()
                     for k, e in rec.items() if isinstance(e, dict) and e.get("ok") is False})
    if failed:
        out.append(_svc(2, "Updatemanager Systeme", f"{len(failed)} mit Fehler beim letzten Lauf: {', '.join(failed)}",
                        f"failed={len(failed)}"))
    else:
        n = len(cfg.get("customers", [])) + (1 if cfg.get("master", {}).get("host") else 0)
        out.append(_svc(0, "Updatemanager Systeme", f"{n} Systeme, keine Fehler beim letzten Lauf", "failed=0"))
    # --- Checkmk-Versionen
    plan = st.get("checkmk_plan") or {}
    chk = st.get("checkmk_check") or {}
    blocked = plan.get("blocked") or []
    if blocked:
        out.append(_svc(1, "Updatemanager Checkmk-Versionen", f"{len(blocked)} Hinweis(e): {blocked[0]}"
                        + (" (weitere im Status)" if len(blocked) > 1 else "")))
    else:
        majors = chk.get("majors") or []
        txt = "Keine Blockaden" + (f", neue Major-Version im Repo: {', '.join(majors)}" if majors else "")
        out.append(_svc(0, "Updatemanager Checkmk-Versionen", txt))
    # --- Wartungsfenster (Info)
    try:
        inside, _ = window_info(cfg["schedule"])
        s = cfg["schedule"]
        out.append(_svc(0, "Updatemanager Zeitplan",
                        f"Wartungsfenster {s['weekdays']} {s['start']}-{s['end']}" + (" – laeuft gerade" if inside else "")))
    except Exception:
        pass
    return out
