# updatemanager-checkmk – Programmbeschreibung

Zentrale Updates für Checkmk-Satelliten · Stand: Version 0.4.4

---

## 1. Ziel

Die Kunden-Satelliten (Linux-VMs mit Checkmk Remote Site und Agent) und der Checkmk-Master werden zentral aus dem NOC automatisch aktuell gehalten:

- **Betriebssystem und Checkmk** werden automatisch aktualisiert, sobald der Hersteller ein neues Release veröffentlicht, jeweils im nächsten Wartungsfenster.
- **Zugriff auf die Kunden ausschließlich per SSH aus dem NOC.** Die Kunden bauen keine Verbindung ins NOC auf (einzige optionale Ausnahme: Zeitsynchronisation, siehe Kapitel 9).

## 2. Grundprinzipien

- **Alles zentral im NOC:** Repo, Programm, Kundenliste, Logs und Berichte liegen auf einem Update-Server. Auf den Kunden-VMs liegen keine Skripte und keine Zugangsdaten.
- **Nur SSH zum Kunden:** Der Update-Server baut die Verbindung auf. Pakete laufen durch einen Reverse-Tunnel in dieser SSH-Verbindung.
- **Automatisch aktuell:** Neue Releases werden automatisch erkannt, ins Repo geladen und im nächsten Wartungsfenster ausgerollt. Es gibt keine manuelle Freigabe. Manuelle Läufe sind jederzeit über Menü oder Weboberfläche möglich.
- **Remote Sites vor Master:** Laut Checkmk-Doku werden zuerst alle Remote Sites aktualisiert und zuletzt der Master. Der Master wird nur aktualisiert, wenn alle Remote Sites erfolgreich waren.
- **Kunden gleichzeitig:** Alle Kunden werden parallel aktualisiert (OS und Checkmk). Die Anzahl gleichzeitiger Kunden ist einstellbar (0 = alle).

## 3. Update-Ströme

| Was | Quelle | Automatisierung |
|---|---|---|
| Betriebssystem der Satelliten und des Masters | Offline-Repo (Spiegel der OS-Quellen: Ubuntu/Debian, optional RHEL-artige und SUSE) | automatisch, im Wartungsfenster |
| Checkmk-Patch-Releases (z. B. 2.5.0p5 → p16) | Offline-Repo (eigener Checkmk-Bereich pro OS) | automatisch, im nächsten Wartungsfenster |
| Checkmk-Major-Releases (z. B. 2.5 → 2.6) | Offline-Repo | automatisch mit Regelprüfung (Kapitel 6); abschaltbar, dann nur Meldung |
| Checkmk-Agenten auf den Kunden-Hosts | Checkmk Agent Bakery (nur kommerzielle Editionen) | durch Checkmk, kein Repo nötig |

> Major-Updates laufen nur, wenn alle Voraussetzungen erfüllt sind: immer nur **eine** Major-Version pro Schritt, vorher alle Sites auf dem neuesten Patch-Stand, optional ein Mindest-Patchstand, und das Paket muss für das OS jeder Site verfügbar sein. Fehlt etwas (z. B. weil ein OS-Upgrade nötig ist), wird der Schritt blockiert und gemeldet. Mit der Einstellung `major_auto = aus` werden Major-Releases nur geladen und gemeldet.

## 4. Architektur

![Architektur](img/architektur.svg)

### Bausteine

- **Update-Server (eine Linux-VM im NOC)** vereint Repo und Steuerung:
  - **Repo:** spiegelt die OS-Quellen aller bei Kunden eingesetzten Distributionen (aptly für Ubuntu/Debian, dnf-reposync + createrepo für RPM-Systeme) und lädt neue Checkmk-Pakete aus dem Downloadportal. Jeder neue Stand wird als datierter Snapshot gesichert, signiert (GPG) und per nginx veröffentlicht. Die letzten 8 Snapshots dienen als Rückfallebene.
  - **Steuerung:** kennt alle Satelliten aus der Kundenliste, sammelt die Versionsstände ein, entscheidet, was zu tun ist, und führt die Updates per SSH aus.
  - **Zeitplan:** ein systemd-Timer ruft jede Minute `updatemanager-checkmk tick` auf. Änderungen am Zeitplan (Menü oder Weboberfläche) gelten sofort, ohne sudo.
  - **Bedienung:** CLI-Menü und eine abschaltbare Weboberfläche (Übersicht, Repo, Läufe, Zeitplan, Einstellungen, Hilfe).
  - **Berichte:** Log, Laufhistorie, Teams-Benachrichtigung, optional Checkmk-Services (Local Check) auf dem Update-Server.
- **Checkmk-Master:** wird per SSH aktualisiert (zuletzt). Über die REST-API setzt der Update-Server Downtimes für die betroffenen Hosts.

### Satelliten beim Kunden

- Als einzige Paketquelle ist das NOC-Repo eingetragen (`http://localhost:3142`), erreichbar nur über den SSH-Tunnel des Update-Servers.
- Ohne offenen Tunnel kann ein Satellit keine Pakete laden. Updates kommen damit nur aus dem gesteuerten Ablauf.

## 5. Neue Releases erkennen und bereitstellen

Der **Repo-Sync** läuft täglich zur eingestellten Uhrzeit (Standard 05:30, bis 6 Stunden danach wird er nachgeholt).

### Betriebssystem

1. Die Spiegel werden aktualisiert.
2. Gibt es Änderungen, wird ein neuer, datierter Snapshot erstellt (enthält jeweils den neuesten Stand jedes Pakets) und sofort veröffentlicht.
3. Im nächsten Wartungsfenster holen die Satelliten und der Master diesen Stand.

### Checkmk

1. Der Repo-Sync prüft im Checkmk-Downloadportal, ob ein neues Release verfügbar ist. Vor dem Checkmk-Schritt im Wartungsfenster wird erneut geprüft, falls die letzte Prüfung älter als 6 Stunden ist.
2. Neue Pakete werden für jedes verwendete OS heruntergeladen, gegen die SHA256-Prüfsummen von Checkmk geprüft und ins Checkmk-Repo aufgenommen.
3. Patch-Releases werden automatisch zur neuen Zielversion.
4. Major-Releases werden ebenfalls geladen und – sofern die Regeln erfüllt sind – im Wartungsfenster ausgerollt (siehe Kapitel 6).
5. Fehlschläge beim Sync oder Download werden per Teams gemeldet.

### Aufräumen

- **Repo:** Checkmk-Pakete werden gelöscht, sobald keine Site sie mehr braucht. Behalten werden immer: installierte Versionen, die Master-Version und alles, was neuer ist als der Master.
- **Sites:** Nach jedem erfolgreichen Update entfernt `omd cleanup` ungenutzte Checkmk-Versionen auf dem System.
- Beides ist abschaltbar und auch manuell (Menü / Weboberfläche) möglich.

## 6. Versionsvergleich

Vor jedem Lauf prüft das Programm jedes System (Vorprüfung) und sammelt die Stände aller Sites per SSH ein, auch den des Masters. Zielversion ist das neueste Patch-Release im Repo für das jeweilige OS.

| Situation | Beispiel | Aktion |
|---|---|---|
| Site gleich Zielversion | RS 2.5.0p16 · Ziel 2.5.0p16 | nichts |
| Site älter, gleiche Major-Version | RS 2.5.0p5 · Ziel 2.5.0p16 | Remote Site aktualisieren |
| Alle Remote Sites erfolgreich, Master älter | RS alle p16 · Master p5 | Master aktualisieren |
| Mindestens eine Remote Site nicht erfolgreich | Kunde nicht erreichbar, Update fehlgeschlagen | Master nicht aktualisieren, Bericht |
| Remote Site einen Major-Zweig hinter dem Master | Master 2.6 · RS 2.5 | Remote Site auf 2.6 nachziehen |
| Remote Site mehr als einen Zweig hinter dem Master | Master 2.6 · RS 2.4 | blockiert, manuell schrittweise |
| Neue Major-Version im Repo, alle Regeln erfüllt | alle Sites 2.5 neuester Patch, 2.6 verfügbar | Major-Schritt: erst Remote Sites, dann Master |
| Neue Major-Version, Regel verletzt | 2.6 nicht für das OS einer Site verfügbar | blockiert, Meldung (OS-Upgrade nötig) |
| Edition der Site weicht ab | Site „cloud“ statt „community“ | blockiert, Meldung |
| Vorprüfung fehlgeschlagen | SSH, sudo, Repo über Tunnel, Uhrzeit > 120 s Abweichung, Site fehlt | überspringen, Bericht |

## 7. Abläufe

![Abläufe: OS-Update (links) und Checkmk-Update (rechts)](img/ablauf.svg)

### Wartungslauf

Startet automatisch, sobald das Wartungsfenster beginnt (Standard Montag 09:00–12:00), genau einmal pro Fenster.

1. **OS-Update** – alle Kunden gleichzeitig, danach der Master (abschaltbar).
2. **Checkmk-Update** – alle Remote Sites gleichzeitig, danach der Master, nur wenn alle erfolgreich waren.
3. **Aufräumen und Bericht.**

Neue Teilschritte werden nur begonnen, wenn noch genügend Zeit im Fenster ist (Standard mindestens 20 Minuten).

### OS-Update

- Vorprüfung, Downtime setzen, Updates aus dem Repo einspielen, Neustart nur wenn nötig, prüfen ob die Site läuft, Downtime entfernen.
- Checkmk bleibt dabei unberührt, weil jede Checkmk-Version einen eigenen Paketnamen hat.
- Der Bericht nennt die aktualisierten Pakete (alt → neu), ob neu gestartet wurde und den Kernelwechsel.

### Checkmk-Update (pro Site)

1. Vorprüfung, Downtime im Master setzen.
2. Backup der Site (`omd backup`, standardmäßig ohne Verlaufsdaten; die letzten 3 bleiben auf dem Satelliten).
3. Neues Checkmk-Paket aus dem Repo installieren.
4. Site stoppen, auf die neue Version umstellen (`omd update`), starten.
5. Prüfen: richtige Version, Site läuft.
6. Bei Erfolg: `omd cleanup`, Downtime entfernen.
7. Bei Fehler: Hat Checkmk vorab abgebrochen, wird die unveränderte Site gestartet. Sonst wird die Site automatisch aus dem Backup zurückgesetzt (`omd restore`). Der Kunde wird im Bericht markiert, der Master nicht aktualisiert.

### Regeln für das Programm

- `omd update` läuft ohne `-f`: Die Sicherheitsprüfungen von Checkmk (Versionssperre, Editions-Prüfung, Vorabprüfung) bleiben aktiv. Bei Problemen in der Vorabprüfung bricht Checkmk ab, die alte Version läuft weiter.
- Bei Konflikten in Standard-Konfigurationsdateien wird die neue Checkmk-Vorlage übernommen (`--skeleton=install`). Lokale Änderungen an diesen Vorlagedateien gehen dabei verloren; das Backup davor ist die Absicherung.
- Vor jedem Checkmk-Update ein Backup. Zurück auf die alte Version geht laut Doku sauber nur über das Backup.
- Der Master wird nur aktualisiert, wenn alle Remote Sites erfolgreich waren.
- Major-Schritte nur nach den Regeln aus Kapitel 3 und 6.
- Repo-Sync und Wartungslauf laufen nie gleichzeitig; ein später Start wartet bis zu 3 Stunden auf den laufenden.

### Was die Checkmk-Doku vorgibt

- Zuerst alle Remote Sites, zuletzt den Master.
- Unterschiedliche Patch-Stände sind in der Regel erlaubt. Ausnahmen stehen in den Werks.
- Ein Master mit höherer Major-Version als die Remote Sites ist nicht erlaubt. Findet das Programm diesen Zustand vor, zieht es die Remote Sites sofort nach.
- Major-Updates nur schrittweise (eine Major-Version pro Schritt, vorher neuester Patch-Stand).

> Ohne Testphase landen Fehler eines Releases direkt bei den Kunden. Absicherung: Vorprüfung, Backup vor jedem Checkmk-Update mit automatischem Rollback, Snapshots im Repo als Rückfallebene und ein Bericht nach jedem Lauf.

## 8. Benachrichtigung und Überwachung

- **Teams:** Nach jedem automatischen Lauf (oder nur bei Problemen) eine Karte im Teams-Kanal über einen Workflows-Webhook: Ergebnis pro System, aktualisierte Pakete, blockierte Updates, Fehler beim Repo-Sync oder Checkmk-Download, nicht erreichbare Systeme.
- **Checkmk (optional):** Local Check auf dem Update-Server zeigt den Status der Läufe als Services.
- **Laufhistorie:** in der Weboberfläche mit vollständiger Ausgabe jedes Laufs.

## 9. Zugänge und Rechte

| Wo | Was |
|---|---|
| Satelliten und Master | eigener Benutzer für Updates, Anmeldung nur per SSH-Key |
| Satelliten und Master | sudo nur für Paketverwaltung (`apt-get`/`dnf`/`zypper`), `/usr/bin/omd` und `/usr/sbin/reboot` |
| Satelliten | SSH-Weiterleitung (`AllowTcpForwarding remote`) für den Repo-Tunnel |
| Firewall / VPN | Update-Server → Satelliten: SSH. Optional Satelliten → Update-Server: 123/udp (NTP), falls der Update-Server Zeitquelle ist |
| Checkmk-Master | Automationsbenutzer mit Recht auf Downtimes (REST-API) |
| Checkmk-Portal | Download-Zugang für kommerzielle Editionen, nur auf dem Update-Server |
| Update-Server → Internet | OS-Quellen, Checkmk-Downloadportal, Teams-Webhook (HTTPS) |
| Weboberfläche | eigene Benutzer (Passwort-Hash), standardmäßig nur lokal erreichbar (Zugriff per SSH-Tunnel), optional HTTPS |

> Wer Pakete installieren darf, hat faktisch root-Rechte. Der Zugang zum Update-Server und zur Weboberfläche muss entsprechend geschützt sein. Geheimnisse (Download-Zugang, Teams-Webhook-URL, API-Schlüssel) liegen nur im geschützten Secrets-Verzeichnis des Update-Servers.

**Uhrzeit:** Alle Systeme müssen eine korrekte Uhrzeit haben (sonst scheitern TLS und Signaturprüfung). Die Vorprüfung meldet Abweichungen über 30 s und überspringt Systeme mit mehr als 120 s Abweichung.

## 10. Umsetzung in Phasen

| Phase | Inhalt | Ergebnis |
|---|---|---|
| 1 · Testaufbau ✔ | Drei VMs: Update-Server, ein „Master“, ein „Satellit“ mit Remote Site. Abläufe komplett durchgespielt; Labormodus setzt Testsysteme auf alte Stände zurück. | OS-Update und Checkmk-Patch-Update laufen fehlerfrei und automatisch (Stand 0.4.4) |
| 2 · Pilot | Ein unkritischer Kunde. Zuerst nur OS-Updates, danach ein Checkmk-Patch-Update. | Erfahrungen, angepasstes Runbook |
| 3 · Rollout | Weitere Kunden aufnehmen, Quellen umstellen, Wartungsfenster vereinbaren. | Alle Satelliten über das Repo versorgt |
| 4 · Betrieb | Weboberfläche für Übersicht, Protokolle und manuelle Läufe (vorhanden), Teams-Benachrichtigung, Checkmk-Local-Check. | Wartbarer, nachvollziehbarer Betrieb |

## 11. Kundenabsprache

- Festes Wartungsfenster, z. B. Montag 09:00–12:00.
- Updates nur, wenn verfügbar, Neustart nur, wenn nötig.
- Während des Fensters ist das Monitoring des Kunden eingeschränkt, wenn der Satellit neu startet oder die Remote Site aktualisiert wird (Downtimes werden automatisch gesetzt und nach dem Update entfernt).
