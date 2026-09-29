#!/usr/bin/with-contenv bashio
set -euo pipefail

if ! bashio::config.has_value 'bridge_secret' || ! bashio::config.has_value 'spotify_client_id'; then
  bashio::log.error 'bridge_secret und spotify_client_id müssen im Add-on gesetzt werden.'
  exit 1
fi

export SPOTIFY_BRIDGE_SECRET="$(bashio::config 'bridge_secret')"
export SPOTIFY_CLIENT_ID="$(bashio::config 'spotify_client_id')"

# OAuth token files are staged through the shared Home Assistant config volume
# and copied into the add-on's persistent data volume at startup.
TOKEN_SOURCE='/config/spotify-tokens'
TOKEN_TARGET='/data/spotify-tokens'
mkdir -p "$TOKEN_TARGET"
if [ -d "$TOKEN_SOURCE" ]; then
  find "$TOKEN_SOURCE" -maxdepth 1 -type f -name '*.json' -exec cp -f {} "$TOKEN_TARGET/" \;
  find "$TOKEN_TARGET" -maxdepth 1 -type f -name '*.json' -exec chmod 600 {} \;
fi

exec python3 /app/addon_entrypoint.py
