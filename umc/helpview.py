"""Hilfe: Markdown lesbar im Terminal darstellen, Kapitelauswahl."""
import os
import re
import shutil
import subprocess
import tempfile
import textwrap

from .common import COLOR, DOCS_DIR, bold, cyan, dim, strip_ansi, yellow

HELP_FILE = DOCS_DIR / "HILFE.md"


def _inline(s):
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", s)
    s = re.sub(r"\*\*([^*]+)\*\*", lambda m: bold(m.group(1)), s)
    s = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", lambda m: cyan(m.group(1)), s)
    s = re.sub(r"`([^`]+)`", lambda m: cyan(m.group(1)), s)
    return s


def _plain(s):
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", s)
    s = s.replace("**", "").replace("`", "")
    return re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"\1", s)


def _table(rows):
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows
             if not re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", r)]
    if not cells:
        return []
    n = max(len(r) for r in cells)
    cells = [r + [""] * (n - len(r)) for r in cells]
    width = shutil.get_terminal_size((100, 20)).columns
    w = [max(len(_plain(r[i])) for r in cells) for i in range(n)]
    # letzte Spalte umbrechen, wenn die Tabelle zu breit ist
    room = max(20, width - 4 - sum(w[:-1]) - 2 * (n - 1))
    out = []
    for k, r in enumerate(cells):
        last = _plain(r[-1])
        chunks = textwrap.wrap(last, room) or [""]
        for j, chunk in enumerate(chunks):
            parts = []
            for i in range(n - 1):
                txt = _plain(r[i]) if j == 0 else ""
                parts.append(txt.ljust(w[i]))
            line = "  " + "  ".join(parts + [chunk])
            out.append(bold(line) if k == 0 else line)
        if k == 0:
            out.append("  " + "  ".join(["─" * x for x in w[:-1]] + ["─" * min(w[-1], room)]))
    return out


def render(md):
    out, code, tbl = [], False, []
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            if tbl:
                out += _table(tbl)
                tbl = []
            code = not code
            continue
        if code:
            out.append("    " + dim(line) if COLOR else "    " + line)
            continue
        if line.lstrip().startswith("|"):
            tbl.append(line)
            continue
        if tbl:
            out += _table(tbl)
            tbl = []
        if line.startswith("# "):
            out += ["", bold(cyan(line[2:].upper())), ""]
        elif line.startswith("## "):
            t = line[3:]
            out += ["", bold(cyan(t)), cyan("─" * len(t))]
        elif line.startswith("### "):
            out += ["", bold(line[4:])]
        elif line.strip() == "---":
            out.append("")
        elif re.match(r"^\s*- ", line):
            ind = len(line) - len(line.lstrip())
            out.append(" " * ind + "  • " + _inline(line.lstrip()[2:]))
        else:
            out.append(_inline(line))
    if tbl:
        out += _table(tbl)
    return "\n".join(out) + "\n"


def sections():
    """[(titel, markdown)] – Kapitel = '## '-Ueberschriften."""
    if not HELP_FILE.exists():
        return []
    text = HELP_FILE.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^## ", text)
    res = [("Ueberblick", parts[0])]
    for p in parts[1:]:
        title, _, body = p.partition("\n")
        res.append((title.strip(), f"## {title}\n{body}"))
    return res


def show(md):
    text = render(md)
    if os.isatty(1) and shutil.which("less"):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(text)
            path = f.name
        try:
            print(dim("Blaettern: Leertaste / Pfeiltasten   Suchen: /Begriff   Zurueck: q"))
            subprocess.call(["less", "-R", "-F", "-X", path])
        except KeyboardInterrupt:
            pass
        finally:
            os.unlink(path)
    else:
        print(text if COLOR else strip_ansi(text))


def show_all():
    if HELP_FILE.exists():
        show(HELP_FILE.read_text(encoding="utf-8"))
    else:
        print(yellow(f"Hilfedatei fehlt: {HELP_FILE} – bitte install.sh erneut ausfuehren."))
