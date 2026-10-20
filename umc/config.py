"""Konfiguration (conf/config.json): Standardwerte, Laden, Speichern, Pruefregeln."""
import copy
import json
import os
import re

from .common import CONF_FILE, SECRETS_DIR, parse_weekdays

MASTER_DEFAULT = {
    "name": "zentrale",          # Anzeigename
    "host": "",                  # IP/Hostname aus Sicht des Update-Servers
    "site": "",                  # Checkmk-Site auf dem Master
    "ssh_port": 22,
    "jumphost": "",
    "checkmk_host": "",          # Hostname des Masters IN Checkmk (fuer Downtime)
    "api_url": "",
    "api_user": "svc-update",
    "api_secret_file": str(SECRETS_DIR / "cmk-secret"),
    "api_verify_tls": True,
}

CUSTOMER_DEFAULT = {
    "name": "",
    "host": "",
    "site": "",                  # Remote Site (leer = nur OS-Updates)
    "ssh_port": 22,
    "jumphost": "",
    "checkmk_host": "",          # Hostname der VM in Checkmk (fuer Downtime)
    "enabled": True,
    "notes": "",
}

DEFAULTS = {
    "version": 1,
    "repo": {
        "root": "/srv/repo",
        "filter_mode": "packages",       # apt: packages = nur benoetigte Pakete, full = alles
        "gpg_key": "",
        "listen_host": "127.0.0.1",      # Repo aus Sicht des Update-Servers
        "listen_port": 80,
        "tunnel_port": 3142,             # Port auf den Satelliten
        "keep_snapshots": 8,
        "sync_retries": 3,
        "sync_retry_wait_s": 120,
        "sync_timeout_min": 240,         # max. Dauer je Spiegel-Abgleich, danach Abbruch
    },
    "ssh": {
        "user": "svc-update",
        "key": "~/.ssh/svc-update",
        "connect_timeout": 10,
        "jumphost": "",
    },
    "os_profiles": {},                   # automatisch erkannte Betriebssysteme (id -> Spiegel)
    "master": copy.deepcopy(MASTER_DEFAULT),
    "customers": [],
    "checkmk": {
        "edition": "community",
        "auto_patch": True,
        "credentials_file": str(SECRETS_DIR / "checkmk-download"),
        "verify_signature": True,        # Checkmk-Pakete nur mit gueltiger Checkmk-Signatur (GnuPG)
        "signing_fprs": [],              # erlaubte Schluessel; leer = offizieller Checkmk-Schluessel
        "backup": "no-past",             # full | no-past | none
        "keep_backups": 3,
        "auto_rollback": True,
        "major_download": True,          # neue Major-Versionen sofort ins Repo laden
        "major_auto": True,              # Major-Updates automatisch im Wartungsfenster (mit Regelpruefung)
        "min_patch": {},                 # optional: {"2.6.0": "2.5.0p20"} Mindest-Patchstand vor Major-Schritt
        "repo_cleanup": True,            # Checkmk-Pakete aus dem Repo loeschen, sobald keine Site sie mehr braucht
        "omd_cleanup": True,             # nach erfolgreichem Update ungenutzte Checkmk-Versionen auf dem System entfernen
    },
    "schedule": {
        "weekdays": "Mon",
        "start": "09:00",
        "end": "12:00",
        "timezone": "Europe/Berlin",
        "repo_sync": "05:30",
        "os_update": True,
        "include_master_os": True,
        "checkmk_update": True,
        "min_minutes_left": 20,
    },
    "updates": {
        "batch_size": 0,                 # Kunden gleichzeitig (OS + Checkmk), 0 = alle auf einmal
        "downtime_min": 60,
        "reboot_timeout_min": 15,
        "site_start_timeout_min": 5,
    },
    "ntp_server": "",
    "notify": {
        "teams": False,                  # Benachrichtigung in einen Teams-Kanal (Webhook)
        "teams_webhook_file": str(SECRETS_DIR / "teams-webhook"),
        "mode": "always",                # always = nach jedem Lauf, problems = nur bei Fehlern/Hinweisen
        "manual_runs": False,            # auch bei Laeufen, die im Menue gestartet wurden
        "checkmk_localcheck": False,     # Status als Checkmk-Services auf dem Update-Server
    },
    "lab": {
        "enabled": False,                # Labormodus: Testsysteme zuruecksetzen (nur Testumgebung!)
    },
    "web": {
        "listen": "127.0.0.1",           # 127.0.0.1 = nur lokal (Zugriff per SSH-Tunnel), 0.0.0.0 = im Netz
        "port": 8470,
        "tls": False,                    # HTTPS (Zertifikat: updatemanager-checkmk web tls)
        "cert": str(SECRETS_DIR / "web-cert.pem"),
        "key": str(SECRETS_DIR / "web-key.pem"),
        "session_hours": 12,
    },
}


def default_config():
    return copy.deepcopy(DEFAULTS)


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load():
    if not CONF_FILE.exists():
        return None
    data = json.loads(CONF_FILE.read_text())
    cfg = _merge(default_config(), data)
    cfg["customers"] = [_merge(copy.deepcopy(CUSTOMER_DEFAULT), c) for c in cfg.get("customers", [])]
    migrate(cfg)
    return cfg


def migrate(cfg):
    """Aeltere Konfigurationen (nur Ubuntu, Version 0.1) in OS-Profile umwandeln."""
    cfg["checkmk"].pop("require_hash", None)      # bis 0.4.5: durch die Signaturpruefung ersetzt
    r = cfg["repo"]
    legacy = {k: r.pop(k) for k in ("ubuntu_mirror", "dists", "components", "codename") if k in r}
    if legacy.get("codename") and not cfg["os_profiles"]:
        cn = legacy["codename"]
        pid = f"ubuntu-{cn}"
        cfg["os_profiles"][pid] = {
            "id": pid, "family": "apt", "distro": "ubuntu", "version": "", "codename": cn,
            "label": f"Ubuntu ({cn})", "cmk_tag": cn, "enabled": True, "note": "", "repos": [],
            "mirrors": [{"dist": d, "url": legacy.get("ubuntu_mirror", "http://archive.ubuntu.com/ubuntu"),
                         "components": legacy.get("components", ["main", "universe"])}
                        for d in legacy.get("dists", [cn, f"{cn}-updates", f"{cn}-security"])],
        }


def save(cfg):
    CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONF_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o640)
    os.replace(tmp, CONF_FILE)


def new_customer():
    return copy.deepcopy(CUSTOMER_DEFAULT)


def find_customer(cfg, name):
    return next((c for c in cfg["customers"] if c["name"] == name), None)


def master_target(cfg):
    m = dict(cfg["master"])
    m.setdefault("name", "master")
    return m


def api_url_default(host, site, https=False):
    if not host or not site:
        return ""
    return f"{'https' if https else 'http'}://{host}/{site}/check_mk/api/1.0"


# ---------------------------------------------------------------- Pruefregeln
def v_name(s):
    return None if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}", s) else \
        "Nur Buchstaben, Ziffern und _ . - (max. 41 Zeichen)"


def v_site(s):
    return None if re.fullmatch(r"[a-z][a-z0-9_]{0,15}", s) else \
        "Site-Name: Kleinbuchstaben, Ziffern, _ ; beginnt mit Buchstabe; max. 16 Zeichen"


def v_site_opt(s):
    return None if s == "" else v_site(s)


def v_host(s):
    return None if re.fullmatch(r"[A-Za-z0-9_.:\-\[\]]+", s) else "Ungueltige Adresse"


def v_time(s):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return "Format HH:MM, z. B. 09:00"
    return None


def norm_time(s):
    h, m = s.split(":")
    return f"{int(h):02d}:{int(m):02d}"


def v_weekdays(s):
    try:
        parse_weekdays(s)
        return None
    except ValueError as e:
        return str(e)
