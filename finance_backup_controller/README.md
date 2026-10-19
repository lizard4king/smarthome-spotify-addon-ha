# Finance Backup Controller — kanonisches Prüf- und Veröffentlichungspaket

Dieses eigenständige HA-App-Paket enthält genau **eine** `controller.py`. Beide Testdateien prüfen diese Fassung direkt. Die älteren Root-/`addon/`-Fassungen außerhalb dieses Ordners sind Prüfprototypen und gehören nicht zum späteren Veröffentlichungspaket. Es wurde nichts gebaut, installiert, aktiviert oder mit echten Daten ausgeführt.

Der prüfbare Ablauf ist in [flow.svg](flow.svg) und [flow.png](flow.png) dargestellt.

## Rechte und Vorgaben

Das konkrete Manifest `config.json` verlangt `homeassistant_api: true` sowie `hassio_api: true` mit `hassio_role: default`. Core-API-Zugriff ist breit und nicht auf Backup-Befehle beschränkt; die Supervisor-Informationsrolle liefert ausschließlich aktuelle Finance-Version und internen Hostnamen. Fremde Optionen werden durch diese Rolle redigiert und deshalb nicht daraus beurteilt. Beide Zugriffe benötigen vor Installation eine ausdrückliche Freigabe des Eigentümers. Keine Managerrolle, keine Umgehung über den Core-Supervisorproxy, keine Erweiterung der vorhandenen SSH-App und keine fremden Tokens.

Es gibt keine Ports, Hostmounts, Geräte oder erweiterten Containerrechte. Dauerhafte Daten liegen ausschließlich im automatisch bereitgestellten privaten `/data`. `boot: manual`, `enabled: false`, `inspect_only: false`, `initialize_ui: false` und `apply_retention: false` sind die Vorgaben. Kein Watchdog und kein S6-Neustartdienst: Ein unsicherer Lauf muss beendet bleiben, bis seine Ursache untersucht wurde.

Offizielle Basisbilder: `ghcr.io/home-assistant/{arch}-base:3.24-2026.08.0` für amd64/aarch64. Python und WebSocket-Client werden ausschließlich über Alpine `apk add --no-cache python3 py3-websocket-client` bezogen. Kein pip, kein Laufzeit-Codeabruf. Die Bilder sind über Tags festgelegt; ein Containerbuild, die Supervisor-Manifestannahme und die Architekturprüfung stehen noch aus.

## Sicherheitsprüfung und Inhalt

Vor jeder Sicherung wird über die feste interne GET-Route `http://supervisor/addons/{addon_slug}/info` ausschließlich Version **0.2.69** und der exakt erwartete Hostname (`addon_slug.replace('_', '-')`) geprüft. Anschließend folgt ein **tokenfreier** GET an genau diesen belegten Hostnamen auf Port 8785, Route `/health/backup-guard`. Nur die exakte Antwort `{"schema_version":1,"backup_safe":true,"addon_version":"0.2.69"}` ist zulässig. Die Supervisor-Bearer-Anmeldung wird niemals an Finance weitergegeben. Finance liest dafür die tatsächliche Optionsdatei bei jeder Anfrage neu und bestätigt nur ein ausdrücklich vorhandenes leeres `access_token`; fehlende, abweichende oder unlesbare Werte ergeben lediglich einen negativen Status ohne Details. Die Version stammt aus dem im Finance-Bild eingebundenen Buildmanifest. Der noch ungemergte Release 0.2.69 erhält diese Ergänzung; die Versionsnummer wird dafür nicht angehoben.

Ein anderer oder unvollständiger Stand stoppt vor Backup-Mutationen. Der Slug besteht aus einem streng geprüften Pfadsegment; keine Redirects oder Umgebungs-Proxys. Antwortgröße je Abfrage höchstens 256 KiB, Socketwartezeit höchstens 20 Sekunden. Optionen, Token und rohe Fehlertexte werden niemals ausgegeben. Die Vorprüfung wird vor jedem eigenen Löschaufruf erneut durchgeführt. Das vom Info-Endpunkt nicht gelieferte `backup_exclude` wird nicht angenommen, sondern muss für die separat freigegebene Finance-Version belegt sein.

Die Core-WebSocket-Version muss `2026.9.4` oder `2026.10.2` sein. Die Quelle der Sicherung ist genau das konfigurierte Finance-Add-on, mit `include_all_addons`, `include_homeassistant` und `include_database` jeweils `false` und einer leeren Ordnerliste. Ziele sind ausschließlich die beiden expliziten und unterschiedlichen lokalen/Drive-Agent-IDs; beide müssen verfügbar sein und Verschlüsselung verlangen.

`SUPERVISOR_TOKEN` wird automatisch von HA bereitgestellt und ausschließlich im RAM verwendet. Der vorhandene HA-Backup-Schlüssel wird über `backup/config/info` ebenfalls nur im RAM gelesen und ausdrücklich an `backup/generate` übergeben. Ein fehlender Schlüssel stoppt den Lauf. Das Weglassen von `password` übernimmt diesen Schlüssel nicht automatisch und kann unverschlüsselte Uploads zulassen. Keine Schlüssel gehören in App-Optionen, Dateien, Logs oder Drive. Die Einrichtung des Schlüssels und sichere Wiederherstellungsunterlagen sind ein separater Betreiber-Schritt; dabei darf kein nativer Plan mit Core-Daten nach Drive aktiviert werden.

Ein vollständig beobachtetes Abschlussereignis genügt noch nicht: Der Controller verlangt eine neue eindeutige Backup-ID zum zufälligen eigenen Namen sowie passende Detailmetadaten, beide verschlüsselte Kopien und leere Fehlerlisten. Die native Erzeugung liefert zunächst eine Job-ID, keine Backup-ID. Eine Unterbrechung hinterlässt einen blockierenden Zustand; keine automatische Wiederholung oder Verbindungserneuerung. WebSocket-Verbindungs-/Lesefrist höchstens 30 Sekunden, Gesamtfrist 30 Minuten.

## Sieben Tage und 84 Tage

Die Root-CLI `controller.py` enthält keinen Zeitgeber. Der App-Prozess `scheduler.py` startet bei `enabled: true` einen fälligen Einmallauf und plant anschließend sieben Tage nach der letzten dauerhaft gespeicherten erfolgreichen Sicherung in UTC. Er schläft höchstens eine Stunde je Phase und liest Optionen/Zustand danach erneut. Ein Neustart erzeugt keine Nachholserie. Mit `enabled: false` beendet sich die App ohne Verbindung und ohne eigene Schreibzugriffe.

Der gesonderte Diagnosemodus `enabled: true` plus `inspect_only: true` authentisiert genau einmal an Core und liest ausschließlich `backup/agents/info` sowie `backup/config/info`, danach beendet er sich. Die Ausgabe enthält nur `agent_ids`, `ha_version`, `key_configured`, `native_pruning_configured` und die booleschen oder unbekannten `agent_protected`-Flags. Keine Namen, E-Mails, Schlüssel oder rohen Konfigurationen. Er verwendet weder die synthetischen Quell-/Ziel-IDs noch Zustandsdateien und schreibt keine Controller-Konfiguration. Mit `enabled: false` bleibt auch dieser Modus vollständig offline. Er darf zur Ermittlung der echten IDs dienen, die später ausschließlich über die Servermaske gesetzt werden. Ein positiver Diagnosebericht ersetzt keinen der zusätzlichen Guards des normalen Sicherungslaufs.

Die **84 Tage gelten ausschließlich für den eigenen Controller-Plan**: Nur mit `apply_retention: true` werden nach einer neuen vollständig geprüften Sicherung eigene IDs älter als 84 Tage berücksichtigt. Die neueste erfolgreiche Sicherung bleibt erhalten. Es gibt keinen nativen 84-Tage-Timer.

Alle nativen globalen und agentbezogenen `copies`- und `days`-Limits müssen `null` sein, einschließlich fremder Agents. Jeder andere Wert stoppt den Controller. Native Kopienlimits können bei Erzeugung fremde Sicherungen entfernen; HA besitzt zusätzlich einen unabhängigen Tagestimer, der auch ohne aktiven nativen Sicherungsplan fremde automatische Sicherungen löschen kann. Der Controller ändert diese Einstellungen nicht selbst. Native Limits auszuschalten bedeutet nicht unbegrenzte Controller-Aufbewahrung: Die eigene 84-Tage-Prüfung bleibt davon getrennt.

Die Zustandsdatei speichert ausschließlich Konfiguration, eigene erfolgreiche IDs, Namen, Datum und offene Erzeugungs-/Löschabsichten. Nur `null` bedeutet freie Intentfelder; auch `{}`, `0`, `false` und `""` blockieren. Eine zufällige Tempdatei wird exklusiv und bereits bei Erstellung mit Modus `0600` geöffnet; Flush, `fsync` und atomarer Austausch schützen die Zustandsübergänge. Unter Linux wird zusätzlich das Verzeichnis synchronisiert. Vorhandene Tempdateien/Symlinks werden nicht wiederverwendet. Die konkrete Löschabsicht wird vor jedem Löschaufruf dauerhaft geschrieben; Teilfehler bleiben zur manuellen Prüfung erhalten. Lock-/State-Dateien dürfen bei Unsicherheit nicht einfach gelöscht oder durch einen leeren Bestand ersetzt werden.

## Native Oberfläche einmalig freischalten

`enabled: true`, `initialize_ui: true` und `inspect_only: false` führen ausschließlich eine einmalige UI-Initialisierung aus. Die synthetischen Quell-/Ziel-IDs werden in diesem Modus nicht verwendet. Voraussetzung sind ein bereits vorhandener HA-Backup-Schlüssel, der native Zeitplan `never` und ausschließlich `null` für sämtliche globalen und agentbezogenen `copies`-/`days`-Grenzen. Der Schlüssel bleibt im RAM. Ohne diese Voraussetzungen endet der Lauf vor einem Schreibaufruf.

Nach genau einer Core-Anmeldung und `backup/config/info` wird höchstens ein `backup/config/update` mit ausschließlich `automatic_backups_configured: true` gesendet. Ist das Flag bereits gesetzt, entfällt der Schreibaufruf. Anschließend wird die gesamte Konfiguration erneut gelesen und einschließlich unbekannter Felder und Schlüssel typgetreu mit dem vorherigen Stand verglichen; einzig das Flag darf auf `true` wechseln. Es gibt keine Backup-Erzeugung, Löschung, Zustandsdatei oder Wiederholung. Nur nach erfolgreicher Prüfung und geschlossenem Socket erscheint `Native backup UI initialized; no backup created`. Bei Fehlern oder Abweichungen bleibt das Ergebnis unbestimmt; ein gesendetes Flag wird nicht automatisch zurückgesetzt. Den Lauf dann untersuchen, nicht automatisch erneut starten.

Danach ändert der Eigentümer den Schlüssel selbst direkt in der nativen Backupübersicht und sichert das neue Notfallpaket außerhalb von HA. Diese Schlüsseländerung benötigt keine Backuperstellung. Die Initialisierung ändert selbst weder Schlüssel noch Ziele, Zeitplan oder Aufbewahrung; sie schaltet nur den regulären UI-Zugang frei. Anschließend `initialize_ui` wieder auf `false` setzen; das ist keine Freigabe für den normalen Sicherungslauf. `initialize_ui` und `inspect_only` gleichzeitig sind ungültig. `enabled: false` bleibt auch mit gesetztem Initialisierungsflag offline.

## Lokale Prüfung ohne Netzwerk

Im Ordner dieses README mit Python 3.10+:

```text
python -m unittest -v
python controller.py --config config.example.json --state synthetic-state.json --dry-run
```

Der erste Befehl führt alle **45 synthetischen Tests** gegen die eine kanonische Controllerfassung aus; die bestehenden 39 Tests bleiben erhalten. Keine Verbindung, keine echten Credentials, kein Containerbuild. Der zweite zeigt ausschließlich den Offline-Plan und erzeugt keine Zustandsdatei. Die Beispielkonfiguration enthält synthetische IDs. Ein realer CLI-Lauf benötigt zusätzlich `--run-once`; Löschungen außerdem `--apply-retention`. Diese Schalter erst nach gesonderter Betriebsfreigabe verwenden.

## Grenzen und Review-Differenz

Die Tests bestätigen den lokalen Ablauf, nicht reale Core-/Supervisor-Authentisierung, Drive-Upload, Wiederherstellung, Finanzdatenkonsistenz oder Add-on-interne Geheimnisausschlüsse. Vor Installation müssen die konkrete Finance-Version, deren Archivinhalt, IDs, vorhandener Schlüssel, serverseitige Logredaktion und Wiederherstellung separat geprüft werden. Der Controller bleibt beim Cold Backup des Finance-Add-ons als eigener Prozess aktiv; das reale Verhalten ist noch zu verifizieren.

Die HA-Lösch-API löscht eine ID auf allen Agents. Deshalb müssen die aktuellen Metadaten exakt die beiden erwarteten Kopien, eigene ID, Name, Zeit und Inhalte bestätigen. Prüfung und Löschung bilden keinen atomaren API-Aufruf; parallele externe Änderungen bleiben eine Grenze. Die zusammengeführte HA-Metadatenantwort ist außerdem keine kryptografische Inhaltsprüfung jeder Kopie. Ein Fehler beendet den App-Prozess; externe automatische Neustarts dürfen diese Abbruchschranke nicht umgehen.

Gegenüber dem zuletzt abgeschlossenen Claude-Review kamen der native `days`-Guard, die kanonische Paketstruktur und der zweistufige aktuelle Finance-Prüfvertrag hinzu: Default-Supervisorinfo bestätigt Version/Hostname; Finance bestätigt geheimnisfrei tatsächliche Optionssicherheit. Der Bytevergleichstest entfällt hier, weil es im Paket keine zweite Controllerquelle gibt; beide Testsuites importieren unmittelbar dieselbe Datei. Manifestrechte, sieben Tage und 84 Tage bleiben erhalten. Dieser Nachtrag benötigt vor Veröffentlichung erneut Review.

Öffentliche Primärquellen: HA-Core [WebSocket 2026.9.4](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/backup/websocket.py), [Manager](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/backup/manager.py), [Config einschließlich Tagestimer](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/backup/config.py), [Models](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/backup/models.py), [Supervisor-Anbindung](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/hassio/backup.py); zusätzlich geprüft gegen [2026.10.2](https://github.com/home-assistant/core/tree/2026.10.2/homeassistant/components/backup). Container/Manifest: [offizielles SSH-Buildmanifest](https://github.com/home-assistant/addons/blob/master/ssh/build.yaml), [HA-App-Konfiguration](https://contributors.home-assistant.io/apps/reference/configuration/), [Alpine-Paket](https://pkgs.alpinelinux.org/package/v3.24/community/x86_64/py3-websocket-client). Quellenprüfung: 19.10.2026.
