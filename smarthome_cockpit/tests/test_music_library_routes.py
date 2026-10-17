"""Synthetic route contracts for the local music library and browser audio."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.server import CockpitHandler
from dashboard.music_assistant import MusicAssistantError

PROVIDER = "filesystem_local--abc"
URI = PROVIDER + "://track/Artist/Album/song.flac"


def handler(path, **headers):
    value = object.__new__(CockpitHandler)
    value.path = path
    value.headers = {"Host": "cockpit.test", "Content-Type": "application/json", **headers}
    value.wfile = io.BytesIO()
    value.send_response = Mock()
    value.send_header = Mock()
    value.end_headers = Mock()
    return value


def sent_header(request, name):
    return next(call.args[1] for call in request.send_header.call_args_list if call.args[0] == name)


class MusicLibraryRouteTests(unittest.TestCase):
    def test_album_folder_and_collection_parameters_are_forwarded(self):
        routes = [
            ("/api/music/albums?q=Joni&offset=25", "albums", ("Joni", 25)),
            ("/api/music/folders?path=Artist%2FAlbum&q=Blue&offset=50", "folders",
             ("Artist/Album", "Blue", 50)),
            ("/api/music/collection?uri=" + URI, "collection", (URI,)),
        ]
        for path, method, expected in routes:
            with self.subTest(path=path):
                request = handler(path)
                with patch("dashboard.server.MusicAssistant") as client, \
                     patch("dashboard.server.MusicLibrary") as library:
                    getattr(library.return_value, method).return_value = {"items": []}
                    request.do_GET()
                library.assert_called_once_with(client.return_value)
                getattr(library.return_value, method).assert_called_once_with(*expected)
                request.send_response.assert_called_with(200)

    def test_invalid_offsets_return_400(self):
        for path in ("/api/music/albums?offset=bad", "/api/music/folders?offset=bad"):
            with self.subTest(path=path):
                request = handler(path)
                with patch("dashboard.server.MusicAssistant"), \
                     patch("dashboard.server.MusicLibrary") as library:
                    request.do_GET()
                request.send_response.assert_called_with(400)
                library.return_value.albums.assert_not_called()
                library.return_value.folders.assert_not_called()
                self.assertIn("error", json.loads(request.wfile.getvalue()))

    def test_audio_head_forwards_range_and_writes_no_body(self):
        request = handler("/api/music/audio?uri=" + URI, Range="bytes=2-8")
        with patch("dashboard.server.MusicAssistant") as client, \
             patch("dashboard.server.serve_audio") as serve:
            request.do_HEAD()
        serve.assert_called_once_with(request, client.return_value, URI, head=True)
        self.assertEqual(request.wfile.getvalue(), b"")

    def test_browser_status_does_not_echo_credentials(self):
        request = handler("/api/music/browser-status")
        secret = "synthetic-private-token"
        with patch("dashboard.server.MusicAssistant") as client:
            client.return_value.url = "https://ma.synthetic.test"
            client.return_value.token = secret
            with patch("dashboard.server.BrowserAudio") as browser:
                browser.return_value.status.return_value = {"available": False}
                request.do_GET()
        body = request.wfile.getvalue().decode("utf-8")
        self.assertEqual(json.loads(body), {"available": False})
        self.assertNotIn(secret, body)
        self.assertNotIn("ma.synthetic.test", body)

    def test_cross_site_is_rejected_on_all_five_library_routes_before_client(self):
        paths = ["/api/music/albums", "/api/music/folders", "/api/music/collection",
                 "/api/music/audio?uri=" + URI, "/api/music/browser-status"]
        for path in paths:
            with self.subTest(path=path):
                request = handler(path, **{"Sec-Fetch-Site": "cross-site"})
                with patch("dashboard.server.MusicAssistant") as client:
                    request.do_GET()
                request.send_response.assert_called_with(403)
                client.assert_not_called()

    def test_malformed_origin_is_rejected_before_client_for_get_and_post(self):
        get_request = handler("/api/music/browser-status", Origin="http://[")
        with patch("dashboard.server.MusicAssistant") as client:
            get_request.do_GET()
        get_request.send_response.assert_called_with(403)
        client.assert_not_called()

        post_request = handler("/api/music/play", Origin="http://[")
        with patch("dashboard.server.MusicAssistant") as client:
            post_request._handle_music_post({"uri": URI, "player_id": "synthetic"})
        post_request.send_response.assert_called_with(403)
        client.assert_not_called()

    def test_head_json_errors_have_content_length_and_no_body(self):
        cases = [
            ("/api/music/audio?uri=" + URI, {}, 400,
             {"error": "synthetic invalid request"}, ValueError("synthetic invalid request")),
            ("/api/music/browser-status", {"Origin": "http://evil.test"}, 403,
             {"available": False, "error": "Cross-Origin-Musikzugriff ist nicht freigegeben."}, None),
            ("/api/music/albums", {}, 405,
             {"error": "HEAD ist nur für Audio verfügbar."}, None),
            ("/api/music/audio?uri=" + URI, {}, 503,
             {"available": False, "error": "synthetic offline"}, MusicAssistantError("synthetic offline")),
        ]
        for path, headers, status, payload, route_error in cases:
            with self.subTest(status=status):
                request = handler(path, **headers)
                request.command = "HEAD"
                with patch("dashboard.server.MusicAssistant") as client, \
                     patch("dashboard.server.serve_audio", side_effect=route_error) as serve:
                    request.do_HEAD()
                request.send_response.assert_called_with(status)
                encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
                self.assertEqual(sent_header(request, "Content-Length"), str(len(encoded)))
                self.assertEqual(request.wfile.getvalue(), b"")
                if status in (400, 503):
                    serve.assert_called_once()
                if status == 403:
                    client.assert_not_called()

    def test_malformed_origin_is_rejected_before_spotify_status(self):
        request = handler("/api/spotify/status", Origin="http://[")
        with patch("dashboard.server.SpotifyCockpit") as spotify:
            request.do_GET()
        request.send_response.assert_called_with(403)
        spotify.assert_not_called()

    def test_malformed_origin_control_is_rejected_before_read_or_spotify(self):
        request = handler("/api/spotify/control", Origin="http://[", **{"Content-Length": "2"})
        request.rfile = Mock()
        request.rfile.read = Mock(return_value=b"{}")
        with patch("dashboard.server.SpotifyCockpit") as spotify:
            request.do_POST()
        request.send_response.assert_called_with(403)
        spotify.assert_not_called()
        request.rfile.read.assert_not_called()
        self.assertEqual(json.loads(request.wfile.getvalue()), {
            "error": "Cross-Origin-Spotifyzugriff ist nicht freigegeben.", "outcome": "not_sent"})

class AddonOptionsTests(unittest.TestCase):
    def test_optional_browser_options_are_forwarded_to_environment(self):
        from smarthome_cockpit import addon_entrypoint

        options = {"music_browser_root": "/media/music", "music_browser_allow_playback": True}
        with patch.dict("os.environ", {}, clear=True), \
             patch("builtins.open", return_value=io.StringIO(json.dumps(options))), \
             patch.object(addon_entrypoint, "_load_private_music_fallback"):
            addon_entrypoint._load_options_fallback()
            import os
            self.assertEqual(os.environ["MUSIC_BROWSER_ROOT"], "/media/music")
            self.assertEqual(os.environ["MUSIC_BROWSER_ALLOW_PLAYBACK"], "true")


if __name__ == "__main__":
    unittest.main()
