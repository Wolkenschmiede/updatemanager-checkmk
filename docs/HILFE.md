# updatemanager-checkmk – Hilfe

Zentrale Update-Steuerung für Checkmk-Satelliten ohne Internetzugang.
Ein **Update-Server** im NOC hält ein eigenes Paket-Repository (Betriebssystem-Pakete + Checkmk)
und aktualisiert per SSH im Wartungsfenster:

1. das **Betriebssystem** aller Kunden-VMs, danach das des Masters,
2. die **Checkmk-Remote-Sites**, danach als Letztes den **Checkmk-Master**.

Auf den Zielsystemen läuft **kein eigenes Programm**: Der Update-Server erkennt per SSH
Betriebssystem, Version, offene Updates und Checkmk-Version und führt alles aus der Ferne aus.

Die Satelliten brauchen kein Internet. Während eines Updates öffnet der
Update-Server einen **SSH-Reverse-Tunnel**: Auf dem Satelliten ist das Repo dann
unter `127.0.0.1:3142` erreichbar. Außerhalb eines Laufs gibt es diese Verbindung nicht.

```
 Internet                  NOC                                  Kunde (ohne Internet)
 ─────────                 ───────────────────────────          ──────────────────────────
 OS-Spiegel           ──▶  Update-Server                        Kunden-VM (Ubuntu/Debian/
 (Ubuntu, Debian,          ├─ Repo je OS + nginx                 EL/SUSE)
  EL, SUSE)                ├─ updatemanager-checkmk  ═══SSH -R══▶ ├─ Remote Site kunde_a
 download.checkmk.com ──▶  │   (svc-update)                       │  apt/dnf/zypper → 127.0.0.1:3142
                           └─ REST-API ──▶ Checkmk-Master        └─ Checkmk-Agent
```

---

## Schnellstart

```bash
tar xzf updatemanager-checkmk-*.tar.gz && cd updatemanager-checkmk && ./install.sh
updatemanager-checkmk            # erster Start: Einrichtungsassistent
```

Reihenfolge beim ersten Mal:

1. **Assistent** durchlaufen. Er fragt Repo und Tunnel, SSH, Master, Checkmk, Wartungsfenster und Kunden ab
   und richtet danach optional den Update-Server ein (Pakete, nginx, GPG, NTP).
2. **Master vorbereiten:** siehe „Anleitung: Checkmk-Master vorbereiten“.
3. **Kunden einrichten:** siehe „Anleitung: Neuen Kunden einrichten (VM + Remote Site)“.
4. **Systeme erfassen** und **Repo synchronisieren:** Menü 6 → 2, danach 6 → 1.
   Dabei werden Betriebssysteme erkannt und pro OS-Version automatisch Spiegel angelegt.
5. **Vorprüfung:** Menü 7 → 1. Alle Systeme müssen „OK“ zeigen.
6. **Testlauf im Testmodus:** Hauptmenü `t`, danach Menü 7 → 4. Es wird nur angezeigt, was passieren würde.
7. **Zeitplan aktivieren:** Menü 4 → 2.

---

## Anleitung: Update-Server einrichten

Der Update-Server hält das Repo und steuert alle Updates. Er steht im NOC.

### Phase 1 – VM bereitstellen

1. **Ubuntu Server 24.04** installieren (ohne grafische Oberfläche).
   Richtwert: 2 vCPU, 4 GB RAM, 50 GB Platte – plus Platz für die Spiegel
   (je OS-Version etwa 2–5 GB gefiltert, 10–20 GB bei rpm-Systemen, über 100 GB bei kompletten apt-Spiegeln).
2. **Hostname**, feste IP, DNS setzen. Zeitzone und Zeit:
   ```bash
   sudo timedatectl set-timezone Europe/Berlin
   timedatectl            # "System clock synchronized: yes"
   ```
3. **Verbindungen**, die der Update-Server braucht:

   | Richtung | Ziel | Port | Zweck |
   |---|---|---|---|
   | ausgehend | OS-Spiegel (z. B. archive.ubuntu.com, deb.debian.org, dl.rockylinux.org) | 80/443 | Pakete laden |
   | ausgehend | download.checkmk.com | 443 | Checkmk-Pakete laden |
   | ausgehend | Checkmk-Master | 22, 80/443 | SSH (Updates), REST-API (Downtimes) |
   | ausgehend | alle Kunden-VMs (oder Jumphost) | 22 | SSH mit Tunnel |
   | eingehend | Kunden-VMs | 123/udp | nur falls der Update-Server Zeitquelle für die Kunden ist |

### Phase 2 – Programm installieren und einrichten

1. Paket kopieren und installieren:
   ```bash
   tar xzf updatemanager-checkmk-*.tar.gz && cd updatemanager-checkmk && ./install.sh
   updatemanager-checkmk
   ```
2. Der **Assistent** fragt Repo/Tunnel, SSH-Schlüssel, Master (inkl. Automation-Secret – siehe
   „Anleitung: Checkmk-Master vorbereiten“), Checkmk-Edition, Wartungsfenster und Kunden ab.
3. Am Ende **„Update-Server einrichten“** mit Ja beantworten (installiert aptly, nginx, GPG-Schlüssel,
   optional RPM-Werkzeuge und Zeitquelle). Später jederzeit über Menü **9 → 2** wiederholbar.
4. Prüfen: Menü **9 → 1** – alles sollte „OK“ sein, bis auf Punkte, die erst nach dem ersten Repo-Sync grün werden.
5. Wenn Master und Kunden eingerichtet sind: Menü **4 → 2** aktiviert den Zeitplan.

## Anleitung: Checkmk-Master vorbereiten

Einmalig, damit der Update-Server Downtimes setzen und den Master selbst aktualisieren kann.

### Phase 1 – Voraussetzungen prüfen

1. **Version und Edition** des Masters notieren (*Help → Info* oder auf dem Master `omd version`),
   z. B. `2.5.0p11.ultimate`. Alle Remote Sites müssen dieselbe Edition und denselben Zweig haben.
2. **Verbindungen** vom Master aus:

   | Richtung | Ziel | Port | Zweck |
   |---|---|---|---|
   | ausgehend | jede Remote Site | 6557/tcp | Livestatus (TLS) |
   | ausgehend | jede Remote Site | 80/tcp (oder 443) | Konfiguration verteilen |
   | eingehend | Update-Server | 22/tcp | SSH für OS-/Checkmk-Updates des Masters |
   | eingehend | Update-Server | 80/443 | REST-API (Downtimes) |

3. **Uhrzeit** synchron (`timedatectl`).

### Phase 2 – Automation-User für den Update-Server (Weboberfläche)

1. *Setup → Users → Users → Add user*
   - Username `svc-update`, Full name z. B. `Update-Manager`
   - Authentication: **Automation secret for machine accounts** → Würfel klicken → Secret kopieren
   - Roles: zum Testen **Administrator**. Für den Betrieb besser eine eigene Rolle (z. B. Kopie von
     *Normal monitoring user*) mit dem Recht, Downtimes auf allen Hosts zu setzen und zu entfernen.
2. **Save** → **Activate changes**.
3. Das Secret im Assistenten eingeben (oder später Menü **3 → 1**). Test: Menü **3 → 2** muss
   „REST-API erreichbar – Master läuft mit Checkmk …“ melden.

### Phase 3 – Master für Updates vorbereiten (updatemanager-checkmk)

1. Menü **3 → 4 → 2** (Automatisch per Admin-Login): Admin-Benutzer des Masters + Passwort.
   Richtet `svc-update` (SSH-Schlüssel, eingeschränktes sudo) und die Paketquellen über den Tunnel ein.
2. Menü **3 → 3** (Verbindung testen) – alles „OK“.

Hinweis: Danach holt sich auch der Master seine OS-Updates nur noch über das Repo des Update-Servers.
Die alten Paketquellen liegen unter `/root/updatemanager-backup-sources/`.

### Phase 4 – Master als Host überwachen

Nötig, damit beim OS-Update des Masters eine Downtime gesetzt werden kann.

1. Agent auf dem Master installieren:
   ```bash
   sudo apt install /omd/versions/default/share/check_mk/agents/check-mk-agent_*_all.deb      # Ubuntu/Debian
   sudo dnf install /omd/versions/default/share/check_mk/agents/check-mk-agent-*.noarch.rpm    # RHEL-Familie
   ```
2. *Setup → Hosts → Add host*: Hostname z. B. `checkmk-master`, IP des Masters (oder `127.0.0.1`),
   Monitored on site = die zentrale Site → **Save & run service discovery** → **Accept all** → **Activate changes**.
3. Den Hostnamen im updatemanager bei „Hostname des Masters IN Checkmk“ eintragen (Menü **3 → 1**).

### Phase 5 – Pro neuem Kunden im Master (Kurzfassung)

Die Details stehen in „Anleitung: Neuen Kunden einrichten“, Phase 6 und 7:

1. *Setup → General → Distributed monitoring → Add connection* (TCP, Port 6557, Encryption an,
   URL prefix `http://<IP>/<site>/`, Replikation `http://<IP>/<site>/check_mk/`).
2. **Login** mit dem cmkadmin-Passwort **der Remote Site**.
3. **CA der Remote Site vertrauen** (*Global settings → Trusted certificate authorities for SSL*) –
   nur das Zertifikat, nie den privaten Schlüssel.
4. **Activate changes**, dann den Host der VM mit *Monitored on site* = Remote Site anlegen.

Zeigt die Übersicht *Distributed monitoring* in einer weiteren Spalte (z. B. „Message broker“) einen Handlungsbedarf,
dem Hinweis der Oberfläche folgen. Für Status und Konfiguration der Remote Site sind die Schritte oben entscheidend.

## Anleitung: Neuen Kunden einrichten (VM + Remote Site)

Diese Anleitung führt von der leeren VM bis zum Kunden, der automatisch aktualisiert wird.
Platzhalter: `<IP>` = Adresse der Kunden-VM, `<site>` = Name der Remote Site (z. B. `kunde_a`),
`<version>`/`<edition>` = Checkmk-Version und Edition **des Masters** (siehe Status-Übersicht, z. B. `2.5.0p15` / `ultimate`).

### Phase 1 – VM beim Kunden bereitstellen

1. **Betriebssystem** installieren, das Checkmk unterstützt (Ubuntu, Debian, RHEL/Rocky/Alma/Oracle, SLES),
   als Server ohne grafische Oberfläche. Größe als Richtwert: 2 vCPU, 4 GB RAM, 40 GB Platte
   (mehr bei vielen Hosts).
2. **Hostname** setzen – nur Kleinbuchstaben, Ziffern und `-` (kein `_`):
   ```bash
   sudo hostnamectl set-hostname vm-kunde-a
   ```
3. **Netzwerk**: feste IP, DNS, Gateway. Internetzugang ist **nicht** nötig.
4. **Zeitzone und Zeitquelle** – sehr wichtig, sonst scheitern Updates und TLS:
   ```bash
   sudo timedatectl set-timezone Europe/Berlin
   timedatectl          # "System clock synchronized: yes" muss erscheinen
   ```
   Als Zeitquelle den Domain Controller des Kunden oder den Update-Server eintragen
   (das Vorbereitungs-Skript erledigt das, wenn auf dem Update-Server ein NTP-Server eingestellt ist).
5. **SSH-Server** installieren und aktivieren, **Admin-Benutzer mit sudo** anlegen:
   ```bash
   sudo apt install -y openssh-server      # Ubuntu/Debian
   sudo dnf install -y openssh-server      # RHEL-Familie
   sudo systemctl enable --now ssh || sudo systemctl enable --now sshd
   ```
6. **Firewall** – nur diese Verbindungen werden gebraucht:

   | Richtung | Port | Von → Nach | Zweck |
   |---|---|---|---|
   | eingehend | 22/tcp | Update-Server (bzw. Jumphost) → VM | Updates per SSH (Tunnel) |
   | eingehend | 6557/tcp | Checkmk-Master → VM | Livestatus (Status der Remote Site), TLS |
   | eingehend | 80/tcp (oder 443) | Checkmk-Master → VM | Konfigurations-Replikation |
   | ausgehend | – | – | nichts nötig (kein Internet) |

   ```bash
   sudo ufw allow 22/tcp && sudo ufw allow 6557/tcp && sudo ufw allow 80/tcp          # Ubuntu, falls ufw aktiv
   sudo firewall-cmd --permanent --add-port={22,6557,80}/tcp && sudo firewall-cmd --reload   # RHEL-Familie
   ```

### Phase 2 – Im updatemanager-checkmk anlegen und vorbereiten

1. Menü **2 → n**: Name, IP, Site-Name und Hostname der VM in Checkmk eintragen.
2. **Vorbereiten → Automatisch per Admin-Login**: Admin-Benutzer und Passwort der VM eingeben.
   Das richtet `svc-update`, sudo-Regeln, Paketquellen über den Tunnel, Zeitquelle usw. ein.
3. Menü **6 → 2** (Systeme erfassen) und **6 → 1** (Repo aktualisieren).
   Ein neues Betriebssystem bekommt dabei automatisch einen Spiegel, und die Checkmk-Version des Masters
   wird für dieses OS ins Repo geladen. Bei RHEL/SLES vorher die Repo-URLs in Menü **6 → 7** eintragen.
4. Menü **2 → Kunde → Verbindung testen** – alles muss „OK“ sein
   (der Hinweis „Site nicht gefunden“ ist an dieser Stelle normal).

### Phase 3 – Checkmk auf der VM installieren

Die VM hat kein Internet – das Paket kommt aus dem Repo des Update-Servers über den Tunnel.
**Auf dem Update-Server** ausführen (gleiche Version und Edition wie der Master!):

```bash
# Ubuntu/Debian
ssh -i ~/.ssh/svc-update -R 3142:127.0.0.1:80 svc-update@<IP> \
  "export DEBIAN_FRONTEND=noninteractive; sudo apt-get update && sudo apt-get install -y check-mk-<edition>-<version>"
# RHEL-Familie
ssh -i ~/.ssh/svc-update -R 3142:127.0.0.1:80 svc-update@<IP> "sudo dnf -y --refresh install check-mk-<edition>-<version>"
```

Alternative ohne Tunnel: das Paket aus `/srv/repo/checkmk/<profil>/` per `scp` auf die VM kopieren und dort
mit `sudo apt install ./<datei>.deb` bzw. `sudo dnf install ./<datei>.rpm` installieren.

### Phase 4 – Remote Site anlegen (auf der VM)

```bash
sudo omd create <site>
sudo omd config <site> set LIVESTATUS_TCP on      # TLS ist standardmäßig eingeschaltet
sudo omd start <site>
sudo omd config <site> show | grep LIVESTATUS     # TCP on, TLS on, Port 6557
```

Das bei `omd create` angezeigte **cmkadmin-Passwort notieren** – es wird einmalig für den Login aus
dem Master gebraucht. Vergessen? Neu setzen mit `sudo omd su <site>`, dann `cmk-passwd cmkadmin`, dann `exit`
(die Befehle einzeln eingeben).

### Phase 5 – Agent auf der VM installieren

Damit die VM selbst überwacht wird – das Paket liegt bereits in der Checkmk-Installation:

```bash
sudo apt install /omd/versions/default/share/check_mk/agents/check-mk-agent_*_all.deb      # Ubuntu/Debian
sudo dnf install /omd/versions/default/share/check_mk/agents/check-mk-agent-*.noarch.rpm    # RHEL-Familie
```

### Phase 6 – Remote Site im Master einbinden (Weboberfläche des Masters)

1. *Setup → General → Distributed monitoring → Add connection*

   | Feld | Wert |
   |---|---|
   | Site ID | `<site>` – exakt der Site-Name auf der VM |
   | Alias | z. B. `Kunde A` |
   | Connection | Connect via TCP (IPv4), Host `<IP>`, Port `6557`, Encryption an |
   | URL prefix | `http://<IP>/<site>/` |
   | Enable replication | Push configuration to this site |
   | URL of remote site | `http://<IP>/<site>/check_mk/` |
   | Disable configuration via Setup on this site | anhaken |

2. **Save** → in der Übersicht bei der Site auf **Login** → `cmkadmin` + Passwort **der Remote Site**.
3. **CA der Remote Site vertrauen** – sonst Fehler „unknown ca“ / „EOF occurred in violation of protocol“.
   Auf der VM **nur das Zertifikat** ausgeben (die Datei enthält auch den privaten Schlüssel – nie komplett kopieren!):
   ```bash
   sudo openssl x509 -in /omd/sites/<site>/etc/ssl/ca.pem
   ```
   Im Master: *Setup → Global settings* → Suche „Trusted certificate authorities“ → *Add new CA certificate*
   → einfügen → **Save**.
4. Oben **Activate changes → Activate on selected sites**.
5. Kontrolle: Status connection **Online**, Configuration connection **Online**.

### Phase 7 – VM als Host überwachen (Weboberfläche des Masters)

1. *Setup → Hosts → Add host*: Hostname wie in Phase 2 eingetragen, IP `<IP>`,
   **Monitored on site** = `<site>`.
2. **Save & run service discovery** → **Accept all** → **Activate changes**.
3. Nach 1–2 Minuten unter *Monitor → All hosts* sichtbar.

### Phase 8 – Abschluss im updatemanager-checkmk

1. Menü **2 → Kunde → Verbindung testen** – jetzt ohne Hinweise.
2. Menü **1** (Status): Betriebssystem, offene Updates und Checkmk-Version erscheinen.
3. Fertig – ab jetzt wird die VM im Wartungsfenster automatisch aktualisiert (OS und Checkmk).

### Kurz-Checkliste

| Nr | Schritt | Wo |
|---|---|---|
| 1 | OS, Hostname, feste IP, Zeitzone/NTP, SSH, Admin-User, Firewall | VM |
| 2 | Kunde anlegen + automatisch vorbereiten | updatemanager-checkmk, Menü 2 |
| 3 | Systeme erfassen + Repo aktualisieren | Menü 6 → 2, 6 → 1 |
| 4 | Checkmk-Paket installieren (Version/Edition des Masters) | Update-Server → VM |
| 5 | Site anlegen, LIVESTATUS_TCP on, starten, Passwort notieren | VM |
| 6 | Agent installieren | VM |
| 7 | Distributed monitoring: Verbindung, Login, CA vertrauen, Activate | Master |
| 8 | Host anlegen (Monitored on site), Discovery, Activate | Master |
| 9 | Verbindung testen, Status prüfen | Menü 2, Menü 1 |

---

## Was das Vorbereitungs-Skript erledigt (Master und Kunden)

Das Skript wird von updatemanager-checkmk erzeugt (`/opt/updatemanager-checkmk/vorbereitung/`). Es kann
automatisch per Admin-Login (scp + sudo) oder von Hand ausgeführt werden und lässt sich gefahrlos wiederholen:

| Schritt | Inhalt |
|---|---|
| Service-User | `svc-update`, nur Schlüssel-Login (`restrict,port-forwarding`), Passwort gesperrt |
| OS-Erkennung | erkennt Ubuntu/Debian, EL-Familie und SUSE selbst |
| sudo | nur Paketverwaltung (`apt-get`/`dnf`/`zypper`), `/usr/bin/omd`, `/usr/sbin/reboot` – Datei `/etc/sudoers.d/updatemanager-checkmk` |
| Repo-Schlüssel | apt: `/etc/apt/keyrings/updatemanager-repo.asc`, rpm: `/etc/pki/rpm-gpg/RPM-GPG-KEY-updatemanager-checkmk` |
| Paketquellen | apt: `/etc/apt/sources.list.d/updatemanager.sources`, dnf: `/etc/yum.repos.d/updatemanager-checkmk.repo`, zypper: `/etc/zypp/repos.d/updatemanager-*.repo` – alle zeigen auf `127.0.0.1:3142`. Alte Quellen werden nach `/root/updatemanager-backup-sources/` verschoben |
| Auto-Updates | `unattended-upgrades`, `apt-daily*`, `dnf-automatic`, `dnf-makecache` werden abgeschaltet – Updates kommen nur noch zentral |
| Zeit | Zeitzone, NTP-Server (timesyncd oder chrony, falls eingestellt) |
| SSH-Server | Warnung, falls `AllowTcpForwarding` den Tunnel verhindert |

**Wichtig – Zeit:** Satelliten ohne Internet brauchen eine **interne Zeitquelle**
(Domain Controller des Kunden oder den Update-Server per chrony). Ohne korrekte Uhr lehnt apt das Repo ab
(„Release file … is not valid yet“), und auch TLS und Checkmk schlagen fehl.

## Aufruf

| Befehl | Wirkung |
|---|---|
| `updatemanager-checkmk` | Menü (beim ersten Start der Assistent) |
| `updatemanager-checkmk status` | Status-Übersicht |
| `updatemanager-checkmk check` | Vorprüfung aller Systeme |
| `updatemanager-checkmk inventory` | Systeme erfassen: OS, Version, offene Updates, Checkmk, Paketlisten |
| `updatemanager-checkmk repo-sync` | Systeme erfassen, Checkmk-Versionen (Patch + Major) laden, alle OS-Spiegel synchronisieren |
| `updatemanager-checkmk update-os` | OS-Updates (Kunden, danach Master) |
| `updatemanager-checkmk update-checkmk` | Checkmk-Updates (Remote Sites, danach Master) |
| `updatemanager-checkmk maintenance` | Wartungslauf – läuft nur innerhalb des Wartungsfensters |
| `updatemanager-checkmk prepare NAME` | Vorbereitungs-Skript für Kunde/Master erzeugen |
| `updatemanager-checkmk edition-switch EDITION` | alle Sites auf eine höhere Edition umstellen (gleiche Version) |
| `updatemanager-checkmk notify-test` | Testnachricht an den Teams-Kanal |
| `updatemanager-checkmk checkmk-localcheck` | Status als Checkmk-Local-Check ausgeben (nutzt der Agent) |
| `updatemanager-checkmk checkmk-check` | beim Hersteller nach neuen Checkmk-Versionen suchen und laden |
| `updatemanager-checkmk checkmk-fetch 2.5.0p15` | bestimmte Checkmk-Version für alle Betriebssysteme ins Repo laden |
| `updatemanager-checkmk checkmk-list` | Checkmk-Pakete im Repo mit Nutzung („behalten“ / „entbehrlich“) |
| `updatemanager-checkmk checkmk-delete 2.5.0p12` | Checkmk-Paket löschen (`--profile ubuntu-noble`, `--edition community`) |
| `updatemanager-checkmk checkmk-cleanup` | alle entbehrlichen Checkmk-Pakete löschen |
| `updatemanager-checkmk repo-cleanup` | alte Repo-Stände der OS-Spiegel aufräumen |
| `updatemanager-checkmk repo-rollback ID STAND` | älteren Repo-Stand eines Betriebssystems veröffentlichen |
| `updatemanager-checkmk web on` / `off` / `status` | Weboberfläche ein-/ausschalten, Status |
| `updatemanager-checkmk web passwd [NAME]` | Benutzer der Weboberfläche anlegen / Passwort ändern |
| `updatemanager-checkmk web tls` | HTTPS-Zertifikat erzeugen und HTTPS einschalten |
| `updatemanager-checkmk web listen 0.0.0.0` | Weboberfläche im Netz erreichbar machen (`127.0.0.1` = nur lokal) |
| `updatemanager-checkmk lab on` / `off` | Labormodus (nur Testumgebung) |
| `updatemanager-checkmk lab-rollback …` | Testsysteme auf alten Stand zurücksetzen (siehe Kapitel Labormodus) |
| `updatemanager-checkmk setup` | Einrichtungsassistent erneut starten |

| Option | Wirkung |
|---|---|
| `--only kunde_a,kunde_b` | nur diese Systeme (der Master kann über seinen Namen gewählt werden) |
| `--no-master` | Master auslassen |
| `--dry-run` | Testmodus: nichts wird verändert, alle Schritte werden angezeigt |
| `--force` | `maintenance` auch außerhalb des Wartungsfensters |
| `-h`, `--help` | diese Hilfe |

Rückgabewerte: `0` OK · `1` Fehler bei mindestens einem System · `2` Aufruf/Einrichtung fehlt · `3` anderer Lauf aktiv.

---

## Was im Wartungsfenster passiert

**Repo-Sync und Wartungsfenster** dürfen sich überschneiden: Läuft der eine noch, wartet der andere, bis er
fertig ist (bis zu 3 Stunden), statt abzubrechen. Ist die letzte Suche nach Checkmk-Versionen älter als
6 Stunden, sucht das Wartungsfenster vor dem Checkmk-Teil selbst nach neuen Versionen. Empfohlen bleibt:
Repo-Sync einige Stunden vor dem Fenster (Standard 05:30, Fenster 09:00).

**So startet der Zeitplan:** Ein einziger Timer (`updatemanager-checkmk-zeitplan`) prüft jede Minute die
eingestellten Zeiten. Ist die Repo-Sync-Zeit erreicht, startet der Sync; hat das Wartungsfenster begonnen, startet
der Wartungslauf – jeweils genau einmal pro Termin. Geänderte Uhrzeiten (Menü 4 oder Weboberfläche → Zeitplan)
gelten **sofort**, ohne sudo. Zum Testen also einfach „Repo-Sync in 5 Minuten, Fenster in 15 Minuten“ eintragen.
Eine neue Uhrzeit, die heute schon vorbei ist, wird nicht nachgeholt. War der Update-Server zu Fensterbeginn aus,
startet der Lauf, sobald er innerhalb des Fensters wieder läuft. Einmalig einrichten: Menü **4 → 2**
(von Versionen vor 0.4.1 wird dabei automatisch umgestellt).

Zu Beginn des Fensters läuft `updatemanager-checkmk maintenance`:

**Gleichzeitigkeit:** OS- und Checkmk-Updates laufen auf allen Kunden gleichzeitig (Einstellung „Kunden gleichzeitig“, 0 = alle; bei vielen Kunden z. B. 10, um Leitung und Update-Server zu schonen). Der Master kommt immer zuletzt – beim Checkmk-Update nur, wenn alle Remote Sites erfolgreich waren.

**OS-Update** (Kunden gleichzeitig, danach der Master):

1. Vorprüfung: SSH, sudo-Rechte, Uhrzeit, Repo über den Tunnel, freier Platz.
2. Downtime für den Host im Master setzen (REST-API).
3. Paket-Update über den Tunnel, je nach System mit `apt-get dist-upgrade`, `dnf upgrade` oder
   `zypper update`. Ubuntu-„Phased Updates“ werden sofort mitgenommen.
4. Wenn das System einen Neustart verlangt (`reboot-required`, `needs-restarting`, `needs-rebooting`): Neustart. Danach wird gewartet, bis die **Boot-ID** sich geändert hat
   und die Checkmk-Site wieder läuft.
5. Downtime entfernen.

**Checkmk-Update** (Remote Sites nacheinander, danach der Master). Pro Lauf passiert genau **eine** Art:

1. **Nachholen:** Eine Remote Site steht einen Zweig hinter dem Master (z. B. nach einem früheren Fehler)
   → sie wird auf den Zweig des Masters gebracht.
2. **Patch-Update:** Jede Site, die nicht auf dem neuesten Patch **ihres** Zweigs ist, wird aktualisiert.
3. **Major-Update:** Erst wenn alle Sites auf dem neuesten Patch sind und `major_auto` an ist:
   Wechsel auf den **nächsten** Zweig (z. B. 2.5 → 2.6). Gibt es mehrere neuere Zweige, geht es pro Lauf
   immer nur einen Schritt weiter (2.5 → 2.6, im nächsten Fenster 2.6 → 2.7).

Ablauf pro Site: Vorprüfung → Edition prüfen → Downtime → Backup der Site (`omd backup`, Standard ohne
Messhistorie) → Paket aus dem Repo installieren → `omd stop` / `omd update` / `omd start` → Prüfung.
Bei Fehlern wird automatisch aus dem Backup zurückgerollt (abschaltbar), der Kunde wird als Fehler markiert.

Der **Master wird nur aktualisiert, wenn alle Remote Sites erfolgreich waren.** So schreibt es Checkmk vor:
erst die Remote Sites, zuletzt die zentrale Site. Remote Sites dürfen vorübergehend neuer sein als der Master,
der Master nie neuer als eine Remote Site.

**Regeln für Major-Updates** – alle müssen erfüllt sein, sonst wird nichts begonnen und der Grund
erscheint in Status und Log:

| Regel | Prüfung |
|---|---|
| Ein Schritt | nur der direkt nächste Zweig nach dem Master |
| Patchstand | alle Sites zuerst auf dem neuesten Patch ihres Zweigs; optional zusätzlich eine Mindest-Tabelle (`2.6.0=2.5.0p20`, Menü 6 → 8) |
| Betriebssystem | Checkmk muss die neue Version für das OS **jedes** Systems anbieten. Fehlt sie, meldet das Programm „OS-Upgrade nötig“ |
| Gleicher Stand | alle Sites stehen auf dem Zweig des Masters (oder schon auf dem neuen) |
| Edition | Edition jeder Site = eingestellte Edition (Ultimate bleibt Ultimate) |
| Wartungsfenster | genug Zeit bis zum Fensterende |

Das Update läuft mit `omd update --pre-flight=abort --skeleton=install --confirm-version` – **ohne** `-f`.
Damit bleiben die Sicherheitsprüfungen von Checkmk aktiv (Versionssperre, Editions-Prüfung, Vorabprüfung z. B.
auf inkompatible Erweiterungen). Bricht Checkmk vorab ab, bleibt die Site unverändert und wird wieder gestartet;
scheitert das Update mittendrin, greift der Rollback aus dem Backup. Das Zielverzeichnis wird exakt nach
Version **und** Edition gewählt, auch wenn mehrere Editionen derselben Version installiert sind.

**Wartungsfenster-Regel:** Wenn weniger als die eingestellten Minuten (Standard 20) bis zum
Fensterende übrig sind, wird kein neues System mehr begonnen. Laufende Updates werden nicht abgebrochen.

**Täglicher Repo-Sync** (Standard 05:30):
Er erfasst alle Systeme, sucht neue Checkmk-Patches **und** Major-Versionen (alle neueren Zweige),
lädt sie für jedes vorkommende Betriebssystem ins Repo (mit Herstellerprüfsumme, soweit vorhanden)
und spiegelt die OS-Pakete. Installiert wird dabei nichts.
Dabei entsteht jedes Mal ein datierter Repo-Stand (Snapshot), der für einen Rollback erhalten bleibt.

---

## Unterstützte Betriebssysteme

Unterstützt werden die Systeme, auf denen Checkmk selbst als Server bzw. Remote Site läuft.
Das Betriebssystem wird automatisch erkannt (`/etc/os-release`), für jede OS-Version entsteht ein **Profil**
mit eigenem Spiegel:

| Familie | Systeme | Spiegel auf dem Update-Server | Update-Befehl |
|---|---|---|---|
| apt | Ubuntu, Debian | aptly (gefiltert auf installierte Pakete oder komplett) | `apt-get dist-upgrade` |
| dnf | RHEL, Rocky, AlmaLinux, Oracle Linux, CentOS Stream | `dnf reposync` (neueste Pakete), Hardlink-Snapshots | `dnf upgrade` |
| zypper | SLES, openSUSE Leap | `dnf reposync` (rpm-md) | `zypper update` |

- Ubuntu, Debian, Rocky, Alma, Oracle, CentOS Stream und openSUSE Leap haben **öffentliche Standard-Spiegel**, die automatisch eingetragen werden.
- **RHEL** und **SLES** brauchen eine Subscription: Repo-URLs (bei SLES inkl. Token) unter
  Menü 6 → 7 eintragen.
- Für rpm-Systeme braucht der Update-Server `dnf` und `createrepo_c` (Menü 9 → 2 installiert sie).
- Profile, Spiegel-Adressen und einzelne Repos (z. B. `crb`, EPEL) lassen sich in Menü 6 → 7 anpassen.
- Ein OS-Release-Upgrade (z. B. Ubuntu 22.04 → 24.04, RHEL 8 → 9) macht das Programm **nicht**. Es meldet aber,
  wenn eine neue Checkmk-Version für ein System nicht mehr angeboten wird.

**Stand der Tests:** Ubuntu ist im Lab getestet. Debian, die EL-Familie und SUSE sind eingebaut, aber noch
nicht an echten Systemen geprüft.

## Editionen (Community, Pro, Ultimate …)

- Die Edition wird beim Einrichten aus der Version des Masters erkannt (z. B. `2.5.0p11.ultimate` → `ultimate`).
- Es werden **nur Pakete dieser Edition** gesucht, geladen und installiert (`check-mk-<edition>-<version>`).
- Vor jedem Update wird die Edition der Site geprüft. Passt sie nicht, wird diese Site **nicht** angefasst –
  ein versehentlicher Wechsel z. B. von Ultimate auf Community ist ausgeschlossen.
- Kommerzielle Editionen: Der Download braucht die Zugangsdaten aus dem Checkmk-Kundenportal
  (`user:passwort`, Menü 6 → 8). Major-Updates setzen eine gültige Subscription voraus.

**Edition wechseln** (Menü 7 → 6 oder `updatemanager-checkmk edition-switch ultimate`):

- Nur **nach oben** erlaubt: community → pro → ultimate (bzw. raw → enterprise → cloud).
  Ein Wechsel auf eine kleinere Edition wird blockiert – dabei gingen Funktionen und Daten verloren
  (Checkmk warnt selbst davor, siehe Werk 6559). MSP-Editionen nur manuell.
- Voraussetzungen: alle Sites auf **derselben Version**, gültige Lizenz, Download-Zugangsdaten.
- Ablauf: Pakete der neuen Edition (gleiche Version) für alle Betriebssysteme ins Repo laden → Remote Sites
  nacheinander umstellen (Backup, Downtime, `omd update`, Prüfung, Rollback bei Fehler) → Master zuletzt.
- Erst wenn alle Sites gewechselt haben, wird die Edition in der Konfiguration umgestellt. Bricht es mittendrin ab,
  den Wechsel einfach erneut starten – bereits umgestellte Sites werden übersprungen.

**Zurück auf eine ältere Version** (Downgrade) unterstützt Checkmk nur eingeschränkt. Der Weg zurück ist das
Backup der Site, das vor jedem Update angelegt wird (`~svc-update/updatemanager-backups/`, die letzten 3 je Site).

---

## Aufräumen: alte Checkmk-Versionen

Ältere Checkmk-Pakete werden nicht ewig aufgehoben – weder im Repo noch auf den Systemen.

**Im Repo (Update-Server).** Nach jedem Repo-Sync und nach jedem Checkmk-Update prüft das Programm jedes Paket:

| Paket ist … | bleibt / wird gelöscht |
|---|---|
| auf mindestens einer Site installiert | bleibt |
| die Version des Masters | bleibt (wird für neue Kunden gebraucht) |
| neuer als der Master (Update steht an) | bleibt |
| älter als die Version irgendeiner Site auf diesem Betriebssystem | bleibt (wird noch für ein Update gebraucht) |
| älter als der Master und **alle** Sites auf diesem OS sind schon neuer | **wird gelöscht** |
| andere Edition, von keiner Site genutzt und älter als der Master | **wird gelöscht** |
| andere Edition in Master-Version oder neuer | bleibt (für einen Editionswechsel) |

Kennt das Programm von einem System mit Site das Betriebssystem oder die Checkmk-Version nicht (noch nie
erfasst, nicht erreichbar), löscht es sicherheitshalber nichts. Ausschalten: Einstellungen → Checkmk
„Checkmk-Pakete automatisch aus dem Repo löschen“. Von Hand: Menü 6 → 9, `checkmk-delete`, `checkmk-cleanup`
oder in der Weboberfläche unter **Repository** (Mülleimer am Paket).

**Auf den Systemen.** Nach jedem erfolgreichen Checkmk-Update läuft `omd cleanup`: Es entfernt die
Checkmk-Versionen, die keine Site mehr nutzt. Ein Rollback ist davon nicht betroffen – der passiert sofort
beim fehlgeschlagenen Update, also vorher. Ausschalten: Einstellung „alte Checkmk-Versionen auf den Systemen
entfernen“.

**Alte OS-Stände.** Je Betriebssystem bleiben die letzten 8 Repo-Stände erhalten (Einstellung), ältere werden
beim Sync automatisch gelöscht.

---

## Benachrichtigungen (Teams und Checkmk)

Zwei Wege, beide optional und unabhängig voneinander (Menü **b**):

| Weg | Was man sieht |
|---|---|
| Teams-Kanal | nach jedem Wartungslauf/Update eine Karte mit Ergebnis je System; Fehler beim Repo-Abgleich; einmalig jede neue Checkmk-Major-Version im Repo |
| Checkmk (im Master) | Services *Updatemanager …* am Host des Update-Servers: Repo-Sync, Wartungslauf, Systeme, Checkmk-Versionen, Zeitplan – mit WARN/CRIT und damit auch den normalen Checkmk-Benachrichtigungen |

### Teams-Kanal einrichten

1. In Teams den Kanal öffnen → **…** (Weitere Optionen) → **Workflows**.
2. Vorlage **„Beim Empfang einer Webhookanforderung in einem Kanal posten"** wählen, Namen vergeben
   (z. B. *updatemanager-checkmk*), Team und Kanal bestätigen → **Workflow hinzufügen**.
3. Die angezeigte **URL kopieren** (sie ist geheim – wer sie hat, kann in den Kanal posten).
4. Auf dem Update-Server: Menü **b → 1**, URL einfügen (Eingabe unsichtbar). Sie wird unter
   `~/.config/updatemanager-checkmk/teams-webhook` gespeichert, nicht in der Konfiguration.
5. Festlegen: nach **jedem** Lauf melden oder **nur bei Fehlern/Hinweisen**; ob auch im Menü gestartete Läufe
   gemeldet werden sollen.
6. Testnachricht senden (b → 2 oder `updatemanager-checkmk notify-test`).

Ohne eigenes Team (keine Berechtigung, ein Team oder einen Kanal anzulegen): In der Workflows-App nach **Webhook**
suchen und die Chat-Vorlage nehmen (Name je nach Version, z. B. „Webhookwarnungen an einen Chat senden"). Dann:

- **Veröffentlichen als:** Flow-Bot, **Veröffentlichen in:** *Mit einem Flow-Bot chatten*, **Recipient:** die eigene
  Adresse. Die Karten kommen dann im Chat **„Workflows"** an.
- **Nicht** den Chat mit sich selbst wählen – das schlägt fehl mit *„Call made for a thread which is not a ChatThread"*.
- Wird das Ziel nachträglich geändert, leert Teams das Feld **Adaptive Karte** im Schritt *Post card in a chat or
  channel* (Zweig *Falsch* → *For each*). Neu eintragen **als Ausdruck** (fx bzw. `/` → *Ausdruck einfügen*):
  `items('For_each')?['content']` – im Feld muss ein Chip erscheinen, kein normaler Text. Dann speichern.
- Ob Nachrichten ankommen und warum sie scheitern: Workflows-App → Workflow → **Ausführungsverlauf**.

Der Update-Server braucht dafür HTTPS-Zugang zu der Adresse in der Webhook-URL (Microsoft Power Automate).

### Checkmk-Services einrichten (Update-Server als Host im Master)

1. **Agent auf dem Update-Server installieren.** Das Paket liegt auf dem Master unter
   *Setup → Agents → Windows, Linux, Solaris, AIX* (Download „Linux – DEB"). Datei auf den Update-Server kopieren und
   `sudo apt install ./check-mk-agent_*.deb` ausführen.
2. Den Agenten-Port **6556/TCP** vom Master (bzw. der zuständigen Site) zum Update-Server freigeben.
3. Im Master: *Setup → Hosts → Add host*, Name/IP des Update-Servers, Site wählen → **Save & run service discovery**.
4. Auf dem Update-Server: Menü **b → 4**. Das legt den Local Check
   `/usr/lib/check_mk_agent/local/updatemanager_checkmk` an.
5. Im Master beim Host erneut **Service discovery** → die Services *Updatemanager …* übernehmen →
   **Activate changes**.
6. Kontrolle: Menü **b → 5** zeigt dieselbe Ausgabe, die der Agent liefert.

| Service | OK | WARN | CRIT |
|---|---|---|---|
| Updatemanager Repo-Sync | letzter Abgleich OK, jünger als 48 h | älter als 48 h / noch nie gelaufen | fehlgeschlagen oder älter als 96 h |
| Updatemanager Wartungslauf | letzter Lauf OK | länger her als Zeitplan + 1 Tag / noch nie gelaufen | letzter Lauf mit Fehlern |
| Updatemanager Systeme | kein System mit Fehler | – | mindestens ein System mit Fehler beim letzten Lauf |
| Updatemanager Checkmk-Versionen | keine Blockade (zeigt neue Major-Versionen an) | Major-Update blockiert (Grund im Text) | – |
| Updatemanager Zeitplan | Info zum Wartungsfenster | – | – |

---

## Labormodus: Testsysteme auf alten Stand zurücksetzen

Nur für Testumgebungen. Damit lässt sich prüfen, ob Repo-Sync, Wartungsfenster und Benachrichtigungen
wirklich alles automatisch aktualisieren. Einschalten im Terminal (aus Sicherheitsgründen nicht in der
Oberfläche): im Menü **l (Labor) → 1 Labormodus einschalten**. Danach im selben Menü **2 Testsysteme
zurücksetzen** wählen – oder in der Weboberfläche die neue Seite **Labor** öffnen. Nach dem Test wieder
**l → 1** zum Ausschalten. (Alternativ per Befehl: `updatemanager-checkmk lab on` / `lab off`.)

**Was zurückgesetzt wird** (Systeme frei wählbar, Kunden zuerst, Master zuletzt):

| Teil | Ablauf |
|---|---|
| Checkmk | 1. Sicherung des aktuellen Stands · 2. gewählte ältere Version (gleicher Zweig) ins Repo laden und installieren · 3. liegt ein Backup dieser Version auf dem System, wird es zurückgesetzt – sonst Downgrade per `omd update` (von Checkmk nicht offiziell unterstützt) · 4. Version als Standard setzen, neuere entfernen. Misslingt Schritt 3, wird die Sicherung aus 1. zurückgespielt. |
| OS | harmlose Pakete (nano, curl, wget, less, rsync, jq, tzdata, vim-tiny) auf die Release-Version der Distribution. Der Update-Server lädt sie aus dem Internet und kopiert sie per scp – geht nur, wenn er dasselbe Betriebssystem hat wie das Zielsystem. Danach zeigen die Systeme echte offene Updates. |
| Repo | optional alle Checkmk-Pakete löschen – dann muss der Repo-Sync die neue Version selbst finden. |

**Testablauf:** zurücksetzen → Zeitplan vorziehen (Repo-Sync z. B. in 15 Min., Wartungsfenster 30 Min.
später, 2 Std. lang) → zuschauen: Übersicht zeigt offene Updates und Zielversion, das Wartungsfenster spielt
alles ein, Teams meldet das Ergebnis → Zeitplan zurückstellen, `lab off`.

Kommandozeile: `lab-versions` zeigt die älteren Versionen,
`lab-rollback --checkmk 2.5.0p12 --os --clear-repo [--only kunde_a,zentrale] [--dry-run]` setzt zurück.

---

## Weboberfläche

Alles, was das Menü kann, gibt es auch im Browser: Übersicht mit dem Master oben und den Kunden darunter
(Version von VM und Remote Site, offene Updates und Zielversion), Verbindungen mit Details, Repository mit
allen geladenen Checkmk-Paketen und OS-Ständen, Läufe mit Live-Ausgabe, Zeitplan, Einstellungen und diese Hilfe.
Sie ist optional – das Terminal-Menü und der Zeitplan funktionieren unabhängig davon.

### Einschalten

```
updatemanager-checkmk web on
```

Beim ersten Mal wird ein Benutzer mit Passwort (mind. 10 Zeichen) angelegt. Die Oberfläche läuft als Dienst
`updatemanager-checkmk-web` unter demselben Benutzer wie das Programm. Ausschalten: `web off`.
Alternativ im Menü unter **w**.

### Öffnen

Standardmäßig lauscht sie **nur auf dem Update-Server selbst** (127.0.0.1, Port 8470). Vom Arbeitsplatz aus
per SSH-Tunnel öffnen:

```
ssh -L 8470:127.0.0.1:8470 benutzer@update-server
```

Dann im Browser `http://localhost:8470/` aufrufen.

**Im NOC-Netz freigeben** (ohne Tunnel):

```
updatemanager-checkmk web tls               # Zertifikat erzeugen, HTTPS an
updatemanager-checkmk web listen 0.0.0.0    # aus dem Netz erreichbar
```

Danach `https://update-server:8470/`. Das Zertifikat ist selbst signiert (der Browser warnt beim ersten
Aufruf); ein eigenes Zertifikat einfach an die angezeigten Pfade kopieren und `web restart`. Port 8470 in der
Firewall des Update-Servers nur für das NOC-Netz öffnen.

### Benutzer und Sicherheit

- Weitere Benutzer: `web passwd NAME`, entfernen: `web deluser NAME`. Passwort ändern geht auch in der Oberfläche.
- Passwörter liegen nur als Hash (PBKDF2) in `~/.config/updatemanager-checkmk/web-users.json`.
- Nach 6 Fehlversuchen ist die Anmeldung von dieser Adresse 10 Minuten gesperrt; Sitzungen laufen nach 12 Stunden ab.
- Adresse, Port und HTTPS lassen sich absichtlich nur im Terminal ändern.
- Secrets (Automation-Secret, Download-Zugang, Teams-Webhook) können in der Oberfläche gesetzt, aber nie angezeigt werden.

### So arbeitet sie

- Jede Aktion (Update, Repo-Sync, Löschen …) startet im Hintergrund genau den Befehl, den man auch im Terminal
  eingeben würde – mit Log unter `logs/` und denselben Sperren. Die Ausgabe erscheint live unter **Läufe & Logs**;
  der Lauf geht weiter, auch wenn der Browser geschlossen wird.
- Jeder Lauf hat ein Zeitlimit (Repo-Sync 8 Std., Updates/Wartungslauf 12 Std., Prüfen 30 Min.) und lässt
  sich unter **Läufe & Logs** mit **Abbrechen** beenden. Ein Update mittendrin abzubrechen kann ein System halb
  aktualisiert zurücklassen – nur tun, wenn der Lauf wirklich hängt. Ein einzelner Spiegel-Abgleich bricht nach
  4 Stunden ab (Einstellung `repo.sync_timeout_min`). Ein Neustart des Dienstes
  (`sudo systemctl restart updatemanager-checkmk-web`) beendet ebenfalls einen laufenden Vorgang.
- Während ein Lauf das Repository sperrt, zeigt die Seite **Repository** den Stand vom letzten Abgleich.
- Es läuft immer nur ein Vorgang gleichzeitig. Läuft gerade einer aus dem Zeitplan oder im Terminal, zeigt die
  Oberfläche das oben an und startet nichts Neues.
- Der Schalter **Testmodus** oben rechts gilt für alle Aktionen: Updates, Downloads und Löschungen werden dann
  nur angezeigt.
- Die Übersicht zeigt den Stand der letzten Erfassung (täglich mit dem Repo-Sync, nach jedem Lauf).
  **Jetzt erfassen** holt den aktuellen Zustand per SSH.
- Was Root-Rechte mit Passwort braucht (Einrichtung, Timer, Checkmk-Agent), bleibt im Terminal; die Oberfläche
  zeigt an der Stelle den Befehl. Geänderte Zeiten im Zeitplan gelten sofort.

---

## Menü im Überblick

| Menü | Inhalt |
|---|---|
| 1 Status | pro System: Betriebssystem, offene OS-Updates, Checkmk-Version und was der nächste Lauf tut; blockierte Major-Updates mit Grund; Repo-Stände je OS |
| 2 Kunden | anlegen, bearbeiten, (de)aktivieren, vorbereiten, testen, einzeln aktualisieren, löschen |
| 3 Master | bearbeiten, REST-API testen, vorbereiten, testen |
| 4 Zeitplan | Wartungsfenster (Tage, Uhrzeit, Zeitzone), Parallelität, Downtime-Dauer, Timer an/aus |
| 5 SSH & Tunnel | Service-User, Schlüssel, Jumphost, Tunnel- und Repo-Port |
| 6 Repository | synchronisieren, Systeme erfassen, Repo-Stände/Rollback je OS, Checkmk-Versionen, Betriebssysteme/Spiegel, Einstellungen, 9 Checkmk-Pakete anzeigen/löschen/bereinigen |
| 7 Updates | Vorprüfung, OS-/Checkmk-Update aller Systeme, kompletter Lauf, Edition wechseln |
| 8 Logs | Protokolle der Läufe |
| 9 System | Update-Server prüfen/einrichten, Assistent |
| b | Benachrichtigungen: Teams-Kanal, Checkmk-Services |
| w | Weboberfläche: ein/aus, Benutzer, Adresse, HTTPS |
| l | Labor: Labormodus ein/aus, Testsysteme auf alten Stand zurücksetzen (nur Testumgebung) |
| t | Testmodus (Dry-Run) ein/aus |
| h | Hilfe nach Kapiteln |

---

## Dateien und Pfade

```
/opt/updatemanager-checkmk/            Programm (gehört ins Git)
├── updatemanager-checkmk              Startpunkt  (Link: /usr/local/bin/updatemanager-checkmk)
├── umc/                        Programmcode (Python 3, nur Standardbibliothek)
├── umc/web/                    Weboberfläche (HTML, CSS, JavaScript, Schrift IBM Plex – OFL)
├── docs/HILFE.md               diese Hilfe
├── install.sh                  Installation / Aktualisierung
├── conf/config.json            Einstellungen (Master, Kunden, Zeitplan ...)   – nicht im Git
├── conf/aptly.conf, nginx-repo.conf   erzeugt                                   – nicht im Git
├── state/                      Zustand, Paketlisten, Sperrdatei, Läufe der Weboberfläche – nicht im Git
├── logs/                       Protokolle aller Läufe                           – nicht im Git
└── vorbereitung/               erzeugte Vorbereitungs-Skripte                   – nicht im Git

/srv/repo/aptly/                aptly-Datenbank, Paket-Pool, veröffentlichtes Repo (public/)
/srv/repo/aptly/public/rpm/     rpm-Spiegel je Profil (current -> Snapshot) und Checkmk-rpm-Repos
/srv/repo/rpm-mirror/           Arbeitsverzeichnis der rpm-Spiegel
/srv/repo/checkmk/<profil>/     heruntergeladene Checkmk-Pakete je Betriebssystem
~/.ssh/svc-update               SSH-Schlüssel des Update-Servers
~/.config/updatemanager-checkmk/       Secrets (Automation-Secret, Download-Zugang, Teams, Web-Benutzer, Zertifikat)
/etc/systemd/system/updatemanager-checkmk-*.{service,timer}   Zeitplan
/etc/systemd/system/updatemanager-checkmk-web.service         Weboberfläche (optional)
```

---

## Umzug von der Testumgebung auf das Produktivsystem

1. Programm übertragen: Ordner kopieren oder `git clone`, danach `./install.sh`.
2. `updatemanager-checkmk` starten → der Assistent richtet das neue System ein.
   Optional `conf/config.json` aus der Testumgebung mitnehmen und anpassen (Adressen, Kunden).
3. **Neu erzeugt** werden: SSH-Schlüssel, GPG-Repo-Schlüssel, Automation-Secret (eigener User im Produktiv-Master).
4. Repo-Daten werden **nicht** kopiert: Menü 6 → 2 → 1 lädt sie neu.
5. Alle Systeme mit dem neuen Vorbereitungs-Skript vorbereiten (neue Schlüssel!).
6. Vorprüfung, Testlauf im Testmodus, dann den Zeitplan aktivieren.

---

## Entfernen und komplett zurücksetzen

Im Ordner `tools/` liegen zwei Skripte, die alles rückgängig machen – etwa um eine Testumgebung neu aufzusetzen
oder das Programm zu deinstallieren. Checkmk und die Sites selbst bleiben unverändert.

1. **Auf jedem vorbereiteten System** (Kunden-VMs und Master):
   ```bash
   sudo bash entfernen-system.sh
   ```
   Holt die ursprünglichen Paketquellen zurück, entfernt `svc-update` (samt Home und Site-Backups darin),
   die sudo-Regel und den Repo-Schlüssel, schaltet automatische Updates wieder ein und setzt die Zeitquelle zurück.
2. **Im Checkmk-Master** (Weboberfläche): den Automation-User `svc-update` löschen, bei Bedarf auch den Host des
   Masters aus Phase 4 → **Activate changes**.
3. **Auf dem Update-Server** (als normaler Benutzer):
   ```bash
   bash /opt/updatemanager-checkmk/tools/entfernen-update-server.sh
   ```
   Löscht Programm, Einstellungen, Zustand, Logs, Geheimnisse, SSH- und Repo-Schlüssel, Repo-Daten, Zeitplan,
   Weboberfläche und nginx-Seite. Bietet an, die Teams-Webhook-Adresse vorher zu sichern und aptly/nginx zu deinstallieren.

Danach neu installieren wie im Schnellstart beschrieben.

---

## Sicherheit

- Der Update-Server ist ein **hochprivilegiertes System**: Er kann auf allen Satelliten Pakete installieren
  und Checkmk steuern. Zugang nur für das NOC, Updates und Backups wie bei einem Produktivserver.
- `svc-update` hat nur sudo auf `apt-get`, `omd` und `reboot` und meldet sich nur per Schlüssel an.
  Hinweis: `omd` und `apt-get` sind trotzdem mächtig. Der Schutz liegt vor allem am Schlüssel.
- Die Satelliten öffnen **keine** Verbindung nach außen. Der Tunnel besteht nur während eines Laufs.
- Das Repo ist mit einem eigenen GPG-Schlüssel signiert, Checkmk-Pakete werden per Herstellerprüfsumme geprüft.
- Secrets liegen nur in `~/.config/updatemanager-checkmk/` (Rechte 600), nie in `conf/` oder im Git.

---

## Fehlerbehebung

| Meldung | Ursache / Lösung |
|---|---|
| `Release file … is not valid yet` | Uhr auf dem Satelliten falsch → NTP einrichten, Vorprüfung zeigt die Abweichung |
| `Repo über Tunnel nicht erreichbar` | nginx läuft nicht / Repo noch nicht veröffentlicht (Menü 9 → Prüfen) · Port 3142 auf dem Satelliten belegt (alter Tunnel) · `AllowTcpForwarding no` |
| `sudo-Rechte … fehlen` | Vorbereitungs-Skript (erneut) ausführen |
| `SSH nicht erreichbar` | Adresse/Port/Jumphost prüfen · Schlüssel nicht hinterlegt → vorbereiten |
| `md5 hash mismatch` beim Repo-Sync | Ubuntu-Server synchronisiert gerade → wird automatisch wiederholt (Menü 6 → 7: Wiederholungen) |
| `deferred due to phasing` | betrifft nur manuelles apt – updatemanager-checkmk nimmt Phased Updates mit |
| Livestatus `unknown ca` / `EOF occurred` | CA der Remote Site im Master hinterlegen (siehe oben) |
| REST-API `HTTP 401` | Automation-User/Secret falsch → Menü 3 → Bearbeiten |
| Checkmk-Update fehlgeschlagen | automatischer Rollback aus `~svc-update/updatemanager-backups/` · Log in Menü 8 · `omd update` von Hand prüfen |
| `Es läuft bereits ein anderer … Vorgang` | ein Lauf ist aktiv (Timer?) · sonst `state/run.lock` prüfen |
| „Major … blockiert“ | Grund steht dabei: OS wird von der neuen Version nicht unterstützt (OS-Upgrade nötig), Mindest-Patchstand nicht erreicht, Site auf anderem Zweig, falsche Edition |
| „Kein aktives Repo-Profil“ | System neu erfasst, aber Spiegel noch nicht synchronisiert (Menü 6 → 1) oder Profil deaktiviert/ohne URLs (Menü 6 → 7) |
| rpm-Spiegel schlägt fehl | `dnf`/`createrepo_c` installiert (Menü 9 → 2)? Repo-URL erreichbar? Bei RHEL/SLES Zugangsdaten/Token |

Protokolle: `/opt/updatemanager-checkmk/logs/` (Menü 8) · Timer: `systemctl list-timers 'updatemanager-checkmk-*'`
· Ausgabe des Zeitplans: `journalctl -u updatemanager-checkmk-zeitplan`

---

## Grenzen (Stand 0.4.4)

- **OS-Release-Upgrades** (z. B. Ubuntu 22.04 → 24.04) bleiben manuell.
- Debian, EL-Familie und SUSE sind eingebaut, aber noch nicht an echten Systemen getestet.
- Kommerzielle Editionen: Download-Zugang (`user:passwort`) wird benötigt, die Download-Adressen sind noch nicht
  mit echten Zugangsdaten geprüft.
- Benachrichtigung per E-Mail ist nicht eingebaut (Teams und Checkmk-Services schon, siehe Kapitel „Benachrichtigungen“).
- Ein Jumphost wird über `-J` genutzt. Dessen Anmeldung muss über `~/.ssh/config` des Update-Servers funktionieren.
