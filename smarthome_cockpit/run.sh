#!/usr/bin/with-contenv bashio
set -euo pipefail
umask 077

export HOME_ASSISTANT_TOKEN="$(bashio::config 'home_assistant_token')"
export HOME_ASSISTANT_ALLOW_WRITES="$(bashio::config 'allow_writes')"
export SPOTIFY_BRIDGE_SECRET="$(bashio::config 'bridge_secret')"

exec python3 /app/addon_entrypoint.py
