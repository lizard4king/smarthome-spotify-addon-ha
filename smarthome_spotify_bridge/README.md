# SmartHome Spotify Bridge (Home Assistant Add-on)

Dieses Add-on führt die signierte Spotify-Bridge dauerhaft in Home Assistant
aus. Es verwendet keinen OpenAI-Aufruf und keine GitHub-Runner.

Version `0.1.20` verteilt die begrenzten Suchvorschläge auf Titel, Alben,
Playlists und Künstler. Eine volle Titelseite verdrängt dadurch nicht mehr
alle anderen Kategorien. Die automatische Auflösung eines einzelnen Treffers
behält ihre bisherige Titelpriorität. Konten, Ziele und Schreibrechte ändern
sich durch dieses Update nicht.

Vor dem Start müssen im Add-on die Optionen `bridge_secret` (mindestens 32
Zeichen) und `spotify_client_id` gesetzt werden. Die Spotify-Profile und Ziele
werden unter `/config/spotify_profiles.json` und `/config/spotify_targets.json`
bereitgestellt. Die beiden Spotify-Konten müssen auf diesem Home-Assistant-
System separat per PKCE autorisiert werden; Windows-Credential-Store-Einträge
werden nicht übernommen.

Der Container veröffentlicht Port 8766 nur innerhalb des Home-Assistant-
Netzwerks. Eine Cloudflare-Route wird erst nach einem erfolgreichen lokalen
Health-/Signaturtest eingerichtet.
