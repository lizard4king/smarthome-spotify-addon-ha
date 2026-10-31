import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "smarthome_cockpit"))
from dashboard import codex_control


class FakeMusic:
    def __init__(self, replies):
        self.replies = replies
        self.queries = []
        self.play_calls = []

    def tracks(self, query):
        self.queries.append(query)
        value = self.replies.get(query, [])
        if isinstance(value, tuple):
            value = value[0]
            self.replies[query] = self.replies[query][1:]
        if isinstance(value, Exception):
            raise value
        return {"tracks": value}

    def play(self, *args):
        self.play_calls.append(args)


def run_music(monkeypatch, music, query):
    monkeypatch.setattr(codex_control, "MusicAssistant", lambda: music)
    broker = codex_control.ControlBroker("x" * 32, lambda: None, lambda _: None, lambda: 1000, lambda: 1000)
    broker.music_targets = lambda: {"buero": "player-buero"}
    return broker.execute({"operation": "music_play", "target": "buero", "query": query}, "music_play")


def track(uri="local://track/1", title="Ein echtes Wir", artist="AndreasPistelok"):
    return {"uri": uri, "title": title, "artist": artist}


def test_single_word_search_is_not_repeated(monkeypatch):
    music = FakeMusic({"echtes": []})
    result = run_music(monkeypatch, music, "echtes")
    assert result["receipt"] == "Ich habe keinen passenden eigenen Titel gefunden."
    assert music.queries == ["echtes"]
    assert music.play_calls == []


def test_original_misheard_title_suggests_available_title_without_playback(monkeypatch):
    music = FakeMusic({"echtes": [track()]})
    result = run_music(monkeypatch, music, "Ein echtes Bier")
    assert "Ein echtes Wir" in result["receipt"]
    assert "Bitte nenne den gewünschten Titel mit Raum noch einmal." in result["receipt"]
    assert music.queries == ["Ein echtes Bier", "echtes", "bier"]
    assert music.play_calls == []


@pytest.mark.parametrize("title", ["Blaue Wolken", "Klassische Sinfonie", "Sommerregen"])
def test_unrelated_available_title_is_not_suggested(monkeypatch, title):
    music = FakeMusic({"Ein echtes Bier": [], "echtes": [track(title=title)], "Bier": []})
    result = run_music(monkeypatch, music, "Ein echtes Bier")
    assert result == {"receipt": "Ich habe keinen passenden eigenen Titel gefunden."}
    assert music.play_calls == []


def test_at_most_two_nonempty_fallback_queries_and_uri_dedup(monkeypatch):
    music = FakeMusic({"alpha bravo charlie": [], "charlie": [track("local://track/1", "Alpha Bravo Charlie")], "alpha": [track("local://track/1", "Alpha Bravo Charlie")]})
    result = run_music(monkeypatch, music, "alpha bravo charlie")
    assert music.queries == ["alpha bravo charlie", "charlie", "alpha"]
    assert len(music.queries) <= 3 and all(music.queries)
    assert result["receipt"].count("Alpha Bravo Charlie") == 1
    assert music.play_calls == []


def test_network_error_is_not_misreported_as_missing_title(monkeypatch):
    music = FakeMusic({"echtes": TimeoutError("private details")})
    with pytest.raises(TimeoutError):
        run_music(monkeypatch, music, "echtes")
    assert music.play_calls == []


def test_fallback_failure_does_not_expose_details_or_play(monkeypatch):
    music = FakeMusic({"echtes": TimeoutError("private details")})
    result = run_music(monkeypatch, music, "Ein echtes Bier")
    assert result == {"receipt": "Kein exakter Treffer. Weitere Vorschläge sind gerade nicht abrufbar. Es wurde nichts gestartet."}
    assert music.play_calls == []


def test_control_characters_are_removed_and_receipt_bounded(monkeypatch):
    noisy = track(title="Ein\x00 echtes\nWir", artist="Artist\x1f")
    music = FakeMusic({"echtes": [noisy]})
    result = run_music(monkeypatch, music, "Ein echtes Bier")
    assert "Artist" in result["receipt"]
    assert "\x00" not in result["receipt"] and "\n" not in result["receipt"]
    assert len(result["receipt"]) <= 2000
    assert len(codex_control._safe_music_label("x" * 300, 160)) == 160
    assert len(music.play_calls) == 0


def test_fallback_words_are_unique(monkeypatch):
    music = FakeMusic({})
    run_music(monkeypatch, music, "Echtes echtes Bier")
    assert music.queries == ["Echtes echtes Bier", "echtes", "bier"]


def test_real_adapter_keeps_provider_and_availability_filters(monkeypatch):
    from dashboard.music_assistant import MusicAssistant

    monkeypatch.setenv("MUSIC_ASSISTANT_LIBRARY_PROVIDER", "filesystem_local--allowed")
    adapter = MusicAssistant()
    calls = []
    def read_library(command, **args):
        calls.append((command, args))
        if args["search"] == "Ein echtes Bier":
            return []
        return [{"media_type": "track", "name": "Ein echtes Wir", "provider_mappings": [
            {"provider_domain": "filesystem_local", "provider_instance": "filesystem_local--foreign", "item_id": "song.mp3", "available": True},
            {"provider_domain": "filesystem_local", "provider_instance": "filesystem_local--allowed", "item_id": "song.mp3", "available": False},
        ]}]
    monkeypatch.setattr(adapter, "_call", read_library)
    played = []
    monkeypatch.setattr(adapter, "play", lambda *args: played.append(args))
    result = run_music(monkeypatch, adapter, "Ein echtes Bier")
    assert result == {"receipt": "Ich habe keinen passenden eigenen Titel gefunden."}
    assert len(calls) == 3 and not played
    assert all(command == "music/tracks/library_items" and args["provider"] == "filesystem_local--allowed"
               and args["limit"] == 50 and args["offset"] == 0 for command, args in calls)


def test_exact_and_ambiguous_exact_behavior_is_unchanged(monkeypatch):
    exact = track()
    music = FakeMusic({"Ein echtes Wir": [exact]})
    monkeypatch.setattr(codex_control, "MusicAssistant", lambda: music)
    broker = codex_control.ControlBroker("x" * 32, lambda: None, lambda _: None, lambda: 1000, lambda: 1000)
    broker.music_targets = lambda: {"buero": "player-buero"}
    action = {"operation": "music_play", "target": "buero", "query": "Ein echtes Wir"}
    assert broker.execute(action, "music_play") == {"receipt": "Eigene Musik wurde von Music Assistant angenommen."}
    assert music.play_calls == [(exact["uri"], "player-buero")]
    music.replies["Ein echtes Wir"] = [exact, track("local://track/2", "Ein echtes Wir live")]
    music.play_calls.clear()
    assert broker.execute(action, "music_play") == {"receipt": "Bitte nenne den Titel genauer."}
    assert music.play_calls == []
