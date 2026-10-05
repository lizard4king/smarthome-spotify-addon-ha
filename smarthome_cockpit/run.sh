#!/usr/bin/with-contenv bashio
set -euo pipefail
umask 077

export HOME_ASSISTANT_TOKEN="$(bashio::config 'home_assistant_token')"
export HOME_ASSISTANT_ALLOW_WRITES="$(bashio::config 'allow_writes')"
if [ -z "${HOME_ASSISTANT_ENTITIES_JSON:-}" ]; then
  export HOME_ASSISTANT_ENTITIES_JSON="$(bashio::config 'home_assistant_entities_json')"
fi
if [ -z "${HOME_ASSISTANT_WRITE_ALLOWLIST:-}" ]; then
  export HOME_ASSISTANT_WRITE_ALLOWLIST="$(bashio::config 'home_assistant_write_allowlist | join(",")')"
fi
export SPOTIFY_BRIDGE_SECRET="$(bashio::config 'bridge_secret')"
export CODEX_CONTROL_SECRET="$(bashio::config 'codex_control_secret')"
export CODEX_SPOTIFY_PROFILES="$(bashio::config 'codex_spotify_profiles | join(",")')"
export MUSIC_ASSISTANT_URL="$(bashio::config 'music_assistant_url')"
export MUSIC_ASSISTANT_TOKEN="$(bashio::config 'music_assistant_token')"
export MUSIC_ASSISTANT_ALLOW_PLAYBACK="$(bashio::config 'music_assistant_allow_playback')"
export MUSIC_ASSISTANT_LIBRARY_PROVIDER="$(bashio::config 'music_assistant_library_provider')"

bashio::log.info "Cockpit-Konfiguration: HA-Token=$([ -n "$HOME_ASSISTANT_TOKEN" ] && echo gesetzt || echo fehlt), Bridge-Secret=$([ -n "$SPOTIFY_BRIDGE_SECRET" ] && echo gesetzt || echo fehlt), Schreibzugriff=$HOME_ASSISTANT_ALLOW_WRITES"

exec python3 /app/addon_entrypoint.py
