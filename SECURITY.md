# Sicherheit

updatemanager-checkmk läuft auf einem hochprivilegierten System: Der Update-Server darf auf allen
angebundenen Systemen Pakete installieren und Checkmk steuern. Sicherheitsmeldungen nehmen wir deshalb ernst.

## Lücke melden

Bitte **kein öffentliches Issue** anlegen. Stattdessen über GitHub melden:
**Security → Report a vulnerability** (private Sicherheitsmeldung) in diesem Repository.

Hilfreich sind: betroffene Version, Beschreibung, Schritte zum Nachstellen und mögliche Auswirkungen.
Du bekommst eine Rückmeldung, sobald die Meldung gesichtet wurde.

## Unterstützte Versionen

Sicherheitskorrekturen gibt es für die jeweils neueste Version.

## Hinweise für den Betrieb

- Zugang zum Update-Server nur für Administratoren, Anmeldung per Schlüssel.
- Die Weboberfläche nur lokal betreiben (Standard) und per SSH-Tunnel öffnen, oder HTTPS einschalten.
- Geheimnisse liegen im Secrets-Verzeichnis (Rechte 600) und gehören nie in `conf/` oder ein Git-Repository.
- Den Teams-Webhook wie ein Passwort behandeln: Wer die URL kennt, kann in den Kanal schreiben.
