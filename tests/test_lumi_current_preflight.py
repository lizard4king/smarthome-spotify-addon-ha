"""Read-only inventory must never become an implicit deployment or grant."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "deployment/lumi_current_identity/preflight.py"
spec = importlib.util.spec_from_file_location("lumi_current_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


@pytest.fixture
def baseline(tmp_path):
    files = {}
    for app in preflight.APPS:
        folder = tmp_path / app
        folder.mkdir()
        (folder / "app.py").write_bytes(b"print('public source')\n")
        files[app] = {"app.py": hashlib.sha256(b"print('public source')\n").hexdigest()}
    return tmp_path, {"schema": 1, "source_revision": "a" * 40, "files": files}


def healthy(slug):
    app = next(app for app, (name, _) in preflight.APPS.items() if name == slug)
    return {"version": preflight.APPS[app][1], "state": "started"}


def test_baseline_matches_without_changing_files(baseline):
    root, manifest = baseline
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = preflight.check(root, manifest, info=healthy)
    assert result["baseline_match"] and result["read_only"]
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("state", ["stopped", "error", "starting", "unknown"])
def test_target_version_but_not_running_is_not_a_pass(baseline, state):
    root, manifest = baseline
    assert not preflight.check(root, manifest,
        info=lambda slug: {**healthy(slug), "state": state})["baseline_match"]


def test_newer_unknown_version_is_not_downgraded_or_accepted(baseline):
    root, manifest = baseline
    result = preflight.check(root, manifest,
        info=lambda slug: {**healthy(slug), "version": "0.1.99"})
    assert not result["baseline_match"] and result["no_install_or_restart"]


def test_changed_and_missing_public_source_are_reported(baseline):
    root, manifest = baseline
    (root / "codex_voice/app.py").write_text("pass\n")
    (root / "music_alexa/app.py").unlink()
    result = preflight.check(root, manifest, info=healthy)
    assert result["changed_files"] == ["codex_voice/app.py"]
    assert result["missing_files"] == ["music_alexa/app.py"]
    assert not result["baseline_match"]


def test_unknown_file_is_counted_without_opening_or_exposing_it(baseline, monkeypatch):
    root, manifest = baseline
    private = root / "codex_voice/private-unexpected"
    private.write_text("sensitive-marker-never-output")
    read = Path.read_bytes
    def guarded(path):
        assert path != private
        return read(path)
    monkeypatch.setattr(Path, "read_bytes", guarded)
    result = preflight.check(root, manifest, info=healthy)
    assert result["extra_file_count"] == 1 and not result["baseline_match"]
    assert "private-unexpected" not in json.dumps(result)
    assert "sensitive-marker" not in json.dumps(result)


def test_only_line_endings_are_normalized(baseline):
    root, manifest = baseline
    (root / "codex_voice/app.py").write_bytes(b"print('public source')\r\n")
    assert preflight.check(root, manifest, info=healthy)["baseline_match"]
    (root / "codex_voice/app.py").write_bytes(b"print( 'public source')\r\n")
    assert not preflight.check(root, manifest, info=healthy)["baseline_match"]


def test_ha_info_drops_options_and_uses_only_read_commands():
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert args[:3] == ["ha", "apps", "info"]
        data = {"version": "0.1.14", "state": "started", "options": {"password": "secret-marker"}}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": "ok", "data": data}))
    result = preflight.ha_info("local_codex_voice", run=run)
    assert result == {"version": "0.1.14", "state": "started"}
    assert "secret-marker" not in json.dumps(result) and len(calls) == 1


def test_namespace_fallback_is_read_only():
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert args[2:] == ["info", "local_codex_voice", "--raw-json"]
        return SimpleNamespace(returncode=int(args[1] == "apps"), stdout=json.dumps(
            {"version": "0.1.14", "state": "started"}))
    assert preflight.ha_info("local_codex_voice", run=run)["state"] == "started"
    assert [a[1] for a in calls] == ["apps", "addons"]


def test_provider_errors_are_not_printed(tmp_path, monkeypatch, capsys):
    path = tmp_path / "manifest.json"
    path.write_text("not json secret-marker")
    assert preflight.main(["--source-dir", str(tmp_path), "--manifest", str(path)]) == 1
    assert "secret-marker" not in capsys.readouterr().out


@pytest.mark.parametrize("path", ["../other", "/absolute", "a/../b", "a\\b", "C:elsewhere", ""])
def test_bad_manifest_paths_are_rejected(tmp_path, baseline, path):
    _, value = baseline
    value["files"]["codex_voice"] = {path: "a" * 64}
    file = tmp_path / "manifest.json"
    file.write_text(json.dumps(value))
    with pytest.raises(preflight.CheckError):
        preflight.read_manifest(file)


def test_public_baseline_contains_all_current_notion_and_answer_modules():
    value = preflight.read_manifest(SCRIPT.with_name("baseline.json"))
    assert value["source_revision"] == "2a3fd98b21d200411fa1c214157fe274408b3c86"
    assert "answer_pages.py" in value["files"]["music_alexa"]
    assert len([p for p in value["files"]["codex_voice"] if p.startswith("lumi_notion_")]) == 6
    assert "lumi_knowledge.py" in value["files"]["codex_voice"]
