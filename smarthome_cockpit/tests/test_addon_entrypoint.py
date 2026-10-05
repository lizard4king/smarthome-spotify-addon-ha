"""Mock-only tests of the private MA handoff; never open its real path."""
import io
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, mock_open, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import addon_entrypoint as entry

HANDOFF = {"schema": 1, "music_assistant_url": "http://offline-ma.test:8095",
           "music_assistant_token": "offline-test-token", "music_assistant_allow_playback": False,
           "metadata": "ignored"}


class Handle(io.BytesIO):
    def fileno(self):
        return 11


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_library_provider_loaded_without_changing_credentials(self):
        options = {"music_assistant_library_provider": "filesystem_local--example",
                   "music_assistant_token": "existing-offline-token"}
        with patch("builtins.open", mock_open(read_data=json.dumps(options))), \
                patch.object(entry.os, "open"):
            entry._load_options_fallback()
        self.assertEqual(os.environ["MUSIC_ASSISTANT_LIBRARY_PROVIDER"], "filesystem_local--example")
        self.assertEqual(os.environ["MUSIC_ASSISTANT_TOKEN"], "existing-offline-token")

    def network_free_handoff(self, value=HANDOFF, *, raw=None, directory_mode=0o700,
                             file_mode=0o600, file_type=stat.S_IFREG, size=None):
        data = raw if raw is not None else json.dumps(value).encode()
        opener = Mock(side_effect=[9, 11])
        directory = SimpleNamespace(st_mode=stat.S_IFDIR | directory_mode)
        file = SimpleNamespace(st_mode=file_type | file_mode, st_size=len(data) if size is None else size)
        patches = [patch.object(entry.os, "open", opener),
                   patch.object(entry.os, "fstat", side_effect=lambda fd: directory if fd == 9 else file),
                   patch.object(entry.os, "fdopen", return_value=Handle(data)),
                   patch.object(entry.os, "close"),
                   patch.object(entry.os, "O_DIRECTORY", 0x10000, create=True),
                   patch.object(entry.os, "O_NOFOLLOW", 0x20000, create=True),
                   patch.object(entry.os, "O_NONBLOCK", 0x40000, create=True)]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)
        return opener

    def test_valid_private_handoff_only_ma_and_read_only(self):
        os.environ.update(HOME_ASSISTANT_TOKEN="ha-existing", SPOTIFY_BRIDGE_SECRET="bridge-existing",
                          HOME_ASSISTANT_ALLOW_WRITES="true", MUSIC_ASSISTANT_ALLOW_PLAYBACK="true")
        opener = self.network_free_handoff()
        entry._load_private_music_fallback({})
        self.assertEqual(os.environ["MUSIC_ASSISTANT_TOKEN"], HANDOFF["music_assistant_token"])
        self.assertEqual(os.environ["MUSIC_ASSISTANT_URL"], HANDOFF["music_assistant_url"])
        self.assertEqual(os.environ["MUSIC_ASSISTANT_ALLOW_PLAYBACK"], "false")
        self.assertEqual(os.environ["HOME_ASSISTANT_TOKEN"], "ha-existing")
        self.assertEqual(os.environ["SPOTIFY_BRIDGE_SECRET"], "bridge-existing")
        self.assertEqual(os.environ["HOME_ASSISTANT_ALLOW_WRITES"], "true")
        self.assertEqual(opener.call_args_list[0].args[0], entry.MUSIC_HANDOFF.parent)
        self.assertEqual(opener.call_args_list[1].args[0], entry.MUSIC_HANDOFF.name)
        self.assertEqual(opener.call_args_list[1].kwargs, {"dir_fd": 9})
        self.assertTrue(opener.call_args_list[1].args[1] & entry.os.O_NOFOLLOW)
        self.assertTrue(opener.call_args_list[1].args[1] & entry.os.O_NONBLOCK)

    def test_explicit_token_option_wins_without_open(self):
        os.environ.update(MUSIC_ASSISTANT_TOKEN="option-token", MUSIC_ASSISTANT_URL="http://options.test",
                          MUSIC_ASSISTANT_ALLOW_PLAYBACK="true")
        with patch.object(entry.os, "open") as opener:
            entry._load_private_music_fallback({"music_assistant_token": "option-token"})
            opener.assert_not_called()
        self.assertEqual(os.environ["MUSIC_ASSISTANT_TOKEN"], "option-token")
        self.assertEqual(os.environ["MUSIC_ASSISTANT_ALLOW_PLAYBACK"], "true")

    def test_process_token_not_overwritten_and_playback_blocked(self):
        os.environ["MUSIC_ASSISTANT_TOKEN"] = "existing-process-token"
        with patch.object(entry.os, "open") as opener:
            entry._load_private_music_fallback({})
            opener.assert_not_called()
        self.assertEqual(os.environ["MUSIC_ASSISTANT_TOKEN"], "existing-process-token")
        self.assertEqual(os.environ["MUSIC_ASSISTANT_ALLOW_PLAYBACK"], "false")

    def test_url_option_preserved(self):
        matching_url = HANDOFF["music_assistant_url"] + "/"
        os.environ["MUSIC_ASSISTANT_URL"] = matching_url
        self.network_free_handoff()
        entry._load_private_music_fallback({"music_assistant_token": "", "music_assistant_url": matching_url})
        self.assertEqual(os.environ["MUSIC_ASSISTANT_URL"], matching_url)
        self.assertEqual(os.environ["MUSIC_ASSISTANT_TOKEN"], HANDOFF["music_assistant_token"])

    def test_mismatched_url_rejects_handoff_token(self):
        os.environ["MUSIC_ASSISTANT_URL"] = "http://options.test:8095"
        self.network_free_handoff()
        entry._load_private_music_fallback({"music_assistant_token": "", "music_assistant_url": "http://options.test:8095"})
        self.assertEqual(os.environ["MUSIC_ASSISTANT_URL"], "http://options.test:8095")
        self.assertNotIn("MUSIC_ASSISTANT_TOKEN", os.environ)
        self.assertEqual(os.environ["MUSIC_ASSISTANT_ALLOW_PLAYBACK"], "false")

    def test_missing_symlink_and_permission_fail_closed(self):
        for error in (FileNotFoundError(), PermissionError(), OSError("symlink refused")):
            with self.subTest(error=type(error).__name__), patch.object(entry.os, "O_DIRECTORY", 0, create=True), \
                    patch.object(entry.os, "O_NOFOLLOW", 0, create=True), patch.object(entry.os, "open", side_effect=error):
                entry._load_private_music_fallback({})
                self.assertNotIn("MUSIC_ASSISTANT_TOKEN", os.environ)
                self.assertEqual(os.environ["MUSIC_ASSISTANT_ALLOW_PLAYBACK"], "false")

    def test_directory_permissions_checked_before_file_open(self):
        opener = self.network_free_handoff(directory_mode=0o755)
        entry._load_private_music_fallback({})
        self.assertEqual(opener.call_count, 1)
        self.assertNotIn("MUSIC_ASSISTANT_TOKEN", os.environ)

    def test_file_permissions_rejected(self):
        self.network_free_handoff(file_mode=0o644)
        entry._load_private_music_fallback({})
        self.assertNotIn("MUSIC_ASSISTANT_TOKEN", os.environ)

    def test_non_regular_file_rejected(self):
        self.network_free_handoff(file_type=stat.S_IFIFO)
        entry._load_private_music_fallback({})
        self.assertNotIn("MUSIC_ASSISTANT_TOKEN", os.environ)

    def test_oversized_stat_rejected(self):
        self.network_free_handoff(size=entry.MAX_HANDOFF + 1)
        entry._load_private_music_fallback({})
        self.assertNotIn("MUSIC_ASSISTANT_TOKEN", os.environ)

    def test_growth_after_stat_rejected(self):
        self.network_free_handoff(raw=b"x" * (entry.MAX_HANDOFF + 1), size=1)
        entry._load_private_music_fallback({})
        self.assertNotIn("MUSIC_ASSISTANT_TOKEN", os.environ)

    def test_malformed_json_rejected_without_output(self):
        self.network_free_handoff(raw=b'{"token":"private-test-content"')
        with patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO) as errors:
            entry._load_private_music_fallback({})
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("MUSIC_ASSISTANT_TOKEN", os.environ)

    def test_fdopen_failure_closes_both_descriptors(self):
        self.network_free_handoff()
        with patch.object(entry.os, "fdopen", side_effect=OSError()), patch.object(entry.os, "close") as closer:
            entry._load_private_music_fallback({})
            self.assertEqual([call.args[0] for call in closer.call_args_list], [11, 9])
        self.assertNotIn("MUSIC_ASSISTANT_TOKEN", os.environ)

    def test_deep_json_rejected_without_output(self):
        raw = b"[" * 2000 + b"0" + b"]" * 2000
        self.assertLess(len(raw), entry.MAX_HANDOFF)
        self.network_free_handoff(raw=raw)
        with patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO) as errors:
            entry._load_private_music_fallback({})
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("MUSIC_ASSISTANT_TOKEN", os.environ)
        self.assertNotIn("MUSIC_ASSISTANT_URL", os.environ)
        self.assertEqual(os.environ["MUSIC_ASSISTANT_ALLOW_PLAYBACK"], "false")

    def test_empty_option_token_loads_handoff_without_changing_ha_or_bridge(self):
        os.environ.update(HOME_ASSISTANT_TOKEN="ha-existing", SPOTIFY_BRIDGE_SECRET="bridge-existing")
        self.network_free_handoff()
        options = {"music_assistant_token": "", "music_assistant_allow_playback": True}
        with patch("builtins.open", mock_open(read_data=json.dumps(options))):
            entry._load_options_fallback()
        self.assertEqual(os.environ["MUSIC_ASSISTANT_TOKEN"], HANDOFF["music_assistant_token"])
        self.assertEqual(os.environ["MUSIC_ASSISTANT_ALLOW_PLAYBACK"], "false")
        self.assertEqual(os.environ["HOME_ASSISTANT_TOKEN"], "ha-existing")
        self.assertEqual(os.environ["SPOTIFY_BRIDGE_SECRET"], "bridge-existing")

    def test_invalid_field_types_and_playback_rejected(self):
        values = [[], dict(HANDOFF, schema=True), dict(HANDOFF, schema=2),
                  dict(HANDOFF, music_assistant_token=17), dict(HANDOFF, music_assistant_url=[]),
                  dict(HANDOFF, music_assistant_allow_playback=True),
                  dict(HANDOFF, music_assistant_token="bad\nheader"),
                  dict(HANDOFF, music_assistant_url="http://user:pass@ma.test"),
                  dict(HANDOFF, music_assistant_url="http://ma.test/api")]
        for value in values:
            with self.subTest(value_type=type(value).__name__):
                self.network_free_handoff(value)
                entry._load_private_music_fallback({})
                self.assertNotIn("MUSIC_ASSISTANT_TOKEN", os.environ)

    def test_load_options_wins_and_preserves_existing_credentials(self):
        options = {"home_assistant_token": "ha-option", "bridge_secret": "bridge-option",
                   "allow_writes": True, "music_assistant_token": "ma-option",
                   "music_assistant_url": "http://options.test:8095", "music_assistant_allow_playback": True}
        with patch("builtins.open", mock_open(read_data=json.dumps(options))), patch.object(entry.os, "open") as opener:
            entry._load_options_fallback()
            opener.assert_not_called()
        self.assertEqual(os.environ["HOME_ASSISTANT_TOKEN"], "ha-option")
        self.assertEqual(os.environ["SPOTIFY_BRIDGE_SECRET"], "bridge-option")
        self.assertEqual(os.environ["MUSIC_ASSISTANT_TOKEN"], "ma-option")
        self.assertEqual(os.environ["MUSIC_ASSISTANT_ALLOW_PLAYBACK"], "true")

    def test_default_control_options_preserve_legacy_entity_mapping(self):
        from smarthome.home_assistant import HomeAssistantConfig
        os.environ.update(HOME_ASSISTANT_URL="http://127.0.0.1:8123",
                          HOME_ASSISTANT_TOKEN="offline-test-token",
                          HOME_ASSISTANT_WOHNZIMMERLICHT="light.legacy_lamp")
        options = {"home_assistant_entities_json": "", "home_assistant_write_allowlist": []}
        with patch("builtins.open", mock_open(read_data=json.dumps(options))), \
                patch.object(entry, "_load_private_music_fallback"):
            entry._load_options_fallback()
        config = HomeAssistantConfig.from_environment()
        self.assertEqual(config.entities, {"wohnzimmerlicht": "light.legacy_lamp"})
        self.assertIsNone(config.write_allowlist)

    def test_control_options_do_not_replace_existing_process_mapping(self):
        os.environ.update(HOME_ASSISTANT_ENTITIES_JSON='{"existing":"light.existing"}',
                          HOME_ASSISTANT_WRITE_ALLOWLIST="existing",
                          CODEX_CONTROL_SECRET="offline-existing-secret")
        options = {"home_assistant_entities_json": '{"new":"light.new"}',
                   "home_assistant_write_allowlist": ["new"],
                   "codex_control_secret": "offline-new-secret"}
        with patch("builtins.open", mock_open(read_data=json.dumps(options))), \
                patch.object(entry, "_load_private_music_fallback"):
            entry._load_options_fallback()
        self.assertEqual(os.environ["HOME_ASSISTANT_ENTITIES_JSON"], '{"existing":"light.existing"}')
        self.assertEqual(os.environ["HOME_ASSISTANT_WRITE_ALLOWLIST"], "existing")
        self.assertEqual(os.environ["CODEX_CONTROL_SECRET"], "offline-existing-secret")


if __name__ == "__main__":
    unittest.main()
