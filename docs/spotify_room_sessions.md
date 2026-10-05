# Spotify: zentrale Steuerung, getrennte Raumwiedergabe

Das Cockpit verwaltet Titel und Ziel getrennt je Spotify-Profil. Die Suche
wählt einen Titel für das aktuell ausgewählte Profil aus. Der zentrale
Startknopf startet nur die ausdrücklich markierten Profile. Raumkacheln
enthalten keine zweite Spotify-Steuerung, sondern zeigen den abgefragten
Titel, den Wiedergabestatus und das Profil.

Ein Spotify-Konto kann nur ein aktives Connect-Ziel verwenden. Zwei
unterschiedliche Konten können unabhängig auf zwei verschiedenen Alexas
spielen. Eine vorhandene Echo-Gruppe ist ein einzelnes Connect-Ziel; das
nacheinander Umschalten eines Kontos auf mehrere Einzelgeräte ist keine
parallele Wiedergabe.

Die Bridge prüft vor einem Start die vorhandenen Profil-/Ziel-Positivlisten,
die tatsächlichen Spotify-Kontoidentitäten und die verfügbaren Connect-Ziele.
Gleiche Konten, doppelte physische Ziele und ein dort bereits spielendes
anderes Profil werden nicht stillschweigend übernommen. Die Prüfungen
verwenden die vorhandenen Anmeldungen; sie erweitern keine OAuth-Rechte.

Ein angenommener Startauftrag ist noch kein Wiedergabenachweis. Der
Raumstatus kommt ausschließlich aus Spotify. Das offizielle Spotify-Embed
ist eine Vorschau im Browser, nicht der Nachweis einer Wiedergabe auf Alexa.
Bei fehlendem oder unklarem Status wird keine laufende Musik behauptet.

Die privaten Google-Anmeldungen, Add-on-Optionen und gespeicherten Spotify-
Tokens sind kein Bestandteil dieser Änderung und müssen beim Update
erhalten bleiben. Musiktests an echten Geräten erfordern einen gesonderten
Wiedergabeauftrag; automatisierte Tests verwenden ausschließlich Testdaten.
