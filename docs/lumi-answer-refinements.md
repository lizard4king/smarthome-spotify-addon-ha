# Lumi: längere Antworten, Fehler und Begrüßung

Dieser Arbeitsstand übernimmt die tatsächlich installierten Lumi-Laufzeitquellen
von Codex Voice 0.1.12 und Own Music Alexa 0.1.8. Die bisherige öffentliche
`master`-Branch enthält diese lokalen Erweiterungen noch nicht vollständig.
Der Import bleibt auf diese beiden Apps und ihre zugehörigen Tests begrenzt.
Andere Apps werden nicht durch die lokale Installationskopie ersetzt.

Die Kandidatenversionen sind Codex Voice **0.1.13** und Own Music Alexa **0.1.9**.
Ihre Optionsschemata, Standardoptionen und Sicherheitsgrenzen bleiben erhalten.
Insbesondere werden keine Sprachprofile zusätzlich freigegeben und keine
vorhandenen Gesprächsdaten gelöscht. Die bestehende lokale Erinnerung bleibt
getrennt vom flüchtigen Zustand eines gerade vorgelesenen Antwortteils.

## Längere Antworten

Der Gesprächsdienst verarbeitet weiterhin höchstens 6.000 Zeichen je Antwort.
Die automatische Sprachausgabe hat eine eigene Grenze von 2.000 Zeichen.
Die Antwort wird deshalb in Teile mit jeweils höchstens 2.000 Zeichen zerlegt;
der Hinweis auf den nächsten Teil zählt bereits zu dieser Grenze.
Satz- und Wortgrenzen werden bevorzugt; auch unpunktierter Text bleibt erhalten.

Nach einem Teil mit weiterem Inhalt lautet der Hinweis:

> Für den nächsten Teil sage: Alexa, frage Lumi Chat: Antwort.

`GetCodexAnswerIntent` liefert den nächsten Teil, `AMAZON.RepeatIntent` den
aktuellen Teil erneut. Wiederholte Übertragungen desselben Abrufbefehls blättern
nicht mehrfach. Eine neue Frage oder Stopp verwirft den alten Rest. Die
ursprünglichen Job-, Personen- und Sitzungsgrenzen bleiben erhalten.

Ein unbekannter Sprecher erhält weiterhin nur Sitzungskontext. Nach einer neuen
anonymen Sitzung kann deshalb auch ein Rest aus einer früheren Sitzung nicht
abgerufen werden. Dafür wird keine gemeinsame Ersatzidentität eingeführt.

## Verständliche Fehler

Die interne Antwort nennt nur einen festen Fehlercode. Lumi unterscheidet
Zeitüberschreitung, nicht bestätigte Anmeldung, Auslastung und Nichterreichbarkeit.
Die gesprochenen Texte enthalten keine internen Fehlermeldungen, Zugangsdaten
oder Gesprächsinhalte. Ein noch laufender Auftrag wird als solcher bezeichnet;
die frühere pauschale Behauptung einer fehlenden Sprachausgabe entfällt.

Der bestehende Legacy-Voice-Vertrag bleibt kompatibel. Eine fehlerhafte
Signatur darf nicht als vertrauenswürdige Statusmeldung ausgegeben werden.

## Sofortige wechselnde Begrüßung

Der Alexa-Start verwendet eine lokale Auswahl kurzer deutscher Grüße. Er wartet
weder auf das Modell noch startet er einen Hintergrundauftrag. Ein unmittelbar
anschließender Frageauftrag wird dadurch nicht blockiert.

Die Grüße sind namen- und fragenfrei, verwenden keine Gesprächsinhalte und
behaupten keine Sprecheridentität. Neue Aufrufe vermeiden unmittelbare
Wiederholungen. Derselbe erneut übertragene Aufruf bleibt innerhalb seines
begrenzten Cachefensters stabil. Dieser Zustand liegt ausschließlich im RAM.
Es handelt sich um variierende lokale Grüße, nicht um aus privaten Gesprächen
generierte Begrüßungen.
Der ältere authentifizierte Begrüßungsauftrag bleibt für den schrittweisen
Updateweg und ältere Clients kompatibel; der neue Alexa-Start verwendet ihn nicht.

Auch die erste Wissensfrage erhält den Lumi-Persönlichkeitsprompt, wenn die
Gerätesteuerung aktiviert ist. Der bestehende Planner prüft weiterhin zuerst
die aktuelle Äußerung. Eine validierte Geräteaktion wird genau einmal ausgeführt;
nur bei einer Wissensfrage folgt der getrennte Gesprächspfad. Der Planner erhält
dadurch keine neuen Berechtigungen oder längeren Aktionsantworten.

Scheitert während einer laufenden Berechnung nur der Transport, bleibt die
Jobkennung bis zu ihrer ursprünglichen Ablaufzeit für einen bewussten erneuten
Antwortabruf erhalten. Das ist kein automatisches erneutes Absenden der Frage
und keine Wiederholung einer unbestätigten Sprachausgabe.

## Prüfung und Installation

Die Offline-Prüfungen nutzen synthetische Identitäten und temporäre Dateien.
Sie ersetzen keinen Hörtest. Das normale Claude-CLI-Review, Paket- und
Quellhashes sowie die getrennte Live-Abnahme werden im Releasebericht erfasst.
Die Lambda-Vertragstests laufen im getrennten SmartHome-Repository. Historische
repositoryübergreifende Tests werden im Add-on-Repository ausdrücklich als
solche übersprungen, wenn dessen Lambda-Quelle dort nicht vorhanden ist.

Ein reguläres Supervisor-Update verwendet ausschließlich `/local_apps`.
Quellabweichungen, veränderte Optionen oder ein unklarer Update-Ausgang führen
zum Stopp. Kein `rebuild --force`, keine neuen Ports und keine geänderten
Zugriffsrechte. Ein Rückweg darf gespeicherte Gespräche nicht überschreiben.

Refs: lizard4king/SmartHome#55, lizard4king/SmartHome#57.
