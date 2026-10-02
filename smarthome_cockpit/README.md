# Smart Home Cockpit

Das Cockpit läuft als Home-Assistant-Add-on auf dem HA-Server. Tokens und
Secrets werden ausschließlich über die geschützte Add-on-Konfiguration gesetzt.
Der veröffentlichte Dienst ist Port `8767`; Cloudflared soll auf den
Home-Assistant-Host-Port `8767` zeigen. HAL9000 ist kein Produktivbestandteil.
