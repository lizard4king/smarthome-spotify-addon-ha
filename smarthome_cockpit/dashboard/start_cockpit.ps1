param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
Set-Location $ProjectRoot

Write-Host 'Smart Home Cockpit - lokale Zugangsdaten' -ForegroundColor Cyan
Write-Host 'Die Eingaben werden nicht angezeigt und nicht gespeichert.' -ForegroundColor DarkGray

Write-Host 'Kopiere jeweils den Wert im Browser und druecke im Prompt nur Enter.' -ForegroundColor Yellow
$null = Read-Host 'Home-Assistant Long-Lived Access Token kopiert? Enter'
$haToken = (Get-Clipboard -Raw).Trim()
$null = Read-Host 'Spotify Bridge Secret kopiert? Enter'
$bridgeSecret = (Get-Clipboard -Raw).Trim()

$env:HOME_ASSISTANT_URL = 'https://ha.pistelok.de'
$env:HOME_ASSISTANT_TOKEN = $haToken
$env:HOME_ASSISTANT_ALLOW_WRITES = 'false'
$env:SPOTIFY_BRIDGE_URL = 'http://homeassistant.local:8766'
$env:SPOTIFY_BRIDGE_SECRET = $bridgeSecret
$env:COCKPIT_HOST = '0.0.0.0'
$env:COCKPIT_PORT = '8767'

Write-Host 'Cockpit wird mit Live-Lesemodus gestartet.' -ForegroundColor Green
Write-Host 'Geräteschreibzugriffe bleiben bis zur separaten Freigabe deaktiviert.' -ForegroundColor Yellow
python dashboard/server.py
