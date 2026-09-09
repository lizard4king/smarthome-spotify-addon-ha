"""Home Assistant add-on entry point for the local Spotify bridge."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from smarthome.spotify_bridge import SpotifyBridge, make_server
from smarthome.spotify_command_service import SpotifyCommandService
from smarthome.spotify_connect import SpotifyConnectClient
from smarthome.spotify_oauth import SpotifyTokenManager
from smarthome.spotify_playback import SpotifyPlaybackClient
from smarthome.spotify_routing import load_spotify_profile_registry
from smarthome.spotify_search import SpotifyMediaSearchClient
from smarthome.spotify_file_store import SpotifyFileStore
from smarthome.spotify_targets import load_spotify_target_registry
from smarthome.spotify_transport import SpotifyTokenEndpoint


DATA = Path("/data")
CONFIG = Path("/config")


class TokenProvider:
    def __init__(self, client_id: str) -> None:
        store = SpotifyFileStore()
        endpoint = SpotifyTokenEndpoint(client_id)
        self._manager = SpotifyTokenManager(
            store, refresher=lambda token: endpoint.refresh(token)
        )

    def access_token(self, connection_id: str, *, now: datetime) -> str:
        return self._manager.access_token(connection_id, now=now)


def main() -> None:
    profiles = load_spotify_profile_registry(CONFIG / "spotify_profiles.json")
    targets = load_spotify_target_registry(CONFIG / "spotify_targets.json")
    token_provider = TokenProvider(os.environ["SPOTIFY_CLIENT_ID"])
    service = SpotifyCommandService(
        profiles,
        targets,
        token_provider,
        SpotifyMediaSearchClient(),
        SpotifyPlaybackClient(),
    )
    connect = SpotifyConnectClient()
    for profile in profiles.profiles.values():
        try:
            token = token_provider.access_token(
                profile.connection_id, now=datetime.now().astimezone()
            )
            catalog = connect.devices(token)
            names = ", ".join(device.name for device in catalog.devices) or "keine"
            print(f"Spotify-Geräte für {profile.display_name}: {names}", flush=True)
        except Exception as exc:
            print(
                f"Spotify-Geräte für {profile.display_name} konnten nicht geladen werden: {exc}",
                flush=True,
            )

    class Dispatcher:
        def dispatch(self, command, *, voice_identity, session, now):
            return service.play(
                command,
                voice_identity=voice_identity,
                session=session,
                now=now,
            )[0]

    server = make_server(SpotifyBridge(Dispatcher(), os.environ["SPOTIFY_BRIDGE_SECRET"]))
    server.serve_forever()


if __name__ == "__main__":
    main()
