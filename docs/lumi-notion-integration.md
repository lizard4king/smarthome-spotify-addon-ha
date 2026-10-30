# Notion im aktuellen Lumi-Stand

Stand 18.10.2026: Die vorhandene Notion-Umsetzung ist gezielt auf den Lumi-Stand aus Add-on-PR #61 (`0db95d1`) übertragen. Die aktuelle `app.py` bleibt Grundlage. Die Integration umfasst sechs Module, Einstellungen, Start-/Beenden-Anbindung und Docker COPY. Die Installation wird als Codex Voice 0.1.14 vorbereitet. Music Alexa bleibt bei 0.1.9; die vorhandenen Lumi-Funktionen bleiben Grundlage.

## Verhalten und Erhalt bestehender Funktionen

Sprachfragen durchsuchen eine lokale SQLite-Wissenskopie. Die regelmäßige Notion-Suche und Inhaltsübernahme laufen im Hintergrund, getrennt von der Antworterzeugung. Verfügbar sind nur die Seiten, die die eigene Serverintegration erreicht. Notion Search garantiert ausdrücklich keine vollständige Aufzählung des gesamten Kontos; abgeschlossene Pagination ist deshalb kein Beleg für eine vollständige Workspace-Kopie. [Notion Search: Einschränkungen](https://developers.notion.com/reference/search-optimizations-and-limitations).

Notion-Auszüge erreichen ausschließlich den getrennten Antwortaufruf. Der Planner, der strukturierte Geräteaktionen auswählt, erhält weiterhin nur die aktuelle Frage und vorhandene Fähigkeiten. Gesprächsverlauf und Notion-Inhalte erteilen keine Handlungsrechte. Eine aktuelle validierte Aktion wird weiterhin einmal ausgeführt. Die bis zu 6.000 Zeichen langen Antworten, die bestehende Ausgabe in Abschnitten, sichere Fehlertexte, lokale wechselnde Begrüßungen, Modellwahl und persönliche Erinnerung bleiben erhalten. `music_alexa` und die Lambda werden von diesem Paket nicht geändert.

Lesen erfordert ausdrücklich konfigurierte `lumi_notion_owner_keys`. Diese Freigabe ist unabhängig von `lumi_memory_owner_keys`. Auch bei aktivem Gesprächsspeicher entsteht kein Notion-Zugang. Unbekannte Sprecher erhalten weiterhin ihren begrenzten Sitzungskontext, aber keine privaten Notion-Auszüge. Sobald Notion-Auszüge in einer Antwort verwendet werden, speichert Lumi weder diese Frage noch die Antwort im Gesprächsverlauf. Dadurch kann eine gemeinsame Sitzung später keine private Notion-Antwort erben.

Schreibbefehle werden ausschließlich aus der aktuellen Äußerung deterministisch verarbeitet, bevor Modell und Planner aufgerufen werden. Die bestehenden Textformen sind `Notiz in Haus speichern: Filter wechseln` und `Notiz in Haus ändern Block <UUID>: Neuer Text`. Ziel und Inhalt sind ausdrücklich anzugeben; mehrdeutige Seitentitel werden nicht als Ziele verwendet. Schreiben ist auf neue Paragraph-Notizen oder den Ersatz eines eindeutig benannten direkten Paragraphen begrenzt. Die tatsächliche Erkennung dieser Textformen per Alexa ist getrennt abzunehmen.

Die authentifizierte ursprüngliche Anfragekennung wird an den Absender gebunden und vor dem Netzwerkzugriff dauerhaft reserviert. Ein bestätigter Erfolg darf wiedergegeben werden; ein unklarer Vorgang wird nicht automatisch erneut gesendet. Bestätigungen kommen ausschließlich vom Schreibdienst, nicht vom Modell. Nach Neustart sind Schreibziele erst nach neuer erfolgreicher Suche freigegeben; die Freigabe ist zeitlich begrenzt.

## Einstellungen und Zugänge

Alle neuen Funktionen bleiben standardmäßig ausgeschaltet. Bestehende Konfigurationen ohne diese Felder bleiben gültig. Der eigenständige Modus `lumi_knowledge_enabled` liest ausschließlich einen bereits separat befüllten lokalen Cache; er startet keinen Abruf. Ohne diesen vorbereiteten Cache liefert er keine Quellen. Die hier vorgesehene Servereinrichtung nutzt stattdessen `lumi_notion_workspace_enabled`, dessen Hintergrunddienst Katalog und Inhalte selbst abgleicht.

| Einstellung | Standard / Bedeutung |
|---|---|
| `lumi_knowledge_enabled` | `false`; lokaler Cachemodus für ausdrücklich konfigurierte Seiten. |
| `lumi_notion_page_ids` | Leer; begrenzt den lokalen Cachemodus. |
| `lumi_notion_workspace_enabled` | `false`; periodische Suche und Übernahme der gemeldeten Seiten. |
| `lumi_notion_write_enabled` | `false`; erfordert zusätzlich Workspace-Modus und berechtigten Sprecher. |
| `lumi_notion_owner_keys` | Leer; separate geprüfte Sprecherfreigaben für private Notion-Inhalte. |

Die Home-Assistant-Konfiguration enthält zwei zusätzliche Passwortfelder: `lumi_notion_read_token` und `lumi_notion_write_token`. Ein eigener Notion-Serverzugang wird vom Nutzer direkt in diese geschützten Felder eingegeben; der Desktop-Connector wird nicht als Credentialquelle verwendet. Ein eigener nur lesender Zugang für das erste Feld ist möglich. Bei einem einzigen Serverzugang mit Lesen, Einfügen und Ändern kann derselbe Zugang in beide Felder eingetragen werden. Seitenzugriff erhält die Notion-Verbindung ausdrücklich über ihre Inhaltsfreigabe. Benutzerinformationen und Kommentare sind nicht erforderlich.

Beim Start schützt der Root-Prozess die Optionsdatei gegen Zugriff durch den Codex-Benutzer und liest die beiden Zugänge vor dem Wechsel zum unprivilegierten Benutzer ein. Nur der Notion-Adapter erhält sie ausdrücklich im Arbeitsspeicher. Die frühere Übergabe über `LUMI_NOTION_READ_TOKEN` und `LUMI_NOTION_WRITE_TOKEN` wird nicht mehr verwendet. Die feste Unterprozessumgebung enthält keine Notion-Zugänge. Weder Prompts noch API-Antworten oder Logs geben Tokens aus.

Die Passwortfelder maskieren die Anzeige; sie ersetzen keine verschlüsselte Secret-Verwaltung. Home-Assistant-Administratoren und entsprechend berechtigte Sicherungen können die gespeicherten Optionen weiterhin erreichen. Sicherungen müssen daher geschützt bleiben. Die Python-Laufzeit hält Tokens während des Betriebs im Arbeitsspeicher; sichere Speicherlöschung oder Isolation gegen eine vollständige Kompromittierung des Dienstes wird nicht behauptet. Es entstehen keine zusätzlichen Mounts, Ports, Host-, Docker- oder GitHub-Rechte. Fehlende oder ungültige Notion-Zugänge deaktivieren Notion, ohne die übrigen Lumi-Funktionen abzuschalten; eine nicht sicher schützbare Optionsdatei verhindert aus Sicherheitsgründen den Start.

## Fehler und Grenzen

Explizit unvollständige oder ungültige Search-Statusmeldungen ersetzen keinen bisherigen Katalog. Das optionale Feld darf für ältere Antworten fehlen; zusätzliche harmlose Metadaten bleiben kompatibel. Neue Antwortfelder gelten auch bei festgelegter API-Version. [Search-Vertrag](https://developers.notion.com/reference/post-search), [Notion-Versionierung](https://developers.notion.com/reference/versioning).

Die vorhandenen Begrenzungen bleiben aktiv: bis zu 10.000 Suchergebnisse, 8 MiB Katalog, 120 Sekunden Suche; bis zu 32 MiB validierte Cache-Inhalte, 100.000 Blöcke, fünf Ebenen und 900 Sekunden Inhaltsübernahme. Datumsmetadaten sind auf 64 Zeichen begrenzt und werden im Inhaltsbudget mitgezählt. Das Inhaltsbudget ist keine Aussage über die physische SQLite-Dateigröße einschließlich Verwaltungsdaten. Nicht unterstützte Inhalte verhindern die vollständige Textübernahme; Anhänge, fremde verlinkte Seiten und fremde Synced Blocks werden nicht nachgeladen.

Netzwerk- oder Verarbeitungsfehler erhalten den letzten verfügbaren Textstand. Nachweislich entzogene beziehungsweise archivierte Seiten werden entfernt. Nach abgeschlossener Search-Pagination wird der gemeldete Seitenumfang als Cache-Grenze verwendet; Suchindex-Verzögerungen können dabei zeitweise zu fehlenden Quellen führen. Eine optionale Notion-Startstörung beendet nicht den übrigen Lumi-Dienst. Das Beenden signalisiert dem Hintergrundworker das Ende weiterer periodischer Durchläufe; eine harte Unterbrechung jedes bereits laufenden HTTP-Aufrufs wird nicht versprochen.

## Prüfung und spätere Inbetriebnahme

Die Übernahme und Regressionstests verwenden ausschließlich synthetische Identitäten, Daten und Netzwerkantworten. Sie prüfen getrennte Sprecherfreigaben, lokale Suche, fehlende Zugänge, Widerruf, unvollständige Ergebnisse, Wiederholung nach Neustart, unklare Schreibresultate, Unterprozessumgebung sowie Erhalt von Gesprächskontext, langen Antworten und einmaliger Aktionsausführung. Ein normales lesendes Claude-CLI-Review erfasst den vollständigen Änderungssatz.

Vor einer Installation folgen Paketprüfung, normale Claude-CLI-Prüfung und Sicherung des aktuellen Serverstandes. Anschließend sind eigene Notion-Verbindung, Seitenfreigaben und die einmalige Eingabe der Zugänge in Home Assistant nötig. Lesen und Schreiben werden getrennt aktiviert und mit einem benannten Ziel geprüft. Ein Rückweg deaktiviert die neuen Funktionen und verwendet den gesicherten vorherigen Laufzeitstand, ohne bestehende Gesprächsdaten zu überschreiben. Echte Notion-, Server- und Echo-Abnahme bleiben bis dahin offen. [SmartHome-Issue #58](https://github.com/lizard4king/SmartHome/issues/58).
