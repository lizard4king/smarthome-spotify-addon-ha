# Zusätzlicher Lumi-Dialog im Cockpit

Vorbereitet am31.10.2026 für SmartHome#69, Ergänzung zu#66.
Kein Produktionspaket installiert, kein Merge und keine Freigabe aktiviert.
Der bestehende Alexa-/Notion-/Memory-Pfad bleibt erhalten.

## Benutzung

Im Leitstand führt „Mit Lumi sprechen“ zu `lumi.html`. Erst „Gespräch starten“
öffnet eine eigene Browser-Sitzung. Texteingabe bleibt möglich, wenn lokale
Spracheingabe oder eine lokale deutsche Stimme fehlen. Weitere Beiträge
können während einer Antwort eingegeben beziehungsweise gesprochen werden;
höchstens drei warten auf die bestätigte vorherige Verarbeitung.

„Unterbrechen“ stoppt ausschließlich die lokale Sprachausgabe und setzt das
Zuhören fort. Es storniert keinen bereits übermittelten Server-/Geräteauftrag.
„Gespräch beenden“ beziehungsweise der ausdrücklich lokale Sprachbefehl
„Lumi stopp“ oder „Gespräch beenden“ beendet Aufnahme und Sprachausgabe,
verwirft nicht gesendete Beiträge und sperrt verspätete Antworten. Ein neuer
Start erhält einen neuen Kontext und eine neue Sitzungskennung.

Die Steuerung bleibt beim Scrollen sichtbar. Spracheinstellungen sind
eingeklappt. Ohne Kopfhörer ist das Mikrofon während der Sprachausgabe aus,
damit Lumi sich nicht selbst neue Befehle erteilt; die Unterbrechen-Taste
bleibt erreichbar. Der bewusst zuschaltbare Kopfhörer-Modus lässt lokale
Spracherkennung auch während der Ausgabe zu und unterbricht diese bei neuer
Sprache. Dieser Betrieb ist bis zur echten Geräteabnahme experimentell.

## Sprache und Daten

Es wird ausschließlich `SpeechRecognition.processLocally=true` verwendet.
Ein vorhandenes lokales Deutschpaket und die entsprechende Browser-API
werden vor dem Start geprüft. Es gibt weder automatische Sprachpaket-
Downloads noch einen Rückfall auf Cloud-Spracherkennung. Lokale deutsche
`SpeechSynthesis`-Stimmen werden über `localService=true` ausgewählt; eine
entfernte Standardstimme wird nicht stillschweigend benutzt.

Die neue Server-API erhält ausschließlich den erkannten beziehungsweise
getippten Text. Audio wird hier nicht hochgeladen oder gespeichert. Text geht
über die bestehende private Lumi-Bridge an die vorhandene Codex CLI; es wird
kein zusätzlicher Modell-API-Zahlpfad eingeführt. Die lokale Browserunterstützung
ist experimentell und muss auf dem tatsächlichen Handy/Tablet geprüft werden.

Browserdialoge haben ausschließlich eigenen RAM-Kontext, getrennt von Alexa-
Sitzungen. Es gibt keine automatisch übernommene Personenkennung, persönliche
Erinnerung oder private Notion-Freigabe. Der vorhandene Kontextmechanismus
begrenzt Sitzungskontext auf zwölf Beiträge/zehn Minuten Inaktivität.
Ein lokaler Vergiss-Befehl löscht ausschließlich diesen Browserkontext.
Persönliche bestehende Erinnerungen bleiben unangetastet.

## Geschützte Verbindung

Die vorhandene authentifizierte Cloudflared-Verbindung bleibt die Anmeldung.
Origin/CSRF sind kein Ersatz dafür. Nicht ohne diese Zugangskontrolle
einschalten und keinen Host-Port öffnen. Die neue Funktion ist standardmäßig
aus. Vor einer späteren Installation ist ein frischer Live-Quell-/Options-
und Sicherungsabgleich erforderlich; die alten Memory-/Identity-Installer
sind dafür nicht geeignet.

Vorbereitete native Optionen, ohne produktive Werte:

| App | Option | Standard |
|---|---|---|
| Cockpit | `lumi_chat_enabled` | `false` |
| Cockpit | `lumi_chat_origin` | leer, später exakter HTTPS-Ursprung |
| Cockpit | `lumi_voice_bridge_secret` | leer, Passwortfeld |
| Codex Voice | `lumi_browser_control_enabled` | `false` |

Ein vorhandener Bridge-Schlüssel darf nur bei gesondert beauftragter
Einrichtung innerhalb der geschützten Serverkonfiguration hinterlegt werden,
niemals im Chat, JavaScript, URL, Git oder Protokoll. Der vorhandene Alexa-
Bridge-Schlüssel wird nicht ersetzt. Beide neuen Serverteile müssen zusammen
bereitgestellt sein. Gerätebefehle aus dem Browser sind zusätzlich nur bei
explizit aktiviertem Codex-Flag und weiterhin aktivem bestehendem Control-
Client möglich; die unveränderten Geräte-/Providergrenzen gelten weiter.

Das Cockpit vergibt zufällige HttpOnly-/SameSite-Sitzungscookies und einen
gebundenen CSRF-Wert. Es akzeptiert weder Browser-Personenkennungen noch
beliebige URLs/Jobkennungen. Der private Client signiert nur die feste
`local-codex-voice:8098`-Bridge und prüft deren Antwortsignatur. Unklare
Übermittlungen werden reserviert und niemals automatisch wiederholt.
Auf Gesprächsende erfolgt keine verspätete Cookie-Löschung, die ein neueres
Gespräch in einem anderen Tab löschen könnte.

## Betriebsgrenzen und offene Abnahme

Der Browser muss im Vordergrund bleiben. Verbergen der Seite pausiert
Mikrofon und Ausgabe; Fortsetzen ist ausdrücklich. Verbindungsfehler,
abgelaufene Berechtigungen oder nicht unterstützte Spracherkennung werden
sichtbar gemeldet. Dies ist keine Garantie für ein dauerhaft im Hintergrund
laufendes Mikrofon. Lokale Sprachausgabe und ein bereits angenommener
Geräteauftrag haben unterschiedliche Lebenszyklen.

Servergrenzen:32 gleichzeitige Sitzungen,64 Beitragskennungen pro Sitzung,
30Minuten Inaktivität beziehungsweise acht Stunden maximale Laufzeit.
Der private bestehende Jobdienst verarbeitet weiterhin seriell; weitere
Sitzungen können einen belegten Dienst melden. Das begrenzte RAM-Gespräch
ist kein neuer dauerhafter persönlicher Speicher. Ein Browser-Cookie führt
jeweils ein aktives Gespräch; ein neuer Start ersetzt das vorige dort.

Getrennt zu prüfen bleiben: lokale deutsche Spracherkennung auf dem konkreten
Handy/Tablet, hörbare lokale Ausgabe, Sprachunterbrechung mit Kopfhörern,
bewusst freigegebene Geräteaufträge, produktive Anmeldung/Verbindung und
vollständige Alexa-/Notion-Abnahme nach Installation.

Primärquellen: [Web Speech API](https://webaudio.github.io/web-speech-api/),
[lokale Spracherkennung](https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition/processLocally),
[lokale Stimmen](https://developer.mozilla.org/en-US/docs/Web/API/SpeechSynthesisVoice/localService).
