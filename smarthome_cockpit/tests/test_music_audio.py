"""Synthetic browser-audio security and HTTP contract tests; no network/devices."""
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import music_audio
from dashboard.music_assistant import MusicAssistantError

PROVIDER = "filesystem_local--AbC"
URI = PROVIDER + "://track/Artist/Album/song.flac"


class Client:
    library_provider = PROVIDER
    configured = True
    valid_config = True

    def resolve_local_track(self, uri):
        return {}, uri


class Handler:
    def __init__(self, range_value=None):
        self.headers = {} if range_value is None else {"Range": range_value}
        self.status = None
        self.response_headers = {}
        self.wfile = io.BytesIO()
        self.close_connection = False
        self.responses = []

    def send_response(self, status):
        self.status = status
        self.responses.append(status)

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


class ReadSpy(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class FaultStream:
    def __init__(self, failure):
        self.failure = failure
        self.read_count = 0
        self.closed = False
        self.position = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self):
        self.closed = True
        if self.failure == "close":
            raise OSError("synthetic close failure")

    def seek(self, position):
        self.position = position
        if self.failure == "seek":
            raise OSError("synthetic seek failure")

    def read(self, size):
        self.read_count += 1
        if self.failure == "read":
            if self.read_count == 1:
                return b"x"
            raise OSError("synthetic read failure")
        if self.failure == "short":
            return b""
        return b"audio"[:size]


class AudioTests(unittest.TestCase):
    def test_root_parts_validates_absolute_posix_roots(self):
        self.assertEqual(music_audio.root_parts("/srv/music"), ("srv", "music"))
        for invalid in ("", "music", "C:\\music", "//srv/music", "/", "/srv/../music",
                        "/srv/./music", "/srv\\music"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                music_audio.root_parts(invalid)

    def test_byte_ranges(self):
        cases = [(None, (0, 9, 200)), ("bytes=2-5", (2, 5, 206)),
                 ("bytes=7-", (7, 9, 206)), ("bytes=-3", (7, 9, 206)),
                 ("bytes=-20", (0, 9, 206)), ("bytes=9-99", (9, 9, 206))]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(music_audio.byte_range(value, 10), expected)

    def test_invalid_byte_ranges(self):
        invalid = ["", "items=0-1", "bytes=-", "bytes=0-1,3-4", "bytes=10-",
                   "bytes=5-4", "bytes=-0", "bytes=0-1\n", "bytes=" + "1" * 129]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(music_audio.AudioRangeError):
                music_audio.byte_range(value, 10)
        with self.assertRaises(music_audio.AudioRangeError):
            music_audio.byte_range("bytes=0-", 0)

    def test_streaming_range_headers_and_body(self):
        stream = ReadSpy(b"0123456789")
        handler = Handler("bytes=2-5")
        with patch.object(music_audio.BrowserAudio, "open_track", return_value=(stream, 10, "audio/flac")):
            music_audio.serve_audio(handler, Client(), URI)
        self.assertEqual(handler.status, 206)
        self.assertEqual(handler.wfile.getvalue(), b"2345")
        self.assertEqual(handler.response_headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(handler.response_headers["Content-Length"], "4")
        self.assertEqual(handler.response_headers["Content-Type"], "audio/flac")
        self.assertEqual(handler.response_headers["Accept-Ranges"], "bytes")
        self.assertEqual(handler.response_headers["Cache-Control"], "private, no-store")
        self.assertEqual(handler.response_headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(handler.response_headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertTrue(stream.read_sizes)
        self.assertLessEqual(max(stream.read_sizes), music_audio.CHUNK_SIZE)

    def test_head_sends_headers_without_reading_body(self):
        stream = ReadSpy(b"0123456789")
        handler = Handler()
        with patch.object(music_audio.BrowserAudio, "open_track", return_value=(stream, 10, "audio/flac")):
            music_audio.serve_audio(handler, Client(), URI, head=True)
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.response_headers["Content-Length"], "10")
        self.assertEqual(handler.response_headers["Content-Type"], "audio/flac")
        self.assertEqual(handler.wfile.getvalue(), b"")
        self.assertEqual(stream.read_sizes, [])

    def test_invalid_range_returns_416_headers_without_body(self):
        handler = Handler("bytes=0-1,3-4")
        with patch.object(music_audio.BrowserAudio, "open_track", return_value=(io.BytesIO(b"abc"), 3, "audio/flac")):
            music_audio.serve_audio(handler, Client(), URI)
        self.assertEqual(handler.status, 416)
        self.assertEqual(handler.response_headers["Content-Range"], "bytes */3")
        self.assertEqual(handler.response_headers["Content-Length"], "0")
        self.assertEqual(handler.response_headers["Cache-Control"], "no-store")
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_stream_io_errors_after_headers_never_escape_or_add_a_response(self):
        for failure in ("seek", "read", "short", "close"):
            with self.subTest(failure=failure):
                stream = FaultStream(failure)
                handler = Handler()
                with patch.object(music_audio.BrowserAudio, "open_track",
                                  return_value=(stream, 5, "audio/flac")):
                    music_audio.serve_audio(handler, Client(), URI)
                self.assertEqual(handler.responses, [200])
                self.assertEqual(stream.closed, True)
                self.assertNotIn(b"{", handler.wfile.getvalue())
                if failure in ("seek", "read", "short"):
                    self.assertTrue(handler.close_connection)
                if failure == "read":
                    self.assertEqual(handler.wfile.getvalue(), b"x")

    def test_config_gate_and_wrong_provider(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"MUSIC_BROWSER_ROOT": root, "MUSIC_BROWSER_ALLOW_PLAYBACK": "false"}):
                self.assertFalse(music_audio.BrowserAudio(Client()).status()["available"])
            with patch.dict(os.environ, {"MUSIC_BROWSER_ROOT": root, "MUSIC_BROWSER_ALLOW_PLAYBACK": "true"}):
                client = Client()
                client.library_provider = "filesystem_smb--Other"
                self.assertFalse(music_audio.BrowserAudio(client).status()["available"])
                client.library_provider = PROVIDER
                client.configured = False
                self.assertFalse(music_audio.BrowserAudio(client).status()["available"])

    def test_dir_fd_unavailable_disables_browser_audio(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.dict(os.environ, {"MUSIC_BROWSER_ROOT": root, "MUSIC_BROWSER_ALLOW_PLAYBACK": "true"}), \
             patch.object(music_audio.os, "supports_dir_fd", set()):
            result = music_audio.BrowserAudio(Client()).status()
        self.assertFalse(result["available"])
        self.assertIn("reason", result)

    def test_unsupported_format_rejected_before_open(self):
        audio = music_audio.BrowserAudio(Client())
        with patch.object(audio, "status", return_value={"available": True}), \
             patch.object(music_audio.os, "open") as open_file:
            with self.assertRaises(ValueError):
                audio.open_track(PROVIDER + "://track/Artist/Album/cover.jpg")
        open_file.assert_not_called()

    def test_wrong_track_provider_rejected_before_filesystem_access(self):
        client = Client()
        client.resolve_local_track = lambda uri: ({}, "filesystem_local--Other://track/Artist/Album/song.flac")
        audio = music_audio.BrowserAudio(client)
        with patch.object(audio, "status", return_value={"available": True}), \
             patch.object(music_audio.os, "open") as open_file:
            with self.assertRaises(ValueError):
                audio.open_track(URI)
        open_file.assert_not_called()

    def test_fake_posix_walker_uses_no_follow_flags_and_closes_descriptors(self):
        client = Client()
        opened = []
        closed = []
        fake_stream = io.BytesIO(b"synthetic audio")

        def fake_open(path, flags, *, dir_fd=None):
            fd = 10 + len(opened)
            opened.append((path, flags, dir_fd, fd))
            return fd

        with patch.dict(os.environ, {"MUSIC_BROWSER_ROOT": "/srv/music"}), \
             patch.object(music_audio.os, "O_DIRECTORY", 0x10000, create=True), \
             patch.object(music_audio.os, "O_NOFOLLOW", 0x20000, create=True), \
             patch.object(music_audio.os, "open", side_effect=fake_open), \
             patch.object(music_audio.os, "fstat", return_value=type("Info", (), {
                 "st_mode": stat.S_IFREG | 0o600, "st_size": 15})()), \
             patch.object(music_audio.os, "fdopen", return_value=fake_stream), \
             patch.object(music_audio.os, "close", side_effect=closed.append):
            audio = music_audio.BrowserAudio(client)
            with patch.object(audio, "status", return_value={"available": True}):
                stream, size, content_type = audio.open_track(URI)

        dir_flags = os.O_RDONLY | 0x10000 | 0x20000
        file_flags = os.O_RDONLY | 0x20000 | getattr(os, "O_NONBLOCK", 0)
        self.assertEqual(opened, [("/", dir_flags, None, 10),
                                  ("srv", dir_flags, 10, 11),
                                  ("music", dir_flags, 11, 12),
                                  ("Artist", dir_flags, 12, 13),
                                  ("Album", dir_flags, 13, 14),
                                  ("song.flac", file_flags, 14, 15)])
        self.assertEqual(closed, [14, 13, 12, 11, 10])
        self.assertIs(stream, fake_stream)
        self.assertEqual((size, content_type), (15, "audio/flac"))

    def test_fake_posix_walker_closes_open_directories_after_error(self):
        client = Client()
        opened = []
        closed = []

        def fake_open(path, flags, *, dir_fd=None):
            opened.append((path, flags, dir_fd))
            if path == "private":
                raise OSError("synthetic symlink rejection")
            return 20 + len(opened)

        with patch.dict(os.environ, {"MUSIC_BROWSER_ROOT": "/private/music"}), \
             patch.object(music_audio.os, "O_DIRECTORY", 0x10000, create=True), \
             patch.object(music_audio.os, "O_NOFOLLOW", 0x20000, create=True), \
             patch.object(music_audio.os, "open", side_effect=fake_open), \
             patch.object(music_audio.os, "close", side_effect=closed.append):
            audio = music_audio.BrowserAudio(client)
            with patch.object(audio, "status", return_value={"available": True}):
                with self.assertRaises(MusicAssistantError):
                    audio.open_track(URI)

        self.assertEqual([entry[0] for entry in opened], ["/", "private"])
        self.assertEqual(closed, [21])

    @unittest.skipUnless(os.name == "posix", "POSIX dir_fd traversal required")
    def test_regular_file_opens_from_nested_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "music"
            target = root / "Artist" / "Album" / "song.flac"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"synthetic audio")
            with patch.dict(os.environ, {"MUSIC_BROWSER_ROOT": str(root), "MUSIC_BROWSER_ALLOW_PLAYBACK": "true"}):
                stream, size, content_type = music_audio.BrowserAudio(Client()).open_track(URI)
            with stream:
                self.assertEqual(stream.read(), b"synthetic audio")
            self.assertEqual(size, len(b"synthetic audio"))
            self.assertEqual(content_type, "audio/flac")

    @unittest.skipUnless(os.name == "posix", "POSIX dir_fd traversal required")
    def test_symlink_component_and_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "music"
            root.mkdir()
            outside = Path(temp) / "outside.flac"
            outside.write_bytes(b"private")
            (root / "link.flac").symlink_to(outside)
            (root / "linked-dir").symlink_to(outside.parent, target_is_directory=True)
            with patch.dict(os.environ, {"MUSIC_BROWSER_ROOT": str(root), "MUSIC_BROWSER_ALLOW_PLAYBACK": "true"}):
                audio = music_audio.BrowserAudio(Client())
                self.assertTrue(audio.status()["available"])
                for relative in ["link.flac", "linked-dir/outside.flac", "../outside.flac"]:
                    with self.subTest(relative=relative):
                        if relative.startswith(".."):
                            with self.assertRaises(ValueError):
                                audio.open_track(PROVIDER + "://track/" + relative)
                        else:
                            with self.assertRaises(MusicAssistantError):
                                audio.open_track(PROVIDER + "://track/" + relative)

    @unittest.skipUnless(os.name == "posix", "POSIX dir_fd traversal required")
    def test_symlink_in_configured_root_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            actual = base / "actual"
            actual.mkdir()
            root = actual / "music"
            root.mkdir()
            (root / "song.flac").write_bytes(b"synthetic")
            alias = base / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            with patch.dict(os.environ, {"MUSIC_BROWSER_ROOT": str(alias / "music"),
                                         "MUSIC_BROWSER_ALLOW_PLAYBACK": "true"}):
                audio = music_audio.BrowserAudio(Client())
                self.assertTrue(audio.status()["available"])
                with self.assertRaises(MusicAssistantError):
                    audio.open_track(PROVIDER + "://track/song.flac")

    def test_os_error_does_not_leak_path_or_os_message(self):
        secret_path = "/private/music/song.flac"
        with patch.dict(os.environ, {"MUSIC_BROWSER_ROOT": secret_path, "MUSIC_BROWSER_ALLOW_PLAYBACK": "true"}), \
             patch.object(music_audio.BrowserAudio, "status", return_value={"available": True}), \
             patch.object(music_audio.os, "O_DIRECTORY", 0x10000, create=True), \
             patch.object(music_audio.os, "O_NOFOLLOW", 0x20000, create=True), \
             patch.object(music_audio.os, "open", side_effect=OSError(secret_path)):
            with self.assertRaises(MusicAssistantError) as caught:
                music_audio.BrowserAudio(Client()).open_track(URI)
        self.assertNotIn(secret_path, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
