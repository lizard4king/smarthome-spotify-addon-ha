# Lumi: neueren Stand erhalten und Kennungsfenster ergänzen

Stand: 26.10.2026. Lokale Integration; keine Installation und keine neue Freigabe.

Maßgebliche Quelle ist Commit `2a3fd98b21d200411fa1c214157fe274408b3c86`:
Codex Voice 0.1.14 mit Notion und Own Music Alexa 0.1.9 mit langen Antworten,
Antwortabruf, Wiederholen, Abbruch und sofortigen lokalen Begrüßungen. Die ältere
0.1.11/0.1.7-Basis von PR #80 wird hierdurch ersetzt. Aktuelle Funktionen und
Sicherheitsgrenzen haben bei abweichenden älteren Port-Regeln Vorrang.

## Begrenzte Ergänzung

Codex Voice bleibt einschließlich aller 17 Dateien bytegleich. Music Alexa erhält
Version 0.1.10 und ausschließlich die schon vorbereitete Kennungsanzeige
`/lumi/identity`. Das Fenster ist nur in der privaten Verwaltung hinter bestehender
Basic-Auth erreichbar. Es öffnet höchstens 900 Sekunden, hält maximal 16 abgeleitete
Owner-Keys im Arbeitsspeicher und leert sie bei Ablauf, Schließen oder erneutem Öffnen.
Rohe Alexa-Kennungen werden nicht ausgegeben. Es vergibt weder persönliche Speicher-
noch Notion-Rechte und verändert keine vorhandenen Zuordnungen.

Die Beobachtung erfolgt erst nach Signatur-, Event-, Anfragebegrenzungs- und
Kapazitätsprüfung des bestehenden öffentlichen Relay. Die privaten Verwaltungsrouten
besitzen keinen zusätzlichen Rate Limiter. Die vorhandene Origin-Sperre für schreibende
Verwaltungsaufrufe bleibt erhalten; es gibt keine öffentliche Identity-Route.

| Produktionsbereich | Änderung gegenüber `2a3fd98` |
| --- | --- |
| Codex Voice, Notion und Gesprächsspeicher | Keine, alle 17 Dateien bytegleich |
| Music-Relay und Companion | Nur Beobachteranschluss und drei private Verwaltungsrouten |
| Music-Container und Konfiguration | Identity-Modul mitkopieren; Version 0.1.10, übrige Konfiguration unverändert |
| `lumi_identity.py` | Unverändert aus PR #80 übernommen |

Die vollständige Prüfsummenkarte steht in `lumi-source-preservation-20261026.json`.
Sie weist 33 bestehende Produktionsdateien und genau ein zusätzliches Modul aus.

## Bewahrte Unterschiede zum älteren Port

Auch bei pausiertem Gedächtnis bleibt die aktuelle Lumi-Persönlichkeit wirksam.
Pause erhält gespeicherte Gespräche; ausdrückliches eigenes Löschen funktioniert
weiterhin ohne Modellaufruf. Notion bleibt verfügbar. Die Verarbeitung einzelner
Wörter „Antwort“ und „Wiederholen“, Fehlertexte, Seitenfortsetzung, Namensneutralität
und Trennung unbekannter Sprecher folgen unverändert der neueren Quelle.

Die alten Port-Tests, die Notion-Abwesenheit und das ältere Verhalten bei pausiertem
Gedächtnis verlangten, wurden durch gezielte Erhaltungsprüfungen ersetzt. Daraus
entstand keine Änderung der neueren Produktionslogik.

## Prüfung und Abnahmegrenze

782 relevante Lumi-, Notion-, Antwort-, Zustellungs-, Gateway-, Identity- und
Erhaltungsprüfungen bestanden; sieben bekannte Plattform-Skips. Die Identity-Prüfung
vergleicht den Owner-Key zusätzlich mit einem unabhängig berechneten Testvektor.
Signatur-, Application-, Personen- und Sitzungsfehler lassen das Fenster leer.
Erneutes Öffnen, Ablauf und Schließen löschen bereits beobachtete Ergebnisse.
Syntaxprüfung und `git diff --check` bestanden. Ein unabhängiger lesender Zweitreview
bestätigt den begrenzten Produktionsdiff ohne Blocker; normaler Claude-Review erfolgt
auf dem vollständigen Endstand vor Veröffentlichung.

Zusätzlich bestanden 102 Kernprüfungen für Codex Voice, Bridge, Modellauswahl und
Wire-Integration; 18 bekannte Plattform-Skips. Zusammen sind dies 884 bestandene
Prüfungen und 25 Skips in zwei getrennten Läufen. Ein erster breiter Prüflauf scheiterte
an einem fehlenden temporären Elternverzeichnis vor den betroffenen Testkörpern;
nach Anlegen des Testverzeichnisses bestand derselbe relevante Testbestand.

Keine Aussage über aktuell laufende Serverquellen oder hörbare Echo-Ausgabe folgt
aus diesen lokalen Prüfungen. Das ältere Installationsskript mit SHA256
`a44ee514da718999800ba63d8ac40bd136634e737fd77d4164b64c597b2eedf5`
bleibt unverändert und darf für diese neuere Basis nicht erzwungen werden.
