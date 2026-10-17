"""Synthetic Music Assistant collection contract tests; no network or playback."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.music_assistant import MusicAssistant, MusicAssistantError
from dashboard.music_library import MusicLibrary

PROVIDER = "filesystem_local--AbC"
ROOT = PROVIDER + "://"


def track(name="Song", number=1, provider=PROVIDER):
    return {"media_type": "track", "name": name, "uri": "library://track/1",
            "disc_number": 1, "track_number": number, "duration": 125,
            "artists": [{"name": "Artist"}], "album": {"name": "Album"},
            "provider_mappings": [{"provider_domain": "filesystem_local", "provider_instance": provider,
                                   "item_id": name + ".flac", "available": True}]}


def folder(path, name=None):
    return {"media_type": "folder", "provider": PROVIDER, "path": ROOT + path, "name": name or path}


def mapping(path):
    return {"media_type": "track", "provider": PROVIDER, "uri": PROVIDER + "://track/" + path,
            "name": path.rsplit("/", 1)[-1]}


class FakeClient:
    library_provider = PROVIDER
    _local_uri = staticmethod(MusicAssistant._local_uri)
    local_uri = MusicAssistant.local_uri
    call_library = MusicAssistant.call_library
    resolve_local_track = MusicAssistant.resolve_local_track
    _track = MusicAssistant._track

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _call(self, command, **args):
        self.calls.append((command, args))
        return self.responses.pop(0)


class LibraryTests(unittest.TestCase):
    def test_public_library_command_interface_rejects_non_library_commands(self):
        client = FakeClient()
        for command in [None, [], "player_queues/play_media", "music/albums/remove_item", "music/item_by_uri", "players/all"]:
            with self.subTest(command=command), self.assertRaises(ValueError):
                client.call_library(command)
        self.assertEqual(client.calls, [])

    def test_public_local_uri_is_bound_to_configured_provider(self):
        client = FakeClient()
        local = track()
        uri = PROVIDER + "://track/Song.flac"
        self.assertEqual(client.local_uri(local), uri)
        self.assertEqual(client.local_uri(local, preferred=uri), uri)
        self.assertIsNone(client.local_uri(local, preferred=PROVIDER + "://track/Other.flac"))
        self.assertIsNone(client.local_uri(track(provider="filesystem_local--Other")))
        self.assertEqual(client.calls, [])

    def test_public_resolve_local_track_reuses_existing_provenance_checks(self):
        local = track()
        client = FakeClient(local)
        self.assertEqual(client.resolve_local_track("library://track/1"), (local, PROVIDER + "://track/Song.flac"))
        self.assertEqual(client.calls, [("music/item_by_uri", {"uri": "library://track/1"})])
        client = FakeClient()
        with self.assertRaises(ValueError):
            client.resolve_local_track("https://example.test/track.mp3")
        self.assertEqual(client.calls, [])
        client = FakeClient(track(provider="filesystem_local--Other"))
        with self.assertRaises(ValueError):
            client.resolve_local_track("library://track/1")

    def test_domain_without_concrete_instance_never_browses_an_arbitrary_provider(self):
        for provider in ["filesystem_local", "filesystem_smb", "filesystem_nfs"]:
            client = FakeClient()
            client.library_provider = provider
            library = MusicLibrary(client)
            for operation in (lambda: library.albums(), lambda: library.folders(),
                              lambda: library.collection("library://album/1")):
                with self.subTest(provider=provider), self.assertRaisesRegex(MusicAssistantError, "konkrete.*Instanz"):
                    operation()
            self.assertEqual(client.calls, [])

    def test_album_list_provider_and_pagination(self):
        album = {"media_type": "album", "uri": "library://album/12", "name": "Album",
                 "artists": [{"name": "Artist"}], "provider_mappings": track()["provider_mappings"]}
        client = FakeClient([deepcopy(album) for _ in range(50)])
        result = MusicLibrary(client).albums("Album", 50)
        self.assertEqual(result["next_offset"], 100)
        self.assertEqual(result["items"][0], {"kind": "album", "uri": "library://album/12", "title": "Album", "artist": "Artist"})
        self.assertEqual(client.calls, [("music/albums/library_items", {"summary": False, "provider": PROVIDER,
                         "limit": 50, "offset": 50, "search": "Album"})])

    def test_album_list_filters_cloud_and_malformed(self):
        client = FakeClient([None, {}, {"media_type": "album", "uri": "spotify://album/x"},
                             {"media_type": "album", "uri": "library://album/12", "provider_mappings": []}])
        self.assertEqual(MusicLibrary(client).albums(), {"items": []})

    def test_artist_search_reads_real_entities_and_exact_provider(self):
        artist = {"media_type": "artist", "uri": "library://artist/27", "name": "Real Artist",
                  "provider_mappings": track()["provider_mappings"]}
        client = FakeClient([deepcopy(artist) for _ in range(50)])
        result = MusicLibrary(client).artists("Real", 50)
        self.assertEqual(result["items"][0], {"kind": "artist", "uri": "library://artist/27", "title": "Real Artist"})
        self.assertEqual(result["next_offset"], 100)
        self.assertEqual(client.calls, [("music/artists/library_items", {"summary": False, "provider": PROVIDER,
                         "limit": 50, "offset": 50, "search": "Real"})])

    def test_artist_search_rejects_other_sources_and_malformed_mappings(self):
        valid = {"media_type": "artist", "uri": "library://artist/27", "provider_mappings": track()["provider_mappings"]}
        values = [None, {}, {**valid, "uri": "spotify://artist/x"},
                  {**valid, "provider_mappings": track(provider="filesystem_local--Other")["provider_mappings"]},
                  {**valid, "provider_mappings": [{**track()["provider_mappings"][0], "provider_domain": []}]}]
        self.assertEqual(MusicLibrary(FakeClient(values)).artists(), {"items": []})
        for provider in ["", "filesystem_local", "spotify"]:
            client = FakeClient(); client.library_provider = provider
            with self.assertRaises(MusicAssistantError):
                MusicLibrary(client).artists()
            with self.assertRaises(MusicAssistantError):
                MusicLibrary(client).artist("library://artist/27")
            self.assertEqual(client.calls, [])

    def test_artist_details_keep_only_local_album_and_track_relations(self):
        album = {"media_type": "album", "uri": "library://album/12", "name": "Album",
                 "provider_mappings": track()["provider_mappings"]}
        client = FakeClient([album, {**album, "provider_mappings": []}],
                            [track(), track(provider="filesystem_local--Other"), {"media_type": "track", "uri": "spotify://track/x"}])
        result = MusicLibrary(client).artist("library://artist/27")
        self.assertEqual([item["kind"] for item in result["items"]], ["album", "track"])
        self.assertFalse(result["source_limited"])
        self.assertNotIn("complete", result)
        args = {"item_id": "27", "provider_instance_id_or_domain": "library", "provider_filter": PROVIDER}
        self.assertEqual(client.calls, [("music/artists/artist_albums", args), ("music/artists/artist_tracks", args)])

    def test_artist_details_bound_response_and_signal_upstream_limit(self):
        client = FakeClient([], [track(str(n)) for n in range(500)])
        result = MusicLibrary(client).artist("library://artist/27", 450)
        self.assertEqual(len(result["items"]), 50)
        self.assertTrue(result["source_limited"])
        self.assertNotIn("next_offset", result)
        for values in [None, {}, [track()] * 1001]:
            with self.assertRaises(MusicAssistantError):
                MusicLibrary(FakeClient(values, [])).artist("library://artist/27")
        for uri in [None, "spotify://artist/27", "library://artist/27?x", "library://artist/../27"]:
            client = FakeClient()
            with self.assertRaises(ValueError):
                MusicLibrary(client).artist(uri)
            self.assertEqual(client.calls, [])

    def test_existing_track_search_preserves_empty_and_domain_provider_configuration(self):
        for provider in ["", "filesystem_local", PROVIDER]:
            client = FakeClient([]); client.library_provider = provider
            self.assertEqual(MusicAssistant.tracks(client, "Name"), {"tracks": []})
            args = {"search": "Name", "limit": 50, "offset": 0, "summary": False}
            if provider:
                args["provider"] = provider
            self.assertEqual(client.calls, [("music/tracks/library_items", args)])

    def test_album_collection_order_and_exact_command(self):
        second_disc = track("D2", 1)
        second_disc["disc_number"] = 2
        client = FakeClient([second_disc, track("T10", 10), track("T2", 2)])
        result = MusicLibrary(client).collection("library://album/12")
        self.assertTrue(result["complete"])
        self.assertEqual(result["title"], "Album")
        self.assertEqual([t["title"] for t in result["tracks"]], ["T2", "T10", "D2"])
        self.assertEqual(client.calls, [("music/albums/album_tracks", {"item_id": "12", "provider_instance_id_or_domain": "library", "in_library_only": True})])

    def test_album_rejects_partial_local_collection(self):
        for invalid in [None, {}, track(provider="filesystem_local--Other"),
                        {"media_type": "track", "provider": "spotify", "uri": "spotify://track/x"}]:
            with self.subTest(invalid=invalid), self.assertRaises(MusicAssistantError):
                MusicLibrary(FakeClient([track(), invalid])).collection("library://album/1")

    def test_album_rejects_oversize_and_empty(self):
        for values in [[], [track()] * 1001, {}]:
            with self.assertRaises(MusicAssistantError):
                MusicLibrary(FakeClient(values)).collection("library://album/1")

    def test_configured_root_only_and_natural_order(self):
        client = FakeClient([mapping("Song10.flac"), folder("Folder"), mapping("Song2.flac")])
        result = MusicLibrary(client).folders()
        self.assertEqual(result["path"], ROOT)
        self.assertEqual([i["title"] for i in result["items"]], ["Folder", "Song2.flac", "Song10.flac"])
        self.assertEqual(client.calls, [("music/browse", {"path": ROOT})])

    def test_folder_search_pages_without_upstream_pagination(self):
        client = FakeClient([mapping("Song" + str(n) + ".flac") for n in range(60)])
        result = MusicLibrary(client).folders(query="song", offset=0)
        self.assertEqual(len(result["items"]), 50)
        self.assertEqual(result["next_offset"], 50)
        self.assertEqual(len(client.calls), 1)

    def test_folder_collection_recursive_complete(self):
        client = FakeClient([folder("Album2"), folder("Album10")], [mapping("Album2/02.flac"), mapping("Album2/01.flac")], [mapping("Album10/01.flac")])
        result = MusicLibrary(client).collection(ROOT)
        self.assertEqual([t["uri"] for t in result["tracks"]], [PROVIDER + "://track/" + p for p in ["Album2/01.flac", "Album2/02.flac", "Album10/01.flac"]])
        self.assertTrue(result["complete"])
        self.assertNotIn("kind", result["tracks"][0])

    def test_folder_rejects_foreign_unsafe_or_malformed_entries(self):
        for item in [None, {}, folder("../escape"), folder("A/B"),
                     {**folder("A"), "provider": "spotify"},
                     {**mapping("A.flac"), "uri": "https://example.test/A.flac"}]:
            with self.subTest(item=item), self.assertRaises(MusicAssistantError):
                MusicLibrary(FakeClient([item])).collection(ROOT)

    def test_folder_limits_never_return_partial_collection(self):
        with self.assertRaises(MusicAssistantError):
            MusicLibrary(FakeClient([mapping(str(n) + ".flac") for n in range(1001)])).collection(ROOT)
        with patch("dashboard.music_library.MAX_FOLDERS", 1), self.assertRaises(MusicAssistantError):
            MusicLibrary(FakeClient([mapping("A.flac"), folder("B")])).collection(ROOT)
        with patch("dashboard.music_library.MAX_DEPTH", 0), self.assertRaises(MusicAssistantError):
            MusicLibrary(FakeClient([folder("B")])).collection(ROOT)

    def test_untrusted_metadata_is_safe(self):
        value = track()
        value.update(name={}, artists=[None, {"name": []}], album=[], duration=float("nan"))
        result = MusicLibrary(FakeClient([value])).collection("library://album/1")
        self.assertEqual(result["tracks"][0]["title"], "Ohne Titel")
        self.assertNotIn("duration", result["tracks"][0])

    def test_input_validation_prevents_calls(self):
        client = FakeClient()
        library = MusicLibrary(client)
        for uri in [None, 12, "spotify://album/x", "library://album/1?x", ROOT + "../x", ROOT + "%2e%2e/x", ROOT + "C:/x", "filesystem_local--Other://"]:
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                library.collection(uri)
        for query, offset in [(None, 0), ("\n", 0), ("x" * 201, 0), ("", True), ("", -1), ("", 1.5)]:
            with self.assertRaises(ValueError):
                library.albums(query, offset)
        self.assertEqual(client.calls, [])

    def test_no_provider_never_calls_global_root(self):
        for provider in ["", "spotify", None, "filesystem_local--bad/../"]:
            client = FakeClient()
            client.library_provider = provider
            with self.assertRaises(MusicAssistantError):
                MusicLibrary(client).folders()
            self.assertEqual(client.calls, [])

    def test_invalid_browse_envelope(self):
        with self.assertRaises(MusicAssistantError):
            MusicLibrary(FakeClient({"items": []})).folders()

    def test_ma_root_and_parent_navigation_are_not_collection_content(self):
        root_nav = {"media_type": "folder", "name": "..", "item_id": "root", "provider": "library", "path": "root"}
        back_nav = {"media_type": "folder", "name": "..", "item_id": "back", "provider": PROVIDER, "path": ROOT}
        client = FakeClient([root_nav, folder("Album")], [back_nav, mapping("Album/song.flac")])
        result = MusicLibrary(client).collection(ROOT)
        self.assertEqual(len(result["tracks"]), 1)
        self.assertTrue(result["complete"])
        self.assertEqual(client.calls, [("music/browse", {"path": ROOT}), ("music/browse", {"path": ROOT + "Album"})])
        listing = MusicLibrary(FakeClient([root_nav, folder("Album")])).folders()
        self.assertEqual(len(listing["items"]), 1)

    def test_navigation_requires_exact_context_identity_and_path(self):
        genuine = {"media_type": "folder", "name": "..", "item_id": "back", "provider": PROVIDER, "path": ROOT + "Artist"}
        for invalid in [{**genuine, "path": ROOT + "Sibling"},
                        {**genuine, "provider": "filesystem_local--Other"},
                        {**genuine, "provider": "library"},
                        {**genuine, "item_id": "content"},
                        {**genuine, "name": "Parent"},
                        {"media_type": "folder", "name": "..", "item_id": "root", "provider": "library", "path": "root"}]:
            with self.subTest(invalid=invalid), self.assertRaises(MusicAssistantError):
                MusicLibrary(FakeClient([invalid, mapping("Artist/Album/song.flac")])).collection(ROOT + "Artist/Album")
        result = MusicLibrary(FakeClient([genuine, mapping("Artist/Album/song.flac")])).collection(ROOT + "Artist/Album")
        self.assertEqual(len(result["tracks"]), 1)

    def test_untrusted_dotdot_display_name_does_not_hide_real_content(self):
        # Display names are untrusted metadata; a genuine child named '..' is still visited.
        client = FakeClient([folder("Child", "..")], [mapping("Child/song.flac")])
        result = MusicLibrary(client).collection(ROOT)
        self.assertEqual(len(result["tracks"]), 1)
        self.assertEqual(client.calls[-1], ("music/browse", {"path": ROOT + "Child"}))

    def test_collection_budget_checks_before_and_after_calls_without_sleep(self):
        for clock_values, responses, uri, expected_calls in [
            ([0, 20], [], "library://album/1", 0),
            ([0, 1, 20], [[track()]], "library://album/1", 1),
            ([0, 1, 20], [[mapping("song.flac")]], ROOT, 1),
            ([0, 1, 2, 20], [[mapping("song.flac"), folder("Child")]], ROOT, 1),
            ([0, 1, 2, 3, 20], [[folder("Child")], [mapping("Child/song.flac")]], ROOT, 2),
        ]:
            client = FakeClient(*responses)
            with self.subTest(uri=uri, clock_values=clock_values), patch("dashboard.music_library.time.monotonic", side_effect=clock_values):
                with self.assertRaisesRegex(MusicAssistantError, "Zeitlimit"):
                    MusicLibrary(client).collection(uri)
            self.assertEqual(len(client.calls), expected_calls)

    def test_folder_paths_reject_empty_components(self):
        for path in ["a//b", "a/", "/a", "a%2f%2fb"]:
            client = FakeClient()
            with self.subTest(path=path), self.assertRaises(ValueError):
                MusicLibrary(client).folders(ROOT + path)
            self.assertEqual(client.calls, [])

    def test_browse_tracks_must_be_direct_children(self):
        for path in ["Sibling/song.flac", "Album/Child/song.flac", "Album%2fChild%2fsong.flac", "song.flac"]:
            with self.subTest(path=path), self.assertRaises(MusicAssistantError):
                MusicLibrary(FakeClient([mapping(path)])).collection(ROOT + "Album")
            result = MusicLibrary(FakeClient([mapping(path)])).folders(ROOT + "Album")
            self.assertEqual(result["items"], [])
        valid = MusicLibrary(FakeClient([mapping("Album/song.flac")])).collection(ROOT + "Album")
        self.assertEqual(len(valid["tracks"]), 1)

    def test_playlist_is_not_silently_omitted_from_collection(self):
        playlist = {"media_type": "playlist", "provider": PROVIDER, "uri": PROVIDER + "://playlist/list.m3u", "name": "list.m3u"}
        with self.assertRaises(MusicAssistantError):
            MusicLibrary(FakeClient([mapping("song.flac"), playlist])).collection(ROOT)


if __name__ == "__main__":
    unittest.main()
