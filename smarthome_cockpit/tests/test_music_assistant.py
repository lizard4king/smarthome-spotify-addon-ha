"""Offline REST-contract and security tests; no devices or real network."""
import io
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
from music_assistant import MusicAssistant, MusicAssistantError, MAX_JSON, _valid_uri

URI = "filesystem_local--abc://track/Artist/Album/song.flac"
TRACK = {"media_type": "track", "name": "Song", "artists": [{"name": "Artist"}],
         "album": {"name": "Album"}, "provider_mappings": [{"provider_domain": "filesystem_local",
         "provider_instance": "filesystem_local--abc", "item_id": "Artist/Album/song.flac", "available": True}],
         "metadata": {"images": [{"type": "thumb", "proxy_id": "a" * 64}]}}
PLAYER = {"player_id": "echo", "available": True, "supported_features": ["pause", "play_media"]}
QUEUE_ITEM = {"queue_item_id": "opaque", "name": "Artist - Song", "duration": 240, "media_item": TRACK}
QUEUE = {"queue_id": "echo", "active": True, "available": True, "items": 2, "current_index": 0,
         "state": "playing", "elapsed_time": 12, "current_item": QUEUE_ITEM}


class Response(io.BytesIO):
    def __init__(self, value, content_type="application/json", raw=False):
        super().__init__(value if raw else json.dumps(value).encode())
        self.headers = {"Content-Type": content_type}


class FakeNetwork:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, Response) else Response(response)


class MusicTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"MUSIC_ASSISTANT_URL": "http://ma.test:8095",
            "MUSIC_ASSISTANT_TOKEN": "private-test-token", "MUSIC_ASSISTANT_ALLOW_PLAYBACK": "true"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def client(self, *responses):
        client = MusicAssistant()
        client.opener = FakeNetwork(*responses)
        return client

    def test_absent_config_no_network(self):
        with patch.dict("os.environ", {}, clear=True):
            client = self.client()
        self.assertEqual(client.status()["configured"], False)
        self.assertEqual(client.status()["available"], False)
        self.assertEqual(client.opener.requests, [])

    def test_status_and_players_real_result(self):
        client = self.client([{"player_id": "echo", "name": "Office", "available": True}])
        self.assertEqual(client.status()["available"], True)
        req, timeout = client.opener.requests[0]
        self.assertEqual(req.full_url, "http://ma.test:8095/api")
        self.assertEqual(req.get_header("Authorization"), "Bearer private-test-token")
        self.assertEqual(json.loads(req.data)["command"], "players/all")
        self.assertEqual(timeout, 8)

    def test_case_sensitive_filesystem_instance_uri(self):
        uri = "filesystem_smb--GyceRM3q://track/Interpret/Album/Titel.flac"
        self.assertTrue(_valid_uri(uri))
        self.assertTrue(_valid_uri(uri.replace("filesystem_smb", "filesystem_nfs")))
        self.assertTrue(_valid_uri(uri.replace("filesystem_smb", "filesystem_local")))
        self.assertFalse(_valid_uri(uri.replace("filesystem_smb", "FILESYSTEM_SMB")))
        self.assertFalse(_valid_uri(uri.replace("filesystem_smb", "spotify")))

    def smb_track(self):
        uri = "filesystem_smb--GyceRM3q://track/Interpret/Album/Titel.flac"
        mapping = {"provider_domain": "filesystem_smb", "provider_instance": "filesystem_smb--GyceRM3q",
                   "item_id": "Interpret/Album/Titel.flac", "available": True}
        return uri, dict(TRACK, provider_mappings=[mapping])

    def test_uppercase_instance_tracks_and_artwork_roundtrip(self):
        uri, track = self.smb_track()
        result = self.client([track]).tracks()
        self.assertEqual(result["tracks"][0]["uri"], uri)
        artwork_url = result["tracks"][0]["artwork_url"]
        self.assertEqual(urlsplit(artwork_url).path, "/api/music/artwork")
        artwork_uri = parse_qs(urlsplit(artwork_url).query)["uri"][0]
        self.assertEqual(artwork_uri, uri)
        client = self.client(track, Response(b"png", "image/png", raw=True))
        self.assertEqual(client.artwork(artwork_uri), (b"png", "image/png"))
        self.assertEqual(json.loads(client.opener.requests[0][0].data)["args"]["uri"], uri)
        self.assertEqual(client.opener.requests[1][0].full_url,
                         "http://ma.test:8095/imageproxy/" + "a" * 64 + "?size=256&fmt=png")

    def test_uppercase_instance_play_preserves_exact_case(self):
        uri, track = self.smb_track()
        client = self.client(track, [{"player_id": "echo", "available": True}], {"queue_id": "echo"}, None)
        self.assertEqual(client.play(uri, "echo"), {"status": "ok"})
        self.assertEqual(json.loads(client.opener.requests[0][0].data)["args"]["uri"], uri)
        self.assertEqual(json.loads(client.opener.requests[-1][0].data)["args"]["media"], uri)
        # Changing the case identifies a different provider and must fail its mapping check.
        client = self.client(track)
        with self.assertRaises(ValueError):
            client.play(uri.replace("GyceRM3q", "gycerm3q"), "echo")
        self.assertEqual(len(client.opener.requests), 1)

    def test_tracks_filter_sources_and_pagination(self):
        cloud = dict(TRACK, provider_mappings=[{"provider_domain": "spotify"}])
        client = self.client([TRACK] + [cloud] * 49)
        result = client.tracks('Artist "quoted"', 50)
        self.assertEqual(len(result["tracks"]), 1)
        self.assertEqual(result["tracks"][0]["uri"], URI)
        self.assertEqual(result["next_offset"], 100)
        self.assertTrue(result["tracks"][0]["artwork_url"].startswith("/api/music/artwork?uri="))
        args = json.loads(client.opener.requests[0][0].data)["args"]
        self.assertEqual(args["search"], 'Artist "quoted"')
        self.assertFalse(args["summary"])

    def test_play_uses_verified_local_mapping_and_active_queue(self):
        client = self.client(TRACK, [{"player_id": "echo", "available": True}], {"queue_id": "group"}, None)
        self.assertEqual(client.play("library://track/12", "echo"), {"status": "ok"})
        command = json.loads(client.opener.requests[-1][0].data)
        self.assertEqual(command["command"], "player_queues/play_media")
        self.assertEqual(command["args"], {"queue_id": "group", "media": URI, "option": "replace"})

    def test_controls(self):
        for command in ("pause", "resume", "stop"):
            queue = dict(QUEUE, state="paused" if command == "resume" else "playing")
            client = self.client([PLAYER], queue, [QUEUE_ITEM, QUEUE_ITEM], None)
            self.assertEqual(client.control("echo", command), {"status": "ok"})
            self.assertEqual(json.loads(client.opener.requests[-1][0].data)["command"], "player_queues/" + command)

    def test_queue_preserves_selected_player_and_uses_group_features(self):
        group = dict(PLAYER, player_id="group", supported_features=["seek", "pause"])
        client = self.client([PLAYER, group], dict(QUEUE, queue_id="group"), [QUEUE_ITEM, QUEUE_ITEM])
        result = client.queue("echo")
        self.assertEqual(result["player_id"], "echo")
        self.assertEqual(result["queue_id"], "group")
        self.assertTrue(result["own_music"])
        self.assertTrue(result["controls"]["seek"])
        self.assertTrue(all(isinstance(v, bool) for v in result["controls"].values()))
        self.assertEqual(result["current_track"]["uri"], URI)
        self.assertEqual([t["index"] for t in result["tracks"]], [0, 1])
        self.assertNotIn("media_item", json.dumps(result))

    def test_external_source_neutral_and_controls_never_write(self):
        client = self.client([PLAYER], None)
        result = client.queue("echo")
        self.assertIsNone(result["queue_id"])
        self.assertFalse(result["own_music"])
        self.assertFalse(any(result["controls"].values()))
        for command in ("pause", "resume", "stop", "next", "previous"):
            client = self.client([PLAYER], None)
            with self.assertRaises(ValueError):
                client.control("echo", command)
            self.assertEqual(len(client.opener.requests), 2)

    def test_foreign_queue_items_block_controls(self):
        foreign = dict(QUEUE_ITEM, media_item=dict(TRACK, provider_mappings=[{"provider_domain": "spotify"}]))
        for queue, items in ((dict(QUEUE, current_item=foreign), [foreign, foreign]),
                             (QUEUE, [QUEUE_ITEM, foreign])):
            client = self.client([PLAYER], queue, items)
            result = client.queue("echo")
            self.assertFalse(result["own_music"])
            self.assertFalse(any(result["controls"].values()))
            self.assertNotIn("spotify", json.dumps(result))

    def test_mixed_mapping_does_not_mislabel_spotify_stream_as_own_music(self):
        mixed = dict(TRACK, provider="library", provider_mappings=TRACK["provider_mappings"] + [
            {"provider_domain": "spotify", "provider_instance": "spotify--abc", "item_id": "123"}])
        for stream in ({"provider": "spotify--abc"}, None):
            item = dict(QUEUE_ITEM, media_item=mixed, streamdetails=stream)
            client = self.client([PLAYER], dict(QUEUE, current_item=item), [item, item])
            self.assertFalse(client.queue("echo")["own_music"])
        item = dict(QUEUE_ITEM, media_item=mixed, streamdetails={"provider": "filesystem_local--abc"})
        client = self.client([PLAYER], dict(QUEUE, current_item=item), [item, item])
        self.assertTrue(client.queue("echo")["own_music"])

    def test_queue_disabled_playback_preserves_read_status(self):
        with patch.dict("os.environ", {"MUSIC_ASSISTANT_ALLOW_PLAYBACK": "false"}):
            client = self.client([PLAYER], QUEUE, [QUEUE_ITEM, QUEUE_ITEM])
        result = client.queue("echo")
        self.assertTrue(result["own_music"])
        self.assertFalse(any(result["controls"].values()))

    def test_unknown_queue_state_never_enables_controls(self):
        for state in (None, "buffering", [], {"unexpected": True}):
            client = self.client([PLAYER], dict(QUEUE, state=state), [QUEUE_ITEM, QUEUE_ITEM])
            result = client.queue("echo")
            self.assertEqual(result["state"], "unknown")
            self.assertFalse(any(result["controls"].values()))

    def test_stopped_own_queue_can_resume_without_external_fallback(self):
        client = self.client([PLAYER], dict(QUEUE, active=False, state="idle"), [QUEUE_ITEM, QUEUE_ITEM])
        result = client.queue("echo")
        self.assertFalse(result["active"])
        self.assertTrue(result["controls"]["resume"])
        self.assertFalse(any(value for key, value in result["controls"].items() if key != "resume"))

    def test_queue_bounded_window_and_safe_metadata(self):
        poisoned = dict(QUEUE_ITEM, streamdetails={"path": "http://private", "token": "secret"})
        client = self.client([PLAYER], dict(QUEUE, items=200, current_index=80), [poisoned] * 60)
        result = client.queue("echo")
        self.assertEqual(len(result["tracks"]), 50)
        self.assertEqual(result["tracks"][0]["index"], 70)
        self.assertEqual(result["next_offset"], 120)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(json.loads(client.opener.requests[-1][0].data)["args"],
                         {"queue_id": "echo", "limit": 50, "offset": 70})

    def test_queue_seek_requires_player_feature_and_valid_duration(self):
        client = self.client([PLAYER], QUEUE, [QUEUE_ITEM, QUEUE_ITEM])
        self.assertFalse(client.queue("echo")["controls"]["seek"])
        for position in (-1, True, 1.5, None):
            client = self.client()
            with self.assertRaises(ValueError):
                client.control("echo", "seek", position)
            self.assertFalse(client.opener.requests)
        seek_player = dict(PLAYER, supported_features=["seek", "pause"])
        client = self.client([seek_player], QUEUE, [QUEUE_ITEM, QUEUE_ITEM], None)
        self.assertEqual(client.control("echo", "seek", 120), {"status": "ok"})
        self.assertEqual(json.loads(client.opener.requests[-1][0].data)["args"], {"queue_id": "echo", "position": 120})
        client = self.client([seek_player], QUEUE, [QUEUE_ITEM, QUEUE_ITEM])
        with self.assertRaises(ValueError):
            client.control("echo", "seek", 241)
        self.assertEqual(len(client.opener.requests), 3)

    def test_queue_navigation_checks_adjacent_items(self):
        client = self.client([PLAYER], dict(QUEUE, elapsed_time=0), [QUEUE_ITEM, QUEUE_ITEM])
        result = client.queue("echo")
        self.assertFalse(result["controls"]["previous"])
        self.assertTrue(result["controls"]["next"])
        client = self.client([PLAYER], dict(QUEUE, items=1, elapsed_time=0), [QUEUE_ITEM])
        self.assertFalse(client.queue("echo")["controls"]["next"])
        for command in ("next", "previous"):
            client = self.client([PLAYER], QUEUE, [QUEUE_ITEM, QUEUE_ITEM], None)
            self.assertEqual(client.control("echo", command), {"status": "ok"})
            self.assertEqual(json.loads(client.opener.requests[-1][0].data)["command"], "player_queues/" + command)

    def test_explicit_play_falls_back_to_verified_group_queue(self):
        child = dict(PLAYER, active_group="group")
        group = dict(PLAYER, player_id="group")
        client = self.client(TRACK, [child, group], None, {"queue_id": "group", "available": True}, None)
        self.assertEqual(client.play(URI, "echo", "add"), {"status": "ok"})
        calls = [json.loads(req.data) for req, _ in client.opener.requests]
        self.assertEqual(calls[-2]["command"], "player_queues/get")
        self.assertEqual(calls[-2]["args"], {"queue_id": "group"})
        self.assertEqual(calls[-1]["args"], {"queue_id": "group", "media": URI, "option": "add"})
        client = self.client(TRACK, [child], None)
        with self.assertRaises(MusicAssistantError):
            client.play(URI, "echo")
        self.assertEqual(len(client.opener.requests), 3)

    def test_invalid_queue_option_rejected_before_network(self):
        for option in ("next", [], None):
            client = self.client()
            with self.assertRaises(ValueError):
                client.play(URI, "echo", option)
            self.assertFalse(client.opener.requests)

    def test_configured_provider_selects_mapping_and_filters_query(self):
        provider = "filesystem_local--NewCopy"
        local_uri = provider + "://track/new/song.flac"
        local_mapping = dict(TRACK["provider_mappings"][0], provider_instance=provider, item_id="new/song.flac")
        track = dict(TRACK, provider_mappings=TRACK["provider_mappings"] + [local_mapping])
        with patch.dict("os.environ", {"MUSIC_ASSISTANT_LIBRARY_PROVIDER": provider}):
            client = self.client([track])
        self.assertEqual(client.tracks("Enya")["tracks"][0]["uri"], local_uri)
        self.assertEqual(json.loads(client.opener.requests[0][0].data)["args"]["provider"], provider)
        with patch.dict("os.environ", {"MUSIC_ASSISTANT_LIBRARY_PROVIDER": provider}):
            client = self.client(track, [PLAYER], QUEUE, None)
        client.play("library://track/12", "echo")
        self.assertEqual(json.loads(client.opener.requests[-1][0].data)["args"]["media"], local_uri)
        with patch.dict("os.environ", {"MUSIC_ASSISTANT_LIBRARY_PROVIDER": provider}):
            client = self.client(track)
        with self.assertRaises(ValueError):
            client.play(URI, "echo")

    def test_invalid_library_provider_disables_network(self):
        for provider in ("spotify--copy", "filesystem_local--bad/url", "FILESYSTEM_LOCAL"):
            with patch.dict("os.environ", {"MUSIC_ASSISTANT_LIBRARY_PROVIDER": provider}):
                client = self.client()
            self.assertFalse(client.status()["available"])
            self.assertFalse(client.opener.requests)

    def test_playback_gate(self):
        with patch.dict("os.environ", {"MUSIC_ASSISTANT_ALLOW_PLAYBACK": "false"}):
            client = self.client()
        with self.assertRaises(MusicAssistantError):
            client.play(URI, "echo")
        self.assertFalse(client.opener.requests)

    def test_invalid_player_no_command(self):
        for player in ("unknown", "echo", "echo\ncommand"):
            client = self.client([{"player_id": "echo", "available": False}])
            with self.assertRaises(ValueError):
                client.control(player, "pause")
            self.assertEqual(len(client.opener.requests), 1)

    def test_injection_and_raw_paths_rejected_before_network(self):
        bad = ["http://127.0.0.1/private", "file:///etc/passwd", "C:\\music\\song.mp3",
               "filesystem_local--abc://track/../secret", "filesystem_local--abc://track/%2e%2e/secret",
               "filesystem_local--abc://track/%2Fetc/passwd", "library://track/12?url=http://evil",
               'library://track/1\ncommand', "spotify://track/12"]
        for uri in bad:
            self.assertFalse(_valid_uri(uri), uri)
            client = self.client()
            with self.assertRaises(ValueError):
                client.play(uri, "echo")
            self.assertFalse(client.opener.requests)

    def test_unverified_track_mapping_rejected(self):
        for track in ({"media_type": "track", "provider_mappings": []}, dict(TRACK, media_type="radio")):
            client = self.client(track)
            with self.assertRaises(ValueError):
                client.play(URI, "echo")
            self.assertEqual(len(client.opener.requests), 1)

    def test_remote_errors_and_secret_redaction(self):
        for response in (OSError("private-test-token http://ma.test"), Response(b"broken", raw=True), {"error": "private-test-token"}):
            client = self.client(response)
            result = client.status()
            self.assertFalse(result["available"])
            self.assertNotIn("private-test-token", json.dumps(result))

    def test_oversized_response(self):
        client = self.client(Response(b"x" * (MAX_JSON + 1), raw=True))
        self.assertFalse(client.status()["available"])

    def test_deep_json_response_fails_safely_without_output(self):
        raw = b"[" * 2000 + b'"private-test-token"' + b"]" * 2000
        self.assertLess(len(raw), MAX_JSON)
        with patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO) as errors:
            for operation in ("status", "tracks", "artwork"):
                with self.subTest(operation=operation):
                    client = self.client(Response(raw, raw=True))
                    with patch("music_assistant.json.loads", side_effect=RecursionError("private-test-token")):
                        if operation == "status":
                            result = client.status()
                            self.assertFalse(result["available"])
                            self.assertNotIn("private-test-token", json.dumps(result))
                        else:
                            with self.assertRaises(MusicAssistantError) as caught:
                                client.tracks() if operation == "tracks" else client.artwork(URI)
                            self.assertNotIn("private-test-token", str(caught.exception))
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(errors.getvalue(), "")

    def test_real_deep_json_with_controlled_recursion_limit(self):
        # CPython 3.12's C decoder also has a limit independent of the Python limit.
        raw = b"[" * 10000 + b"0" + b"]" * 10000
        for operation in ("status", "tracks", "artwork"):
            with self.subTest(operation=operation):
                client = self.client(Response(raw, raw=True))
                original_limit = sys.getrecursionlimit()
                try:
                    sys.setrecursionlimit(1000)
                    with self.assertRaises(RecursionError):
                        json.loads(raw)
                    if operation == "status":
                        self.assertFalse(client.status()["available"])
                    else:
                        with self.assertRaises(MusicAssistantError):
                            client.tracks() if operation == "tracks" else client.artwork(URI)
                finally:
                    sys.setrecursionlimit(original_limit)

    def test_invalid_images_types_keep_tracks_without_artwork(self):
        for images in (None, 17, "private-test-token", {"type": "thumb", "proxy_id": "a" * 64}):
            with self.subTest(images_type=type(images).__name__):
                track = dict(TRACK, metadata={"images": images})
                with patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO) as errors:
                    result = self.client([track]).tracks()
                    self.assertEqual(len(result["tracks"]), 1)
                    self.assertNotIn("artwork_url", result["tracks"][0])
                    client = self.client(track)
                    with self.assertRaises(MusicAssistantError) as caught:
                        client.artwork(URI)
                    self.assertNotIn("private-test-token", str(caught.exception))
                    self.assertEqual(len(client.opener.requests), 1)
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(errors.getvalue(), "")

    def test_artwork_only_fixed_proxy_and_image_type(self):
        client = self.client(TRACK, Response(b"png", "image/png", raw=True))
        self.assertEqual(client.artwork(URI), (b"png", "image/png"))
        self.assertEqual(client.opener.requests[1][0].full_url, "http://ma.test:8095/imageproxy/" + "a" * 64 + "?size=256&fmt=png")
        remote = dict(TRACK, metadata={"images": [{"type": "thumb", "provider": "builtin", "path": "http://evil/secret"}]})
        client = self.client(remote)
        with self.assertRaises(MusicAssistantError):
            client.artwork(URI)
        self.assertEqual(len(client.opener.requests), 1)
        client = self.client(TRACK, Response(b"<svg/>", "image/svg+xml", raw=True))
        with self.assertRaises(MusicAssistantError):
            client.artwork(URI)

    def test_search_offset_validation(self):
        for q, offset in (("x\n", 0), ("x", -1), ("x", True), ("x", 100001)):
            with self.assertRaises(ValueError):
                self.client().tracks(q, offset)

    def test_invalid_configuration_and_control(self):
        for url in ("http://[invalid", "http://user:pass@ma.test", "http://ma.test/api?token=secret"):
            with patch.dict("os.environ", {"MUSIC_ASSISTANT_URL": url}):
                client = self.client()
            self.assertFalse(client.status()["available"])
            self.assertFalse(client.opener.requests)
        with self.assertRaises(ValueError):
            self.client().control("echo", ["pause"])

    def test_legacy_artwork_path_validation(self):
        image = {"type": "thumb", "provider": "filesystem_local--abc", "path": "Artist/cover.jpg"}
        self.assertTrue(MusicAssistant._image(dict(TRACK, metadata={"images": [image]})).startswith("/imageproxy?"))
        for path in ("../secret", "http://evil", "/etc/passwd", "%2e%2e/secret"):
            self.assertIsNone(MusicAssistant._image(dict(TRACK, metadata={"images": [dict(image, path=path)]})))


class RouteTests(unittest.TestCase):
    def handler(self, path):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from dashboard.server import CockpitHandler
        handler = object.__new__(CockpitHandler)
        handler.path = path
        handler.headers = {"Host": "cockpit.test", "Content-Type": "application/json"}
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        return handler

    def test_music_json_no_cors_existing_json_unchanged(self):
        for path, cors in (("/api/music/status", False), ("/api/capabilities", True)):
            handler = self.handler(path)
            handler._send_json(200, {})
            actual = any(c.args[0] == "Access-Control-Allow-Origin" for c in handler.send_header.call_args_list)
            self.assertEqual(actual, cors)

    def test_cross_origin_write_and_preflight_denied(self):
        handler = self.handler("/api/music/play")
        handler.headers["Origin"] = "http://evil.test"
        with patch("dashboard.server.MusicAssistant") as client:
            handler._handle_music_post({"uri": URI, "player_id": "echo"})
            handler.send_response.assert_called_with(403)
            client.assert_not_called()
        handler.do_OPTIONS()
        handler.send_response.assert_called_with(405)
        self.assertFalse(any(c.args[0] == "Access-Control-Allow-Origin" for c in handler.send_header.call_args_list))

    def test_status_and_play_routes(self):
        handler = self.handler("/api/music/status")
        with patch("dashboard.server.MusicAssistant") as client:
            client.return_value.status.return_value = {"configured": False, "available": False}
            handler.do_GET()
            self.assertFalse(json.loads(handler.wfile.getvalue())["available"])
        handler = self.handler("/api/music/play")
        with patch("dashboard.server.MusicAssistant") as client:
            client.return_value.play.return_value = {"status": "ok"}
            handler._handle_music_post({"uri": URI, "player_id": "echo"})
            client.return_value.play.assert_called_once_with(URI, "echo")
            handler.send_response.assert_called_with(200)

    def test_queue_and_add_and_seek_routes(self):
        handler = self.handler("/api/music/queue?player_id=echo")
        with patch("dashboard.server.MusicAssistant") as client:
            client.return_value.queue.return_value = {"player_id": "echo", "queue_id": "group", "controls": {}}
            handler.do_GET()
            client.return_value.queue.assert_called_once_with("echo")
            self.assertEqual(json.loads(handler.wfile.getvalue())["queue_id"], "group")
        handler = self.handler("/api/music/play")
        with patch("dashboard.server.MusicAssistant") as client:
            client.return_value.play.return_value = {"status": "ok"}
            handler._handle_music_post({"uri": URI, "player_id": "echo", "option": "add"})
            client.return_value.play.assert_called_once_with(URI, "echo", "add")
        handler = self.handler("/api/music/control")
        with patch("dashboard.server.MusicAssistant") as client:
            client.return_value.control.return_value = {"status": "ok"}
            handler._handle_music_post({"player_id": "echo", "command": "seek", "position": 120})
            client.return_value.control.assert_called_once_with("echo", "seek", 120)

    def test_music_cross_site_without_origin_is_rejected(self):
        handler = self.handler("/api/music/control")
        handler.headers["Sec-Fetch-Site"] = "cross-site"
        with patch("dashboard.server.MusicAssistant") as client:
            handler._handle_music_post({"player_id": "echo", "command": "pause"})
            handler.send_response.assert_called_with(403)
            client.assert_not_called()

    def test_music_reads_reject_cross_site_before_client(self):
        paths = ("/api/music/queue?player_id=echo", "/api/music/artwork?uri=" + URI,
                 "/api/music/tracks?q=enya")
        rejected_headers = ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://evil.test"},
                            {"Origin": "null"}, {"Origin": "https://cockpit.test.evil"})
        for path in paths:
            for extra_headers in rejected_headers:
                with self.subTest(path=path, headers=extra_headers):
                    handler = self.handler(path)
                    handler.headers.update(extra_headers)
                    with patch("dashboard.server.MusicAssistant") as client:
                        handler.do_GET()
                        handler.send_response.assert_called_with(403)
                        client.assert_not_called()

    def test_music_reads_accept_same_origin(self):
        for path in ("/api/music/queue?player_id=echo", "/api/music/artwork?uri=" + URI,
                     "/api/music/tracks?q=enya"):
            with self.subTest(path=path):
                handler = self.handler(path)
                handler.headers.update({"Origin": "http://cockpit.test", "Sec-Fetch-Site": "same-origin"})
                with patch("dashboard.server.MusicAssistant") as client:
                    client.return_value.queue.return_value = {"state": "idle"}
                    client.return_value.tracks.return_value = {"tracks": []}
                    client.return_value.artwork.return_value = (b"png", "image/png")
                    handler.do_GET()
                    handler.send_response.assert_called_with(200)
                    client.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
