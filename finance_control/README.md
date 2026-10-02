# Finance Control Home-Assistant-Add-on

Das Add-on stellt Finance Control intern auf Port `8785` bereit. Die Datenbank
und das Profil liegen persistent unter `/data` (Home-Assistant-Add-on-Konfigurationsordner).

Cloudflared soll nach der Installation auf den Home-Assistant-Host-Port zeigen:

```text
finance.pistelok.de -> http://192.168.178.20:8785
```

Die Windows-/Tailscale-Route wird nicht mehr benötigt.
