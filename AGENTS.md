# AGENTS.md — smarthome-spotify-addon-ha

## Projektkonventionen

Dieses Repository enthält die Home-Assistant-Distribution unter `smarthome_spotify_bridge/`. Die dortige `README.md` und Add-on-Konfiguration bleiben maßgeblich; die Hauptbranch heißt `master`. Änderungen an übernommenem SmartHome-Code mit der Quelle abgleichen, keine stillschweigende Divergenz erzeugen. Keine Secrets, lokale Profile oder Zustandsdaten in Git oder Delegationskontext aufnehmen. Prüfungen ohne produktive Geräte-/Wiedergabeaktionen durchführen; Deployment benötigt einen eigenen Auftrag.

## Fast-Subagent-first

Projektspezifische Regeln sowie bestehende Git-, Commit-, Review-, Architektur-, Test-, Datenschutz- und Sicherheitsregeln haben Vorrang. Delegation erweitert weder Auftrag noch Repositorygrenzen und erteilt keine zusätzliche Erlaubnis für Arbeitsbäume; bestehende Repository-Regeln gelten.

Codex bleibt für Gesamtverständnis, Planung, Priorisierung, kritische fachliche und architektonische Entscheidungen, Schnittstellen, Integration, Konfliktauflösung und abschließende Verifikation verantwortlich. Delegiere Arbeit, nicht Verantwortung. Vor größeren Blöcken werden Pakete mit bekannten Schnittstellen und prüfbaren Abnahmekriterien festgelegt.

Bei geeigneter aktuell auswählbarer Modell-/Effort-Kombination wird ausführbare Routinearbeit überwiegend delegiert. Fast Worker übernimmt kleine, klar abgegrenzte Aufgaben mit kleinem Kontext, dem kleinsten ausreichend fähigen aktuell auswählbaren Modell und niedrigem oder angemessenem Reasoning. Standard Worker übernimmt mittlere Mehrdateiänderungen und Refactorings mit passender Kapazität und medium Reasoning, sofern unterstützt. Unterstützte Tool-Overrides für Modell und Effort sind zu verwenden; wiederholt abgelehnte Kombinationen werden nicht erneut versucht. Bei fehlendem Worker führt Codex die Arbeit aus; technische Nichtverfügbarkeit allein erfordert keine Rückfrage.

Geeignete Routinearbeit umfasst Funktionen, Features, Bugfixes, Parser/Adapter, Import/Export, Datenmodelle, GUI/CLI/Plots, Tests und Fixtures, Testdaten, Dokumentation, Typisierung, Ruff-/mypy-/Compile-Fixes, Refactorings, API-Anpassungen, begrenzte Analysen sowie Testausführung und Loganalyse. Unabhängige Pakete werden möglichst parallel mit eindeutiger Dateizuständigkeit vergeben; künstliche Aufteilung bei Kopplung, Konfliktrisiko oder unverhältnismäßigem Koordinationsaufwand unterbleibt.

Fast Worker liefert kompakt Ergebnis, Status, geänderte Dateien, relevante Befunde und offene Punkte. Test- und Logaufträge melden zusätzlich Befehl, Exit-Code, fehlgeschlagene Tests, relevante Fehlermeldungen, vermutete Ursache und gegebenenfalls Korrekturvorschlag. Standard Worker ergänzt Schnittstellen, Risiken und Verifikation. Codex integriert und prüft den Gesamtstand unabhängig.

Claude wird bei Verfügbarkeit als unabhängiger Reviewer über die normale, ausschließlich lesende CLI eingesetzt; kein Ultrareview und keine zweite Implementierung. Zu prüfen sind Fachlichkeit, Architektur, Seiteneffekte, Komplexität, Tests und versteckte Annahmen; mathematische, physikalische, statistische und finanzielle Änderungen werden nach Möglichkeit unabhängig hergeleitet oder gegengerechnet. Bei fehlendem oder rate-limitiertem Claude führt Codex ein ausdrücklich bezeichnetes Selbstreview durch und arbeitet normal weiter. Der Review-Status allein erteilt keine Commit- oder Push-Freigabe; bestehende Branch-, Commit-, Merge- und Veröffentlichungsregeln bleiben unverändert und werden nicht implizit autorisiert.

Eskalation erfolgt nur bei nachgewiesener Unfähigkeit, begründetem Zweifel, wiederholten Fehlern oder echter Kopplung. Im Arbeitsbericht stehen Anzahl und Arten der Worker-Pakete, delegierte Dateiumfänge, selbst ausgeführte Arbeiten samt Grund, tatsächlich verfügbare Modell-/Effort-Auswahl und Review-Status. Zu geringe Delegation trotz geeigneter Arbeit führt zur stärkeren Zerlegung im nächsten Block.
