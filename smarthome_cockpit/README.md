# Smart Home Cockpit

Das Cockpit läuft als Home-Assistant-Add-on auf dem HA-Server. Tokens und
Secrets werden ausschließlich über die geschützte Add-on-Konfiguration gesetzt.
Der veröffentlichte Dienst ist Port `8767`; Cloudflared soll auf den
Home-Assistant-Host-Port `8767` zeigen. HAL9000 ist kein Produktivbestandteil.

## Eigene Musik über Music Assistant

Das Cockpit spricht serverseitig die offizielle [REST API](https://www.music-assistant.io/api/)
an: `POST /api`, mit `Authorization: Bearer`. Add-on-Optionen sind
`music_assistant_url` (Server-Ursprung, z. B. `http://music-assistant:8095`),
`music_assistant_token` (geschütztes Passwortfeld) und
`music_assistant_allow_playback` (standardmäßig `false`). Entsprechende
Umgebungsvariablen heißen `MUSIC_ASSISTANT_URL`, `MUSIC_ASSISTANT_TOKEN` und
`MUSIC_ASSISTANT_ALLOW_PLAYBACK`. Tokens gelangen weder ins Browser-JSON noch in Logs.

Bei leerer MA-Token-Option kann der HA-Einstiegspunkt den privaten Handoff
`/config/private_smarthome_music/cockpit-ma.json` verwenden: `schema: 1`,
`music_assistant_url`, `music_assistant_token` und `music_assistant_allow_playback: false`.
Verzeichnis und Datei benötigen exakt Modus `700` beziehungsweise `600`; Symlinks
und Dateien über 16 KiB werden abgewiesen. Optionen und bereits gesetzte Tokens
haben Vorrang. Der Fallback übernimmt ausschließlich MA-URL/Token und sperrt
Wiedergabe immer; HA-/Spotify-Zugangsdaten werden dadurch nicht verändert.
Ein installierter Stand 0.1.5 erhält diese Fähigkeit erst nach einer separat
freigegebenen Code-Aktualisierung. Fehler führen ohne Secret-Ausgaben zum Abbruch
des Fallbacks.

Die API liefert Status und tatsächliche Player sowie seitenweise lokale Library-Titel
über `/api/music/status`, `/api/music/players` und `/api/music/tracks?q=&offset=0`.
Pro Seite werden 50 Library-Einträge gelesen; `next_offset` bezieht sich auf diese
ungefilterte MA-Liste. Auch eine leere lokale Trefferseite kann deshalb eine Folgeseite haben.
Zugelassen sind verfügbare Zuordnungen aus `filesystem_local`, `filesystem_smb`
und `filesystem_nfs`; Cloud-, Streaming-, Radio-, URL- und rohe Dateipfadziele sind ausgeschlossen.
Bei mehreren Quellen wird ausschließlich die geprüfte lokale Provider-URI gespielt.

`POST /api/music/play` erhält `{ "uri": "…", "player_id": "…" }`;
`POST /api/music/control` erhält `{ "player_id": "…", "command": "pause|resume|stop" }`.
Wiedergabe benötigt die separate Freigabe. Der Player muss tatsächlich verfügbar sein;
die aktive Queue wird bei MA aufgelöst. `status: ok` bestätigt nur die erfolgreiche
REST-Antwort, keine physische Wiedergabeprüfung. Ausfälle werden als nicht verfügbar gemeldet.
Musik-Endpunkte erlauben kein CORS. Die bestehende Zugangssicherung des veröffentlichten
Cockpits muss weiterhin aktiv sein.

Cover kommen ausschließlich serverseitig über `/api/music/artwork?uri=…` aus
MA `/imageproxy/<proxy_id>`; ältere Versionen unterstützen lokale Provider-Cover
über `/imageproxy?provider=…&path=…`. Fremde Cover-URLs werden nicht übernommen.
Timeout: 8 Sekunden je MA-Aufruf; JSON maximal 2 MiB, Cover maximal 4 MiB;
Redirects werden nicht verfolgt. Verifiziert gegen öffentliche Server-Source Tag `2.10.4`
und den älteren Imageproxy-Vertrag `2.8.7`; die installierte Version benötigt einen
separaten Kompatibilitätscheck ohne Wiedergabe.

Lokale Tests ohne Netzwerk: `python -m unittest discover -s smarthome_cockpit/tests -v`.
