"""Einfache Terminal-Dialoge: Fragen, Ja/Nein, Auswahlmenues, Tabellen."""
import getpass
import os
import shutil
import subprocess

from .common import bold, cyan, dim, red, strip_ansi, yellow


class Abort(Exception):
    """Benutzer hat abgebrochen (Strg+C / Strg+D)."""


def _input(prompt, secret=False):
    try:
        return getpass.getpass(prompt) if secret else input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        raise Abort()


def ask(label, default=None, required=True, validate=None, secret=False):
    while True:
        shown = "" if default in (None, "") or secret else f" [{default}]"
        v = _input(f"{label}{shown}: ", secret).strip()
        if not v and default not in (None,):
            v = str(default)
        if not v and required:
            print(red("  Eingabe erforderlich"))
            continue
        if validate and (v or required):
            err = validate(v)
            if err:
                print(red(f"  {err}"))
                continue
        return v


def ask_int(label, default, lo=None, hi=None):
    def check(s):
        if not s.lstrip("-").isdigit():
            return "Bitte eine Zahl eingeben"
        n = int(s)
        if lo is not None and n < lo:
            return f"Mindestens {lo}"
        if hi is not None and n > hi:
            return f"Hoechstens {hi}"
        return None
    return int(ask(label, default, validate=check))


def ask_yes(label, default=True):
    hint = "J/n" if default else "j/N"
    while True:
        v = _input(f"{label} [{hint}]: ").strip().lower()
        if not v:
            return default
        if v in ("j", "ja", "y", "yes"):
            return True
        if v in ("n", "nein", "no"):
            return False
        print(red("  Bitte j oder n"))


def ask_choice(label, options, default=None):
    """options: Liste von (wert, beschreibung)."""
    for i, (val, desc) in enumerate(options, 1):
        mark = " (aktuell)" if val == default else ""
        print(f"  {i}) {desc}{dim(mark)}")
    d = next((str(i) for i, (val, _) in enumerate(options, 1) if val == default), None)
    n = ask(label, d, validate=lambda s: None if s.isdigit() and 1 <= int(s) <= len(options)
            else f"1-{len(options)}")
    return options[int(n) - 1][0]


def header(title):
    width = min(shutil.get_terminal_size((80, 20)).columns, 78)
    print()
    print(bold(cyan(f"── {title} " + "─" * max(0, width - len(title) - 4))))


def menu(title, items, back="Zurueck", info=None):
    """items: Liste von (taste, text). Liefert die Taste oder None (= zurueck)."""
    header(title)
    if info:
        for line in info:
            print(f"  {line}")
        print()
    keys = {}
    for key, text in items:
        if key is None:
            print()
            continue
        keys[str(key).lower()] = key
        print(f"  {bold(key)}) {text}")
    print(f"  {bold('0')}) {back}")
    while True:
        v = _input("Auswahl: ").strip().lower()
        if v in ("0", "q"):
            return None
        if v in keys:
            return keys[v]
        print(red("  Ungueltige Auswahl"))


def table(headers, rows, indent="  "):
    rows = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(strip_ansi(c)))

    def fmt(cells):
        return indent + "  ".join(c + " " * (widths[i] - len(strip_ansi(c))) for i, c in enumerate(cells))
    print(bold(fmt(headers)))
    print(indent + "  ".join("─" * w for w in widths))
    for r in rows:
        print(fmt(r))
    if not rows:
        print(indent + dim("(keine Eintraege)"))


def pause():
    try:
        input(dim("\n[Enter] weiter ... "))
    except (EOFError, KeyboardInterrupt):
        print()


def page(path):
    """Datei anzeigen – mit less, falls vorhanden und Terminal interaktiv."""
    if os.isatty(1) and shutil.which("less"):
        print(dim("Blaettern: Leertaste/Pfeiltasten   Suchen: /Begriff   Beenden: q"))
        try:
            subprocess.call(["less", "-R", "-F", "-P", "Leertaste = weiter, q = beenden", str(path)])
        except KeyboardInterrupt:
            pass
    else:
        print(open(path, encoding="utf-8").read())


def warn_box(lines):
    print()
    for line in lines:
        print(yellow(f"  ! {line}"))
