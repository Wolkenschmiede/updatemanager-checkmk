"""Zeitplan ohne feste Timer-Zeiten: Ein Timer ruft jede Minute 'updatemanager-checkmk tick' auf.

tick prueft anhand der Einstellungen, ob Repo-Sync oder Wartungslauf faellig sind, und startet sie.
Vorteil: Aenderungen am Zeitplan (Menue oder Weboberflaeche) gelten sofort – ohne sudo.

Faellig ist
  - der Repo-Sync, wenn heute die eingestellte Uhrzeit erreicht ist (bis 6 Std. danach wird nachgeholt),
  - der Wartungslauf, wenn das Wartungsfenster laeuft und in diesem Fenster noch kein Lauf war.
Jeder 'Termin' (Datum + Uhrzeit) laeuft genau einmal. Wird die Uhrzeit geaendert, entsteht ein neuer Termin –
liegt er schon in der Vergangenheit, wird er nicht nachgeholt (siehe mark_past).
"""
import datetime as _dt

from .common import (WEEKDAYS, load_state, parse_weekdays, tznow, update_state, weekdays_de)

SYNC_CATCHUP_H = 6


def _today_at(now, hhmm):
    h, m = map(int, hhmm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def slots(cfg, now=None):
    """(sync_key, sync_zeit, wartung_key, wartung_beginn, wartung_ende, heute_wartungstag) fuer heute."""
    s = cfg["schedule"]
    now = now or tznow(s.get("timezone", "Europe/Berlin"))
    day = now.strftime("%Y-%m-%d")
    sync_at = _today_at(now, s["repo_sync"])
    start, end = _today_at(now, s["start"]), _today_at(now, s["end"])
    try:
        is_day = WEEKDAYS[now.weekday()] in parse_weekdays(s["weekdays"])
    except ValueError:
        is_day = False
    return (f"{day} {s['repo_sync']}", sync_at, f"{day} {s['start']}-{s['end']}", start, end, is_day, now)


def due(cfg, st=None):
    """Liste der jetzt faelligen Befehle (Reihenfolge: erst Sync, dann Wartung)."""
    st = st if st is not None else load_state()
    done = st.get("schedule_done", {})
    sk, sync_at, mk, start, end, is_day, now = slots(cfg)
    res = []
    if sync_at <= now < sync_at + _dt.timedelta(hours=SYNC_CATCHUP_H) and done.get("repo-sync") != sk:
        res.append(("repo-sync", sk))
    if is_day and start <= now < end and done.get("maintenance") != mk:
        res.append(("maintenance", mk))
    return res


def mark_past(cfg):
    """Nach einer Aenderung des Zeitplans: Termine von heute, die schon vorbei sind, nicht mehr nachholen."""
    sk, sync_at, mk, start, end, is_day, now = slots(cfg)

    def fn(st):
        d = st.setdefault("schedule_done", {})
        if now >= sync_at + _dt.timedelta(minutes=1):
            d["repo-sync"] = sk
        if is_day and now >= start + _dt.timedelta(minutes=1):
            d["maintenance"] = mk
    update_state(fn)


def mark(cmd, key):
    update_state(lambda st: st.setdefault("schedule_done", {}).update({cmd: key}))


def next_runs(cfg):
    """Naechste Termine als Text (fuer Menue und Weboberflaeche)."""
    s = cfg["schedule"]
    sk, sync_at, mk, start, end, is_day, now = slots(cfg)
    done = load_state().get("schedule_done", {})
    nxt_sync = sync_at if (sync_at > now or (done.get("repo-sync") != sk and now < sync_at + _dt.timedelta(hours=SYNC_CATCHUP_H))) \
        else sync_at + _dt.timedelta(days=1)
    try:
        days = parse_weekdays(s["weekdays"])
    except ValueError:
        days = []
    nxt_win = None
    for add in range(0, 8):
        d = now + _dt.timedelta(days=add)
        st_ = _today_at(d, s["start"])
        en_ = _today_at(d, s["end"])
        if WEEKDAYS[d.weekday()] not in days:
            continue
        if add == 0 and (now >= en_ or (now >= st_ and done.get("maintenance") == mk)):
            continue
        nxt_win = st_
        break
    fmt = "%a %d.%m. %H:%M"
    out = [f"Repo-Sync: taeglich {s['repo_sync']}  ->  naechster Lauf {nxt_sync.strftime(fmt)}"]
    if nxt_win:
        running = nxt_win <= now
        out.append(f"Wartungsfenster: {weekdays_de(s['weekdays'])} {s['start']}-{s['end']}  ->  "
                   + ("laeuft jetzt (Lauf startet innerhalb einer Minute)" if running else
                      f"naechster Lauf {nxt_win.strftime(fmt)}"))
    return out


def tick(cfg):
    """Vom Minuten-Timer aufgerufen. Startet faellige Laeufe nacheinander."""
    from . import cli
    jobs = due(cfg)
    for cmd, key in jobs:
        mark(cmd, key)          # vor dem Start: ein Absturz darf keine Endlosschleife ausloesen
        print(f"Zeitplan: starte {cmd} ({key})", flush=True)
        cli.main([cmd])
    return 0
