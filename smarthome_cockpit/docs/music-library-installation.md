# Installation des kombinierten Musik-/Lumi-Cockpits

Der Quellstand 0.1.18 kombiniert PR #52 mit den bestehenden Cockpit-0.1.17-
Lumi-Funktionen. Music Alexa und Codex Voice werden bei diesem Schritt nicht
ersetzt. Zugangsdaten und persönliche Optionen gehören ausschließlich auf HA.

## Vorbereiteter Vertrag

- Der bisherige Host-Port `8767/tcp` bleibt `null`. Der interne Listener bleibt
  auf 8767 für den vorhandenen geschützten Zugang erreichbar.
- Alle bisherigen Optionen bleiben erhalten. Hinzu kommen `music_browser_root`
  und die unabhängige, standardmäßig ausgeschaltete Browserfreigabe.
- `media:ro` bindet Medien ausschließlich lesend ein. Der Wurzelpfad muss zur
  konkreten `filesystem_local--…`-Instanz passen. Kein anderer Provider wird
  automatisch ausgewählt und kein Verzeichnisname wird geraten.
- `build.yaml` benennt passende Basisimages für beide deklarierten Architekturen
  amd64 und aarch64. Die tatsächlichen Bauprüfungen sind im PR-Nachweis getrennt
  von den Geräteprüfungen dokumentiert.

## Rückweg und tatsächliche Installationsquelle

Vor einer Installation sind laufende Version, effektive Optionen und Grenzen
(Netzwerk, Schutzmodus, Startverhalten, Watchdog, Hostname) privat auf HA zu
sichern. Eine verschlüsselte Teilsicherung muss abgeschlossen sein und den
Cockpit-Eintrag tatsächlich enthalten. Das Passwort bleibt auf HA; weder
Passwort noch private Optionen werden in Git oder Reviewprotokolle übernommen.

Die historische Image-basierte Cockpit-Registrierung ist eine besondere Grenze:
Eine Datensicherung garantiert dort keine Wiederherstellung eines nicht mehr
abrufbaren Images. Vor einem Austausch muss zusätzlich entweder das ursprüngliche
Image verfügbar sein oder der verifizierte vorherige Quellstand als baubares
Rückwegpaket bereitstehen. Der Quellstand und die effektive Supervisor-Registrierung
müssen dazu zusammenpassen. Ein abgeschlossener Backup-Job reicht nicht als
Nachweis einer erfolgreichen Wiederherstellung.

Die tatsächlich von Supervisor verwendete Quelle ist zu prüfen. Historische
Verzeichnisse unter `/addons` sind kein Beweis für die aktive Bauquelle; bei den
Lumi-Updates vom 17.10. war `/local_apps` maßgeblich. `rebuild --force` wird für
den alten nicht veröffentlichten Image-Namen nicht als Ausweichweg verwendet.
Vor Bereitstellung muss der Paket-Hash geprüft werden. Erst nach passender
Registrierung, vollständiger Sicherung und geprüftem Rückweg darf der normale
Supervisor-Updateweg verwendet werden.

## Abnahme nach eigener Installation

Zunächst Version, laufenden Zustand, gespeicherte Optionen und weiterhin
geschlossene Host-Ports prüfen. Dann Bibliothek und Audio über den geschützten
Zugang lesen; unberechtigte externe Zugriffe müssen abgewiesen werden. Erst eine
bewusste Freigabe aktiviert Browseraudio. MA-Spielerfreigabe und HA-Schreibrechte
werden dadurch nicht erweitert.

Danach auf dem genutzten Browser Start, Pause/Fortsetzen, Titelsprung, Springen
im Titel sowie Alben und Ordner prüfen. Lautsprecher, Lumi-Sprachantworten und
Raumzielsteuerung erhalten eine eigene reale Abnahme. Lokale Tests ersetzen
keinen Hörtest.

Primärquelle: [HA-App-Konfiguration](https://contributors.home-assistant.io/apps/reference/configuration/)
für Host-Port `null`, Medienzuordnung und lokalen Bau ohne `image`-Eintrag.
