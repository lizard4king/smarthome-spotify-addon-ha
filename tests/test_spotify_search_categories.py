"""Bounded cross-category suggestions, without Spotify or device access."""
import importlib.util
import io
import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "smarthome_spotify_bridge"))


class SpotifySearchCategoryTests(unittest.TestCase):
    def clients(self, payload):
        for package in ("smarthome_spotify_bridge",):
            spec = importlib.util.spec_from_file_location(
                "category_search_" + package, ROOT / package / "smarthome" / "spotify_search.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            client = module.SpotifyMediaSearchClient(
                requester=lambda request, timeout: io.BytesIO(json.dumps(payload).encode()))
            yield package, client

    @staticmethod
    def payload():
        return {kind + "s": {"items": [
            {"uri": f"spotify:{kind}:{kind}{index}", "name": f"{kind} {index}"}
            for index in range(8)]} for kind in ("track", "album", "playlist", "artist")}

    def test_full_track_page_keeps_other_categories_visible(self):
        for package, client in self.clients(self.payload()):
            with self.subTest(package=package):
                results = client.search("query", access_token="synthetic", limit=8)
                self.assertEqual(len(results), 8)
                for kind in ("track", "album", "playlist", "artist"):
                    self.assertEqual(sum(result.uri.startswith(f"spotify:{kind}:") for result in results), 2)
                self.assertEqual(results[0].uri, "spotify:track:track0")

    def test_resolve_preserves_established_track_priority(self):
        for package, client in self.clients(self.payload()):
            with self.subTest(package=package):
                self.assertEqual(client.resolve("query", access_token="synthetic").track_uris,
                                 ("spotify:track:track0",))

    def test_sparse_and_malformed_sections_do_not_create_suggestions(self):
        payload = {"tracks": {"items": [None, {"name": "missing URI"}]},
                   "albums": None, "playlists": {"items": "invalid"},
                   "artists": {"items": [{"uri": "spotify:artist:artist0", "name": "One artist"}]}}
        for package, client in self.clients(payload):
            with self.subTest(package=package):
                results = client.search("query", access_token="synthetic", limit=8)
                self.assertEqual([item.title for item in results], ["One artist"])


if __name__ == "__main__":
    unittest.main()
