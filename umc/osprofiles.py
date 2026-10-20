"""Betriebssysteme: Erkennung, Profile (welcher Spiegel), Befehle je Paketmanager.

Unterstuetzt werden alle Systeme, auf denen Checkmk als Server/Remote Site laeuft:
  apt    : Ubuntu, Debian
  dnf    : RHEL, Rocky Linux, AlmaLinux, Oracle Linux, CentOS Stream
  zypper : SLES, openSUSE Leap
"""
import re

DETECT_CMD = "cat /etc/os-release; echo UMC_ARCH=$(uname -m); echo UMC_KERNEL=$(uname -r)"


# ================================================================ Erkennung
def parse_os_release(text):
    d = {}
    for line in text.splitlines():
        m = re.match(r'^([A-Z_]+)=(.*)$', line.strip())
        if m:
            d[m[1]] = m[2].strip().strip('"').strip("'")
    return d


def family_of(osr):
    ids = [osr.get("ID", "").lower()] + osr.get("ID_LIKE", "").lower().split()
    if any(i in ("ubuntu", "debian") for i in ids):
        return "apt"
    if any(i in ("rhel", "centos", "fedora", "rocky", "almalinux", "ol") for i in ids):
        return "dnf"
    if any(i in ("suse", "sles", "opensuse", "opensuse-leap") for i in ids):
        return "zypper"
    return None


def _major(v):
    return (v or "").split(".")[0]


def profile_id(osr):
    fam, did = family_of(osr), osr.get("ID", "").lower()
    if fam == "apt":
        return f"{did}-{osr.get('VERSION_CODENAME', '')}"
    if fam == "dnf":
        return f"{did}-{_major(osr.get('VERSION_ID'))}"
    if fam == "zypper":
        return f"{did}-{osr.get('VERSION_ID', '')}"
    return None


def os_label(osr):
    return osr.get("PRETTY_NAME") or f"{osr.get('ID', '?')} {osr.get('VERSION_ID', '')}"


def cmk_tag(osr):
    """Kennung im Dateinamen der Checkmk-Pakete (noble, bookworm, el9, sles15sp6)."""
    fam = family_of(osr)
    if fam == "apt":
        return osr.get("VERSION_CODENAME", "")
    if fam == "dnf":
        return f"el{_major(osr.get('VERSION_ID'))}"
    if fam == "zypper":
        v = osr.get("VERSION_ID", "15")
        maj, _, mi = v.partition(".")
        return f"sles{maj}sp{mi or 0}"
    return ""


# ================================================================ Standard-Spiegel (Katalog)
_EL_REPOS = {
    "rocky": lambda m: [("baseos", f"https://dl.rockylinux.org/pub/rocky/{m}/BaseOS/x86_64/os/"),
                        ("appstream", f"https://dl.rockylinux.org/pub/rocky/{m}/AppStream/x86_64/os/"),
                        ("crb", f"https://dl.rockylinux.org/pub/rocky/{m}/CRB/x86_64/os/")],
    "almalinux": lambda m: [("baseos", f"https://repo.almalinux.org/almalinux/{m}/BaseOS/x86_64/os/"),
                            ("appstream", f"https://repo.almalinux.org/almalinux/{m}/AppStream/x86_64/os/"),
                            ("crb", f"https://repo.almalinux.org/almalinux/{m}/CRB/x86_64/os/")],
    "ol": lambda m: [("baseos", f"https://yum.oracle.com/repo/OracleLinux/OL{m}/baseos/latest/x86_64/"),
                     ("appstream", f"https://yum.oracle.com/repo/OracleLinux/OL{m}/appstream/x86_64/")],
    "centos": lambda m: [("baseos", f"https://mirror.stream.centos.org/{m}-stream/BaseOS/x86_64/os/"),
                         ("appstream", f"https://mirror.stream.centos.org/{m}-stream/AppStream/x86_64/os/"),
                         ("crb", f"https://mirror.stream.centos.org/{m}-stream/CRB/x86_64/os/")],
}


def default_profile(osr):
    """Neues Profil mit Standard-Spiegeln fuer ein erkanntes System (oder None)."""
    fam = family_of(osr)
    if not fam:
        return None
    did = osr.get("ID", "").lower()
    ver = osr.get("VERSION_ID", "")
    p = {"id": profile_id(osr), "family": fam, "distro": did, "version": ver,
         "codename": osr.get("VERSION_CODENAME", ""), "label": os_label(osr),
         "cmk_tag": cmk_tag(osr), "enabled": True, "note": "",
         "mirrors": [], "repos": []}
    if fam == "apt":
        c = p["codename"]
        if did == "ubuntu":
            u = "http://archive.ubuntu.com/ubuntu"
            p["mirrors"] = [{"dist": d, "url": u, "components": ["main", "universe"]}
                            for d in (c, f"{c}-updates", f"{c}-security")]
        else:  # debian und Ableger
            p["mirrors"] = [
                {"dist": c, "url": "http://deb.debian.org/debian", "components": ["main"]},
                {"dist": f"{c}-updates", "url": "http://deb.debian.org/debian", "components": ["main"]},
                {"dist": f"{c}-security", "url": "http://security.debian.org/debian-security", "components": ["main"]},
            ]
    elif fam == "dnf":
        m = _major(ver)
        maker = _EL_REPOS.get(did)
        if maker:
            p["repos"] = [{"name": n, "url": u, "enabled": n != "crb"} for n, u in maker(m)]
            p["note"] = "Falls Checkmk Abhaengigkeiten fehlen: Repo 'crb' aktivieren bzw. EPEL ergaenzen."
        else:
            p["note"] = ("Keine oeffentlichen Spiegel bekannt (z. B. RHEL mit Subscription) – "
                         "Repo-URLs im Menue Repository -> Betriebssysteme eintragen.")
    elif fam == "zypper":
        if did.startswith("opensuse"):
            p["repos"] = [
                {"name": "oss", "url": f"https://download.opensuse.org/distribution/leap/{ver}/repo/oss/", "enabled": True},
                {"name": "update", "url": f"https://download.opensuse.org/update/leap/{ver}/oss/", "enabled": True},
            ]
        else:
            p["note"] = "SLES-Repos benoetigen SUSE-Zugangsdaten (SCC) – URLs inkl. Token im Menue eintragen."
    return p


# ================================================================ Befehle je Paketmanager
APT_UPGRADE = (
    "export DEBIAN_FRONTEND=noninteractive; "
    "sudo apt-get -q update && sudo apt-get -q -y "
    "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "
    "-o APT::Get::Always-Include-Phased-Updates=true dist-upgrade"
)

ADAPTERS = {
    "apt": {
        "pm": "/usr/bin/apt-get",
        "upgrade": APT_UPGRADE,
        "install": "export DEBIAN_FRONTEND=noninteractive; sudo apt-get -q update && sudo apt-get -q -y install {pkg}",
        "reboot_check": "test -f /var/run/reboot-required",
        "pending": ("sudo apt-get -q update >/dev/null 2>&1; apt-get -s -o APT::Get::Always-Include-Phased-Updates=true "
                    "dist-upgrade 2>/dev/null | grep -c '^Inst'"),
    },
    "dnf": {
        "pm": "/usr/bin/dnf",
        "upgrade": "sudo dnf -y --refresh upgrade",
        "install": "sudo dnf -y --refresh install {pkg}",
        "reboot_check": ("if command -v needs-restarting >/dev/null 2>&1; then needs-restarting -r >/dev/null 2>&1; "
                         "[ $? -eq 1 ]; else k=$(rpm -q --last kernel-core kernel 2>/dev/null | head -1 | awk '{print $1}' "
                         "| sed -E 's/^kernel(-core)?-//'); [ -n \"$k\" ] && [ \"$k\" != \"$(uname -r)\" ]; fi"),
        "pending": "sudo dnf -q --refresh check-update 2>/dev/null | awk 'NF==3 && $1 ~ /\\./' | wc -l",
    },
    "zypper": {
        "pm": "/usr/bin/zypper",
        "upgrade": ("sudo zypper --non-interactive --gpg-auto-import-keys refresh && "
                    "sudo zypper --non-interactive update --auto-agree-with-licenses"),
        "install": ("sudo zypper --non-interactive --gpg-auto-import-keys refresh && "
                    "sudo zypper --non-interactive install --auto-agree-with-licenses {pkg}"),
        "reboot_check": "zypper needs-rebooting >/dev/null 2>&1; [ $? -eq 102 ] || test -f /run/reboot-needed",
        "pending": ("sudo zypper --non-interactive -q refresh >/dev/null 2>&1; "
                    "zypper --non-interactive -q list-updates 2>/dev/null | grep -c '^v '"),
    },
}


def adapter(family):
    return ADAPTERS.get(family or "apt", ADAPTERS["apt"])


def repo_probe_path(profile):
    """Datei, die ueber den Tunnel erreichbar sein muss (Vorpruefung)."""
    if not profile:
        return None
    if profile["family"] == "apt":
        return f"/{profile['distro']}/dists/{profile['codename']}/Release"
    first = next((r["name"] for r in profile.get("repos", []) if r.get("enabled", True)), None)
    return f"/rpm/{profile['id']}/current/{first}/repodata/repomd.xml" if first else None


def checkmk_file_regex(edition, ver, profile):
    if profile["family"] == "apt":
        return re.compile(rf"check-mk-{edition}-{re.escape(ver)}_0\.{re.escape(profile['cmk_tag'])}_amd64\.deb")
    return re.compile(rf"check-mk-{edition}-{re.escape(ver)}-{re.escape(profile['cmk_tag'])}-\d+\.x86_64\.rpm")


def checkmk_file_fallback(edition, ver, profile):
    if profile["family"] == "apt":
        return f"check-mk-{edition}-{ver}_0.{profile['cmk_tag']}_amd64.deb"
    return f"check-mk-{edition}-{ver}-{profile['cmk_tag']}-38.x86_64.rpm"


# ================================================================ Editionen
LEGACY_SUFFIX = {"cre": "raw", "cee": "enterprise", "cce": "cloud", "cme": "managed", "cse": "saas"}


def edition_of_site_version(full):
    """'2.5.0p11.ultimate' -> 'ultimate', '2.3.0p1.cee' -> 'enterprise'."""
    m = re.match(r"^\d+\.\d+\.\d+(?:p\d+)?\.?(.*)$", full or "")
    suffix = (m.group(1) if m else "").strip(".").lower()
    return LEGACY_SUFFIX.get(suffix, suffix) or None


# Rangfolge fuer Editionswechsel (nur aufwaerts erlaubt). MSP-Editionen nur manuell.
EDITION_RANK = {"raw": 0, "community": 0, "enterprise": 1, "pro": 1, "cloud": 2, "ultimate": 2}
EDITION_MANUAL = {"managed", "ultimatemt", "msp", "saas"}


def edition_change_allowed(old, new):
    """(erlaubt, begruendung)."""
    if old == new:
        return False, "Edition ist bereits eingestellt"
    if new in EDITION_MANUAL or old in EDITION_MANUAL:
        return False, "Wechsel von/zu MSP-Editionen nur manuell (eigene Mandanten-Struktur)"
    if old not in EDITION_RANK or new not in EDITION_RANK:
        return False, f"Unbekannte Edition ({old} -> {new}); bekannt: {', '.join(sorted(EDITION_RANK))}"
    if EDITION_RANK[new] < EDITION_RANK[old]:
        return False, (f"{old} -> {new} waere ein Wechsel auf eine kleinere Edition – Funktionen und Daten "
                       "gingen verloren; nur manuell")
    if EDITION_RANK[new] == EDITION_RANK[old]:
        return False, f"{old} und {new} sind dieselbe Edition (alter/neuer Name)"
    return True, ""
