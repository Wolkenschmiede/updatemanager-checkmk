#!/bin/bash
# =====================================================================
#  updatemanager-checkmk – Vorbereitung auf einem System rückgängig machen
#
#  Für Checkmk-Master und Kunden-VMs, die mit dem Vorbereitungs-Skript
#  eingerichtet wurden. Danach ist das System wieder so wie vorher:
#    - ursprüngliche Paketquellen zurück, Repo-Schlüssel entfernt
#    - Service-Benutzer (Standard: svc-update) samt Home und sudo-Regel entfernt
#    - automatische Updates wieder eingeschaltet
#    - Zeitquelle (timesyncd) wieder auf Standard
#  Checkmk und die Sites bleiben unverändert.
#
#  Aufruf:  sudo bash entfernen-system.sh [SERVICE-BENUTZER]
# =====================================================================
set -uo pipefail
SVC_USER="${1:-svc-update}"
BK=/root/updatemanager-backup-sources

[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen."; exit 1; }
. /etc/os-release
FAMILY=""
case " ${ID:-} ${ID_LIKE:-} " in
  *" debian "*|*" ubuntu "*) FAMILY=apt ;;
  *" rhel "*|*" fedora "*|*" centos "*|*" rocky "*|*" almalinux "*|*" ol "*) FAMILY=dnf ;;
  *" suse "*|*" sles "*|*" opensuse "*|*" opensuse-leap "*) FAMILY=zypper ;;
esac
[ -n "$FAMILY" ] || { echo "Nicht unterstütztes System: ${PRETTY_NAME:-?}"; exit 1; }

echo "System: ${PRETTY_NAME:-?} ($FAMILY)"
echo "Entfernt wird: Benutzer '$SVC_USER' (inkl. Home und Site-Backups darin), sudo-Regel,"
echo "Repo-Schlüssel und Paketquellen von updatemanager-checkmk. Checkmk bleibt unverändert."
if [ ! -d "$BK" ]; then
  echo "HINWEIS: $BK fehlt – die ursprünglichen Paketquellen können nicht zurückgeholt werden."
fi
read -r -p "Fortfahren? Zum Bestätigen JA eingeben: " a
[ "$a" = "JA" ] || { echo "Abgebrochen."; exit 1; }

echo ">>> 1/5 Paketquellen zurück"
case $FAMILY in
apt)
  rm -f /etc/apt/sources.list.d/updatemanager.sources
  if [ -d "$BK" ]; then
    for f in "$BK"/sources.list "$BK"/*.sources "$BK"/*.list; do
      [ -e "$f" ] || continue
      if [ "$(basename "$f")" = sources.list ]; then cp "$f" /etc/apt/sources.list && rm -f "$f"
      else mv "$f" /etc/apt/sources.list.d/; fi
      echo "    zurück: $(basename "$f")"
    done
  fi
  rm -f /etc/apt/keyrings/updatemanager-repo.asc
  ;;
dnf)
  rm -f /etc/yum.repos.d/updatemanager-checkmk.repo
  [ -d "$BK" ] && for f in "$BK"/*.repo; do [ -e "$f" ] && mv "$f" /etc/yum.repos.d/ && echo "    zurück: $(basename "$f")"; done
  rm -f /etc/pki/rpm-gpg/RPM-GPG-KEY-updatemanager-checkmk
  ;;
zypper)
  rm -f /etc/zypp/repos.d/updatemanager-*.repo
  [ -d "$BK" ] && for f in "$BK"/*.repo; do [ -e "$f" ] && mv "$f" /etc/zypp/repos.d/ && echo "    zurück: $(basename "$f")"; done
  rm -f /etc/pki/rpm-gpg/RPM-GPG-KEY-updatemanager-checkmk
  ;;
esac
[ -d "$BK" ] && rmdir "$BK" 2>/dev/null

echo ">>> 2/5 sudo-Regel und Service-Benutzer '$SVC_USER'"
rm -f /etc/sudoers.d/updatemanager-checkmk
if id "$SVC_USER" >/dev/null 2>&1; then
  pkill -u "$SVC_USER" 2>/dev/null; sleep 1; pkill -9 -u "$SVC_USER" 2>/dev/null
  userdel -r "$SVC_USER" 2>/dev/null || userdel "$SVC_USER"
  echo "    Benutzer entfernt"
else
  echo "    Benutzer gibt es nicht"
fi

echo ">>> 3/5 Automatische Updates wieder einschalten"
for u in apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service dnf-makecache.timer; do
  if systemctl list-unit-files "$u" 2>/dev/null | grep -q "^$u"; then
    systemctl enable --now "$u" >/dev/null 2>&1 && echo "    $u"
  fi
done

echo ">>> 4/5 Zeitquelle"
if grep -qE '^NTP=.+' /etc/systemd/timesyncd.conf 2>/dev/null; then
  sed -i -E 's/^NTP=.*/#NTP=/' /etc/systemd/timesyncd.conf
  systemctl restart systemd-timesyncd 2>/dev/null
  echo "    timesyncd wieder auf Standard (Server der Distribution)"
else
  echo "    nichts zu tun"
fi
for c in /etc/chrony.conf /etc/chrony/chrony.conf; do
  [ -f "$c" ] && grep -qE '^#(pool|server) ' "$c" && \
    echo "    HINWEIS: $c wurde evtl. angepasst – auskommentierte pool/server-Zeilen bei Bedarf wieder aktivieren."
done

echo ">>> 5/5 Paketlisten neu laden"
case $FAMILY in
  apt) apt-get -q update >/dev/null 2>&1 && echo "    ok" || echo "    nicht möglich (kein Internet?) – egal" ;;
  dnf) dnf -q makecache >/dev/null 2>&1 && echo "    ok" || echo "    nicht möglich (kein Internet?) – egal" ;;
  zypper) zypper -q refresh >/dev/null 2>&1 && echo "    ok" || echo "    nicht möglich (kein Internet?) – egal" ;;
esac

echo
echo "Fertig. Das System ist wieder ohne updatemanager-checkmk."
