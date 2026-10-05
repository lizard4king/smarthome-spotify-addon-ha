"""Home Assistant add-on entry point for the local Spotify bridge."""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

from smarthome.spotify_bridge import SpotifyBridge, make_server
from smarthome.spotify_command_service import SpotifyCommandService
from smarthome.spotify_connect import SpotifyConnectClient
from smarthome.spotify_oauth import SpotifyTokenManager
from smarthome.spotify_playback import SpotifyPlaybackClient
from smarthome.spotify_service import SpotifyPlaybackService
from smarthome.spotify_routing import load_spotify_profile_registry
from smarthome.spotify_search import SpotifyMediaSearchClient
from smarthome.spotify_file_store import SpotifyFileStore
from smarthome.spotify_targets import load_spotify_target_registry
from smarthome.spotify_transport import SpotifyTokenEndpoint
from smarthome.spotify_cockpit import SpotifyCockpitService


DATA = Path("/data")
CONFIG = Path("/config")


class TokenProvider:
    def __init__(self, client_id: str) -> None:
        store = SpotifyFileStore()
        endpoint = SpotifyTokenEndpoint(client_id, timeout_seconds=8)
        self._manager = SpotifyTokenManager(
            store, refresher=lambda token: endpoint.refresh(token)
        )
        self._lock = threading.RLock()

    def access_token(self, connection_id: str, *, now: datetime) -> str:
        with self._lock:
            return self._manager.access_token(connection_id, now=now)


def main() -> None:
    profiles = load_spotify_profile_registry(CONFIG / "spotify_profiles.json")
    targets = load_spotify_target_registry(CONFIG / "spotify_targets.json")
    token_provider = TokenProvider(os.environ["SPOTIFY_CLIENT_ID"])
    service = SpotifyCommandService(
        profiles,
        targets,
        token_provider,
        SpotifyMediaSearchClient(timeout_seconds=8),
        SpotifyPlaybackService(token_provider, SpotifyConnectClient(timeout_seconds=8), SpotifyPlaybackClient(timeout_seconds=8)),
    )
    cockpit = SpotifyCockpitService(profiles, targets, token_provider, service)

    class Dispatcher:
        def dispatch(self, command, *, voice_identity, session, now):
            return cockpit.play_command(
                command,
                voice_identity=voice_identity,
                session=session,
                now=now,
            )

        def search(self, profile_alias, query, *, now):
            return service.search(profile_alias, query, now=now)

        def status(self, *, now):
            return cockpit.status(now=now)

        def play_assignments(self, assignments, *, now):
            return cockpit.play(assignments, now=now)

        def control(self, payload, *, now):
            return cockpit.control(payload, now=now)

    # Bind inside the isolated add-on network so Cloudflared can proxy the
    # authenticated endpoint. The bridge is not exposed directly to the LAN.
    server = make_server(
        SpotifyBridge(Dispatcher(), os.environ["SPOTIFY_BRIDGE_SECRET"]),
        host="0.0.0.0",
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
