# Lumi: Musikbefehle und gezielte Rückfragen

## Belegter Fehler

Am 31.10.2026 erkannte Alexa beim Büro-Auftrag den Titel `Ein echtes Bier`
statt `Ein echtes Wir`. Die unveränderte freigegebene lokale Musikquelle
enthält `Ein echtes Wir` genau einmal und verfügbar. Ein eindeutiger Auftrag
über den laufenden Lumi-/Codex-Dienst startete den Titel; die Wiedergabe wurde
vom Nutzer im Büro bestätigt. Das ist noch kein bestandener Sprachtest der
vorliegenden Korrektur. Nachverfolgung: SmartHome Issue #68.

Zusätzlich ersetzte `query_voice` die Rückfrage einer `none`-Planung bei
vorhandenem Kontext durch eine zweite allgemeine Modellantwort. So konnte die
gezielte Rückfrage verloren gehen. Ein früherer Sprachwunsch ohne Zielraum
erhielt tatsächlich eine unbrauchbare pauschale Nicht-können-Antwort; dessen
interner damaliger Modellplan wurde nicht gelesen.

## Korrektur und Grenzen

`clarify` ist ausschließlich ein lokales Planungsergebnis. Ein fehlender
Titel, Alias, Raum oder Spotify-Profil wird konkret erfragt. Der validierte
Text geht direkt zurück; `ControlClient.execute` und der unveränderte
Gerätebroker akzeptieren keine Clarify-Aktion. Allgemeine Fragen verwenden
weiter den bisherigen Gesprächskontext. Frühere Gesprächsaufträge werden
nicht zu einer aktuellen Ausführungsfreigabe.

Ein nachgereichtes `im Büro` ergänzt bisher keinen gespeicherten Auftrag.
Der vollständige Titel samt Raum muss erneut genannt werden. Diese Änderung
ist deshalb kein vollständiger mehrstufiger Befehlsdialog.

Bei null Bibliothekstreffern werden höchstens zwei unterschiedliche Wörter
mit mindestens vier Zeichen über denselben `MusicAssistant.tracks`-Adapter
gesucht, jeweils höchstens die erste Seite mit 50 Einträgen. Die bestehende
Provider-, Verfügbarkeits- und Dateipfadprüfung bleibt erhalten. Höchstens
drei ähnliche Titel werden als Vorschläge gesprochen. Es gibt keinen
automatischen Wiedergabeversuch und keinen versteckten Ja/Nein-Folgedialog.
Eine Netzwerkstörung wird nicht als fehlender Titel ausgegeben.

Dieser Arbeitsstand übernimmt die fünf öffentlichen Cockpit-Dateien aus dem
bereits installierten neueren Stand 0.1.21 (`877b859`) unverändert. Die
ältere Cockpit-Oberfläche wird nicht zurückgespielt. Notion-Module,
Erinnerungsspeicher, Sprecherfreigaben und Zugangsdaten werden nicht geändert.

## Prüfung und Auslieferung

72 gezielte Tests für Rückfragen, Musikvorschläge, bestehende Musiksteuerung,
Notion-Runtime und Gesprächspersönlichkeit bestanden im endgültigen Stand.
Der normale ausschließlich lesende Claude-CLI-Review des vollständigen
betroffenen Quell- und Teststands fand keine bestätigte neue Regression oder
Sicherheitsverletzung. Ein unabhängiger Zweitreview bestätigt die Einordnung.
Die sechs geprüften Quell-/Testdateien wurden anschließend über ihre SHA256
erneut mit dem eingefrorenen Reviewstand verglichen.
Der gesonderte bestehende Runtime-Lauf bestand mit 69 Tests und 18
Plattform-Skips bei ausschließlich testlokalem Log-Verzeichnis.

Ein breiterer Windows-Lauf ist nicht insgesamt grün: Ein unveränderter
Parallelitäts-Test erwartet nur 200-Antworten, obwohl eine laufende reservierte
Operation bereits in der Basis vorübergehend 503 liefert. Netzwerk-Tests
trafen auf gesperrtes Loopback; ein alter Deep-JSON-Test hat unter Python
3.14 andere Decoder-Grenzen. Keine Produktionsänderung wurde zur Umgehung
dieser Befunde vorgenommen. Die gezielte grüne Auswahl ersetzt diese
getrennten offenen Prüfungen nicht.

Kein Paket aus diesem Arbeitsstand ist auf HA installiert. Die bisherigen
PRs bleiben Entwürfe; kein Merge. Vor einer Installation sind der aktuelle
öffentliche Quellbestand, private Sicherung und die unveränderten aktiven
Optionen erneut abzugleichen. Die alte Memory-Installationsanleitung ist
hierfür ungeeignet. Das Projekt verlangt einen eigenen Deployment-Auftrag.

## Gesprächsfortsetzung ist getrennt

Issue #66 bleibt offen. Der wiederholte Bürotest ergab: Antwort kommt,
Ring geht aus. Im aktuellen Relay endet die Sitzung nach vier Sekunden
Warten; die spätere HA-Ansage öffnet das Mikrofon nicht. Die hier geänderte
Rückfragebehandlung korrigiert diesen Transportweg nicht.

Auch reguläre Alexa-Sitzungen besitzen ein begrenztes Zuhörfenster und enden
nach ausbleibender Antwort. Dauerhaftes, unterbrechbares Gespräch bis zu einem
ausdrücklichen Stopp ist damit nicht zugesichert. Keine automatische
Neueröffnung übernimmt alte Sprecher-, Notion- oder Memory-Rechte.

Primärquellen: [Alexa-Sitzungen](https://developer.amazon.com/en-US/docs/alexa/custom-skills/manage-skill-session-and-session-attributes.html)
und [Progressive Responses](https://developer.amazon.com/en-US/docs/alexa/custom-skills/send-the-user-a-progressive-response.html).
