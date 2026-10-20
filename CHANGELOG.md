# Änderungen

## 0.4.4 – erste öffentliche Version

- Weboberfläche (optional): Übersicht, Verbindungen, Repository, Läufe mit Live-Ausgabe und Abbrechen,
  Zeitplan, Einstellungen, Hilfe, Labormodus; hell und dunkel; eigene Benutzer, HTTPS optional
- Zeitplan über einen Minuten-Timer: Änderungen in Menü oder Weboberfläche gelten sofort, ohne sudo
- Alle Kunden werden gleichzeitig aktualisiert (OS und Checkmk), der Master zuletzt; Anzahl einstellbar
- OS-Update-Bericht mit den aktualisierten Paketen, Neustart und Kernelwechsel
- Automatisches Aufräumen alter Checkmk-Versionen im Repo und auf den Systemen (`omd cleanup`), auch manuell
- Benachrichtigungen: Microsoft-Teams-Karte nach jedem Lauf, Status als Checkmk-Services (Local Check)
- Labormodus: Testsysteme auf ältere Checkmk- und OS-Stände zurücksetzen
- Vor dem Checkmk-Schritt wird erneut nach neuen Releases gesucht, falls die letzte Suche älter als 6 Stunden ist
- Repo-Sync und Wartungslauf warten aufeinander statt abzubrechen

## 0.3 und früher

- Offline-Repository je Betriebssystem (aptly, dnf-reposync), Snapshots, GPG-Signatur
- Checkmk-Patch- und Major-Updates mit Regelprüfung, Backup und automatischem Rollback
- Menü mit Einrichtungsassistent, Vorbereitungs-Skripte für Master und Kunden
