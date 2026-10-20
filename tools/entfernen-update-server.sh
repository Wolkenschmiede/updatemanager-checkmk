#!/bin/bash
# =====================================================================
#  updatemanager-checkmk – Update-Server komplett zurücksetzen
#
#  Entfernt Programm, Einstellungen (Master, Kunden, Zeitplan …), Zustand,
#  Logs, Geheimnisse, SSH- und Repo-Schlüssel, Repo-Daten, Zeitplan und
#  Weboberfläche. Danach kann alles neu eingerichtet werden.
#
#  Vorher auf Master und Kunden 'entfernen-system.sh' ausführen –
#  dafür wird der SSH-Zugang des Update-Servers nicht gebraucht.
#
#  Aufruf (als der Benutzer, der das Programm eingerichtet hat, NICHT als root):
#     bash entfernen-update-server.sh
# =====================================================================
set -uo pipefail
APP=/opt/updatemanager-checkmk
SECRETS="$HOME/.config/updatemanager-checkmk"
CONF="$APP/conf/config.json"

if [ "$(id -u)" -eq 0 ]; then
  echo "Bitte als normaler Benutzer ausführen (nicht als root) – sudo wird bei Bedarf abgefragt."; exit 1
fi

# Werte aus der Konfiguration lesen, solange sie noch da ist
REPO_ROOT=/srv/repo; GPG_KEY=""; SSH_KEY="$HOME/.ssh/svc-update"; HOSTS=""
if [ -f "$CONF" ]; then
  eval "$(python3 - "$CONF" <<'PY'
import json, os, shlex, sys
c = json.load(open(sys.argv[1]))
r, s = c.get("repo", {}), c.get("ssh", {})
hosts = [c.get("master", {}).get("host", "")] + [k.get("host", "") for k in c.get("customers", [])]
print("REPO_ROOT=" + shlex.quote(r.get("root") or "/srv/repo"))
print("GPG_KEY=" + shlex.quote(r.get("gpg_key") or ""))
print("SSH_KEY=" + shlex.quote(os.path.expanduser(s.get("key") or "~/.ssh/svc-update")))
print("HOSTS=" + shlex.quote(" ".join(h for h in hosts if h)))
PY
)"
fi
case "$REPO_ROOT" in /|/srv|/home|/root|/opt|/usr|/etc|"") echo "Repo-Pfad '$REPO_ROOT' sieht falsch aus – Abbruch."; exit 1 ;; esac

echo "Folgendes wird gelöscht:"
echo "  Programm und Einstellungen   $APP  (+ /usr/local/bin/updatemanager-checkmk)"
echo "  Geheimnisse                  $SECRETS"
echo "  SSH-Schlüssel                $SSH_KEY(.pub)"
echo "  Repo-Daten                   $REPO_ROOT"
echo "  Repo-Signierschlüssel (GPG)  ${GPG_KEY:-(nicht konfiguriert)}"
echo "  Zeitplan, Weboberfläche, nginx-Seite, aptly-Konfiguration, NTP-Freigabe, Checkmk-Local-Check"
[ -n "$HOSTS" ] && echo "  known_hosts-Einträge         $HOSTS"
echo
read -r -p "Wirklich alles löschen? Zum Bestätigen JA eingeben: " a
[ "$a" = "JA" ] || { echo "Abgebrochen."; exit 1; }

if [ -s "$SECRETS/teams-webhook" ]; then
  read -r -p "Teams-Webhook-Adresse vorher in ~/teams-webhook-sicherung.txt aufheben? [J/n] " t
  if [ "${t:-J}" != "n" ] && [ "${t:-J}" != "N" ]; then
    install -m 600 "$SECRETS/teams-webhook" "$HOME/teams-webhook-sicherung.txt" && echo "    gesichert (Rechte 600)"
  fi
fi

sudo -v || exit 1

echo ">>> 1/7 Zeitplan und Weboberfläche stoppen"
UNITS="updatemanager-checkmk-zeitplan.timer updatemanager-checkmk-zeitplan.service updatemanager-checkmk-web.service
       updatemanager-checkmk-maintenance.timer updatemanager-checkmk-maintenance.service
       updatemanager-checkmk-reposync.timer updatemanager-checkmk-reposync.service"
for u in $UNITS; do sudo systemctl disable --now "$u" >/dev/null 2>&1; done
sudo rm -f /etc/systemd/system/updatemanager-checkmk-*.service /etc/systemd/system/updatemanager-checkmk-*.timer
sudo systemctl daemon-reload
sudo rm -f /usr/lib/check_mk_agent/local/updatemanager_checkmk

echo ">>> 2/7 Repo-Daten löschen ($REPO_ROOT)"
sudo rm -rf "$REPO_ROOT"

echo ">>> 3/7 nginx, aptly, NTP-Freigabe"
sudo rm -f /etc/nginx/sites-enabled/updatemanager-checkmk.conf /etc/aptly.conf /etc/chrony/conf.d/updatemanager-checkmk.conf
if [ -d /etc/nginx/sites-available ] && [ -f /etc/nginx/sites-available/default ] && [ -z "$(ls -A /etc/nginx/sites-enabled 2>/dev/null)" ]; then
  sudo ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
fi
systemctl is-active --quiet nginx && sudo systemctl reload nginx 2>/dev/null
systemctl is-active --quiet chrony && sudo systemctl restart chrony 2>/dev/null

echo ">>> 4/7 Repo-Signierschlüssel"
if [ -n "$GPG_KEY" ] && gpg --list-secret-keys "$GPG_KEY" >/dev/null 2>&1; then
  gpg --batch --yes --delete-secret-and-public-key "$GPG_KEY" && echo "    entfernt"
else
  echo "    keiner gefunden"
fi

echo ">>> 5/7 SSH-Schlüssel und known_hosts"
rm -f "$SSH_KEY" "$SSH_KEY.pub"
for h in $HOSTS; do ssh-keygen -R "$h" >/dev/null 2>&1; done

echo ">>> 6/7 Geheimnisse"
rm -rf "$SECRETS"

echo ">>> 7/7 Programm"
sudo rm -rf "$APP"
sudo rm -f /usr/local/bin/updatemanager-checkmk

echo
read -r -p "Auch die Pakete aptly und nginx deinstallieren (wie ein frischer Server)? [j/N] " p
if [ "$p" = "j" ] || [ "$p" = "J" ]; then
  sudo apt-get -y -q purge aptly nginx nginx-common >/dev/null && sudo apt-get -y -q autoremove >/dev/null && echo "    deinstalliert"
fi

echo
echo "Fertig. Neu installieren:"
echo "  git clone https://github.com/Wolkenschmiede/updatemanager-checkmk.git"
echo "  cd updatemanager-checkmk && ./install.sh && updatemanager-checkmk"
