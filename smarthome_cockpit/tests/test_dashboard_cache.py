"""Static release/cache contracts without sockets, network or playback."""
import io
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.server import CockpitHandler, ROOT


def handler(path, method="GET", **headers):
    request = object.__new__(CockpitHandler)
    request.path, request.command = path, method
    request.directory = str(ROOT)
    request.request_version = "HTTP/1.1"
    request.requestline = method + " " + path + " HTTP/1.1"
    request.headers = Message()
    request.headers["Host"] = "cockpit.test"
    for key, value in headers.items(): request.headers[key] = value
    request.wfile = io.BytesIO()
    request.log_request = Mock()
    return request


def response(request):
    raw = request.wfile.getvalue()
    head, body = raw.split(b"\r\n\r\n", 1)
    status, headers = head.split(b"\r\n", 1)
    return int(status.split()[1]), BytesParser().parsebytes(headers), body


class AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        candidate = attrs.get("src") if tag == "script" else attrs.get("href") if tag == "link" else None
        if candidate and urlsplit(candidate).path in {"music-library.js", "music-library.css", "music-workspace.js", "music-workspace.css"}:
            self.assets.append(candidate)


class DashboardCacheTests(unittest.TestCase):
    def test_all_music_assets_share_installed_release_token(self):
        config = (ROOT.parent / "config.yaml").read_text(encoding="utf-8")
        version = re.search(r'^version: "([^"{}]+)"$', config, re.MULTILINE).group(1)
        parser = AssetReferences()
        parser.feed((ROOT / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(len(parser.assets), 4)
        self.assertEqual(len({urlsplit(value).path for value in parser.assets}), 4)
        for value in parser.assets:
            self.assertEqual(parse_qs(urlsplit(value).query), {"v": [version]})

    def test_html_200_and_304_both_require_revalidation(self):
        for path in ("/", "/index.html", "/index.html?v=synthetic"):
            request = handler(path)
            stream = request.send_head()
            self.assertIsNotNone(stream)
            stream.close()
            status, headers, _ = response(request)
            self.assertEqual(status, 200)
            self.assertEqual(headers.get_all("Cache-Control"), ["no-cache"])
            self.assertFalse(request._revalidate_static_html)
            request = handler(path, **{"If-Modified-Since": headers["Last-Modified"]})
            self.assertIsNone(request.send_head())
            status, headers, body = response(request)
            self.assertEqual(status, 304)
            self.assertEqual(headers.get_all("Cache-Control"), ["no-cache"])
            self.assertEqual(body, b"")

    def test_versioned_assets_still_resolve_to_static_files(self):
        for name in ("music-library.js", "music-library.css", "music-workspace.js", "music-workspace.css"):
            request = handler("/" + name + "?v=0.1.20")
            stream = request.send_head()
            try: self.assertEqual(stream.read(), (ROOT / name).read_bytes())
            finally: stream.close()
            status, headers, _ = response(request)
            self.assertEqual(status, 200)
            self.assertEqual(headers.get_all("Cache-Control", []), [])

    def test_json_audio_and_artwork_keep_single_original_cache_policy(self):
        request = handler("/api/music/status")
        with patch("dashboard.server.MusicAssistant") as client:
            client.return_value.status.return_value = {"available": True}
            request.do_GET()
        self.assertEqual(response(request)[1].get_all("Cache-Control"), ["no-store"])

        request = handler("/api/music/artwork?uri=synthetic")
        with patch("dashboard.server.MusicAssistant") as client:
            client.return_value.artwork.return_value = (b"synthetic", "image/png")
            request.do_GET()
        self.assertEqual(response(request)[1].get_all("Cache-Control"), ["private, max-age=300"])

        request = handler("/api/music/audio?uri=synthetic", Range="bytes=0-3")
        with patch("dashboard.server.MusicAssistant"), patch("dashboard.music_audio.BrowserAudio") as audio:
            audio.return_value.open_track.return_value = (io.BytesIO(b"synthetic"), 9, "audio/flac")
            request.do_GET()
        status, headers, body = response(request)
        self.assertEqual(status, 206)
        self.assertEqual(headers.get_all("Cache-Control"), ["private, no-store"])
        self.assertEqual(body, b"synt")


if __name__ == "__main__": unittest.main()
