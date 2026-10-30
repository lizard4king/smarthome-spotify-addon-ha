# Aktueller Lumi-Stand: inkrementelles Identity-Update

Stand: 26.10.2026. Diese Anleitung ersetzt den älteren Installationsweg für
0.1.11/0.1.7. Dessen Skript und Prüfsumme bleiben unverändert.

## Umfang und feste Voraussetzungen

`deployment/lumi_current_identity/install.py` arbeitet ausschließlich auf der
geprüften Basis `2a3fd98`: Codex Voice 0.1.14 und Music Alexa 0.1.9, beide gestartet.
Das vollständige öffentliche Quellinventar einschließlich des bekannten zusätzlichen
Codex-README muss `baseline.json` entsprechen. Nur CRLF/LF-Unterschiede werden toleriert.
Fehlende, andere oder zusätzliche Dateien und unbekannte Versionen sperren die Änderung.

Nur Music wird zu 0.1.10 neu gebaut. Vier bestehende öffentliche Music-Dateien werden
atomar ersetzt, `lumi_identity.py` wird exklusiv ergänzt. Codex, gespeicherte Gespräche,
Notion, App-Optionen, Owner-Listen und Zugriffsgrenzen werden nicht geschrieben.
Die vorhandenen Options-/Systemgrenzen werden intern vor und nach der Änderung per
Prüfsumme verglichen. Fehlende Metadaten sperren die Installation. Private Werte werden
weder ausgegeben noch in das Journal übernommen.

Das Kandidaten-ZIP enthält exakt:

- `music_alexa/Dockerfile`
- `music_alexa/companion.py`
- `music_alexa/config.yaml`
- `music_alexa/lumi_identity.py`
- `music_alexa/lumi_relay.py`

Das Paket lässt sich aus dem geprüften Checkout ohne Netzwerk erstellen:

```sh
python3 deployment/lumi_current_identity/build_package.py --output lumi-music-identity-0.1.10.zip
```

Der Zielpfad darf noch nicht existieren. Der Builder prüft bereits vor dem Schreiben
alle fünf Quellen gegen die festen Prüfsummen und gibt die vollständige ZIP-SHA256 aus.

Das gesamte ZIP und jeder einzelne Inhalt werden gegen Prüfsummen geprüft. Es werden
keine Quellen heruntergeladen. Die Prüfsummen dieser fünf Inhalte sind im Helfer fest
hinterlegt; das gesamte Baseline-Manifest ist ebenfalls fest gebunden.

## Vor der Installation

Ein bereits vorhandenes HA-Backup muss in `ha backups info <slug> --raw-json` Music
Alexa 0.1.9 enthalten. Der Helfer erstellt nicht eigenmächtig ein Backup. Die Metadaten
bestätigen dessen Existenz und Version, nicht unabhängig die Identität aller Imagebytes
oder einen erfolgreichen Wiederherstellungsversuch.

Der Quellpfad muss ausdrücklich angegeben werden und ein echtes, unverlinktes
Verzeichnis sein. Der Journalpfad liegt außerhalb davon, hat ein bereits vorhandenes
Elternverzeichnis und darf noch nicht existieren. Auch seine Eltern dürfen keine
symbolischen Links sein. Die folgenden Platzhalter müssen aus dem tatsächlich
erreichbaren Serverzustand bestimmt werden; `/addons` wird nicht pauschal angenommen.

Der Helfer liest zuerst den vorhandenen Namespace `ha apps` oder `ha addons`. Ein
Alternativversuch ist ausschließlich beim lesenden `info` zulässig. Vor Änderungen
prüft er dessen `update --help` auf die benötigten Optionen. Die schreibenden Befehle
haben keinen zweiten Namespace und keinen automatischen Wiederholungsversuch.

## Ablauf

Standardmäßig wird nur gelesen. Derselbe vollständige Befehl wird für die folgenden
Operationen verwendet:

```sh
python3 install.py plan \
  --source-dir QUELLVERZEICHNIS \
  --package KANDIDAT.zip --package-sha256 VERÖFFENTLICHTE_ZIP_SHA256 \
  --journal-dir NEUES_PRIVATES_JOURNAL
```

Nur bei bestätigtem `baseline_match: true` ist der Installationsaufruf sinnvoll:

```sh
python3 install.py apply \
  --source-dir QUELLVERZEICHNIS \
  --package KANDIDAT.zip --package-sha256 VERÖFFENTLICHTE_ZIP_SHA256 \
  --journal-dir NEUES_PRIVATES_JOURNAL --backup-slug BACKUP_SLUG
```

`apply` legt zunächst ein exklusives privates Journal und bytegenaue Sicherungen der
vier bestehenden öffentlichen Quellen an. Vor jeder Änderung schreibt und synchronisiert
es eine Absichtserklärung. Nach dem Austausch folgt genau ein `ha store reload`, dann
die Prüfung, dass der Katalog 0.1.10 anbietet, und genau ein Music-`update`. Der
Versionssprung benötigt diese Update-Schnittstelle. `rebuild` ist dafür ungeeignet:
Supervisor verweigert es, wenn installierte Version und Store-Version verschieden
sind. Das entspricht dem [Supervisor-Vertrag](https://contributors.home-assistant.io/apps/supervisor-api/apps/)
und dessen [Versionsprüfung](https://github.com/home-assistant/supervisor/blob/main/supervisor/apps/manager.py).
Der frühere quittierte Neubau vom 19.10.2026 betraf dieselbe Versionsnummer und ist
kein Nachweis für einen Versionssprung. Es gibt keinen Rückfall auf `rebuild`.
Anschließend müssen Quellen, gestartete Versionen und alle Options-/Systemgrenzen stimmen.
Ein erfolgreicher Rückgabewert behauptet keine hörbare Echo-Abnahme.

Für eine bewusst schrittweise Durchführung sind `prepare`, danach `stage`, danach
`submit` verfügbar. `prepare` benötigt den Backup-Slug; alle Schritte verwenden dasselbe
Paket, denselben Quellpfad und dasselbe Journal. Ein bereits angefangener Schritt wird
niemals wiederholt. `apply` setzt ein vorhandenes Journal grundsätzlich nicht fort.

## Abbruch und Rückweg

Nach Timeout, verlorener Quittung oder Teilabbruch ausschließlich `status` mit den
unveränderten Argumenten aufrufen. Diese Operation sendet keinen Schreibauftrag.
`outcome_uncertain: true` bleibt ausdrücklich unklar, selbst wenn inzwischen die
Zielversion gestartet ist. Ein vorhandener Versionswert ersetzt keine Quittung und
keinen vollständigen Quell-/Optionsvergleich.

Keinen neuen `apply`-Aufruf mit einem anderen Journal erzwingen. Zuerst den tatsächlichen
Supervisor-Auftrag und das bisherige Journal prüfen. Teilweise ersetzte Quellen werden
nicht automatisch zurückgesetzt, während ein Neubau noch laufen könnte. Ein Rückweg
verwendet das verifizierte HA-Backup und die öffentlichen Originalquellen im Journal;
Optionen, Owner-Keys und Zugangsdaten werden dabei nicht nebenher überschrieben.

Der Identity-Beobachtungszeitraum wird durch diese Installation nicht geöffnet. Neue
Sprecher- oder Notion-Freigaben bleiben eine getrennte Entscheidung. Beide PRs bleiben
bis zur vollständigen technischen und akustischen Abnahme Entwürfe.
