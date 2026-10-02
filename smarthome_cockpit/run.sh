#!/usr/bin/with-contenv bashio
set -euo pipefail
umask 077

export HOME_ASSISTANT_TOKEN="$(bashio::config 'home_assistant_token')"
export HOME_ASSISTANT_ALLOW_WRITES="$(bashio::config 'allow_writes')"
export SPOTIFY_BRIDGE_SECRET="$(bashio::config 'bridge_secret')"
export MUSIC_ASSISTANT_URL="$(bashio::config 'music_assistant_url')"
export MUSIC_ASSISTANT_TOKEN="$(bashio::config 'music_assistant_token')"
export MUSIC_ASSISTANT_ALLOW_PLAYBACK="$(bashio::config 'music_assistant_allow_playback')"

bashio::log.info "Cockpit-Konfiguration: HA-Token=$([ -n "$HOME_ASSISTANT_TOKEN" ] && echo gesetzt || echo fehlt), Bridge-Secret=$([ -n "$SPOTIFY_BRIDGE_SECRET" ] && echo gesetzt || echo fehlt), Schreibzugriff=$HOME_ASSISTANT_ALLOW_WRITES"

exec python3 /app/addon_entrypoint.py
