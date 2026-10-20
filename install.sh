#!/bin/bash
# install.sh – installiert/aktualisiert updatemanager-checkmk nach /opt/updatemanager-checkmk
# Vorhandene Konfiguration (conf/), Logs und Zustand bleiben erhalten.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET=/opt/updatemanager-checkmk
RUN_USER="${SUDO_USER:-$USER}"

if [ "$(id -u)" -eq 0 ] && [ -z "${SUDO_USER:-}" ]; then
  echo "Bitte als normaler Benutzer ausfuehren (nicht als root) – sudo wird bei Bedarf abgefragt."
  exit 1
fi

command -v python3 >/dev/null || sudo apt-get install -y python3

sudo mkdir -p "$TARGET"
if [ "$SRC" != "$TARGET" ]; then
  # Nur Programmdateien kopieren – conf/, logs/, state/ nicht ueberschreiben
  sudo cp -r "$SRC/umc" "$SRC/docs" "$SRC/tools" "$SRC/updatemanager-checkmk" "$SRC/install.sh" "$SRC/README.md" "$TARGET/"
  [ -f "$SRC/.gitignore" ] && sudo cp "$SRC/.gitignore" "$TARGET/"
  sudo rm -rf "$TARGET/umc/__pycache__"
fi
sudo mkdir -p "$TARGET"/{conf,logs,state}
sudo chown -R "$RUN_USER": "$TARGET"
chmod +x "$TARGET/updatemanager-checkmk" "$TARGET/install.sh"
sudo ln -sf "$TARGET/updatemanager-checkmk" /usr/local/bin/updatemanager-checkmk
# laufende Weboberflaeche auf die neue Version umstellen
if systemctl is-active --quiet updatemanager-checkmk-web 2>/dev/null; then
  sudo systemctl restart updatemanager-checkmk-web && echo "Weboberflaeche neu gestartet."
fi

echo
echo "updatemanager-checkmk $(python3 -c "import sys; sys.path.insert(0,'$TARGET'); from umc import VERSION; print(VERSION)") installiert."
echo "Start:  updatemanager-checkmk        Hilfe:  updatemanager-checkmk --help"
