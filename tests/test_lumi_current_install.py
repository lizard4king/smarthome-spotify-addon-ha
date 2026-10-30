"""Offline release state machine; no HA, model, credentials or real devices."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "deployment/lumi_current_identity/install.py"
spec = importlib.util.spec_from_file_location("current_identity_install", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def metadata(version):
    options = {"private": "SECRET-MARKER"}
    options.update({k: "SECRET-MARKER" for k in release.VISIBLE_OPTIONS[
        "codex_voice" if version == "0.1.14" else "music_alexa"]})
    return {"version": version, "state": "started", "options": options,
            "network": {"5000/tcp": None}, "boot": "manual", "protected": True,
            "watchdog": False, "hostname": "synthetic-host", "version_latest": version}


class FakeHa:
    def __init__(self, *, uncertain=None, wrong_catalog=False):
        self.namespace = "apps"
        self.apps = {"local_codex_voice": metadata("0.1.14"), release.MUSIC: metadata("0.1.9")}
        self.calls = []
        self.uncertain = uncertain
        self.wrong_catalog = wrong_catalog

    def info(self, slug):
        self.calls.append(("info", slug))
        return copy.deepcopy(self.apps[slug])

    def confirm_contract(self):
        self.calls.append(("confirm_contract",))

    def backup(self, slug):
        assert slug == "1234abcd"
        self.calls.append(("backup_info", slug))
        return {"slug": slug, "music_version": "0.1.9"}

    def mutate(self, op):
        self.calls.append((op,))
        # Supervisor permits rebuild only at the already installed store version.
        # A changed version requires update; this models that external contract.
        if op == "rebuild":
            if self.apps[release.MUSIC]["version"] != self.apps[release.MUSIC]["version_latest"]:
                raise release.CheckError("supervisor_rebuild_version_changed")
            return
        assert op in {"reload", "update"}
        if op == "reload" and not self.wrong_catalog:
            self.apps[release.MUSIC]["version_latest"] = "0.1.10"
        if op == "update":
            self.apps[release.MUSIC]["version"] = "0.1.10"
        if op == self.uncertain:
            raise release.CheckError("mutation_outcome_uncertain")


@pytest.fixture
def prepared_case(tmp_path, monkeypatch):
    source = tmp_path / "sources"
    manifest = {"schema": 1, "source_revision": "2a3fd98b21d200411fa1c214157fe274408b3c86", "files": {}}
    for app in ("codex_voice", "music_alexa"):
        folder = source / app
        folder.mkdir(parents=True)
        names = ["README.md", "config.yaml"] if app == "codex_voice" else list(
            set(release.CANDIDATE) - {"lumi_identity.py"})
        entries = {}
        for name in names:
            data = ("baseline-public-" + app + "/" + name + "\n").encode()
            (folder / name).write_bytes(data)
            entries[name] = release.sha(data)
        manifest["files"][app] = entries
    monkeypatch.setattr(release, "BASELINE_MANIFEST_SHA", release.sha(release.canonical(manifest)))
    payload = {name: (ROOT / "music_alexa" / name).read_bytes() for name in release.CANDIDATE}
    return source, manifest, payload, tmp_path / "journal"


def build(case, ha=None):
    return release.Release(*case[:3], "a" * 64, case[3], ha=ha or FakeHa())


def test_plan_is_read_only_and_never_uses_a_mutating_command(prepared_case):
    obj = build(prepared_case)
    before = {p: p.read_bytes() for p in obj.root.rglob("*") if p.is_file()}
    assert obj.plan()["no_install_or_restart"]
    assert not obj.journal.path.exists()
    assert all(call[0] == "info" for call in obj.ha.calls)
    assert {p: p.read_bytes() for p in obj.root.rglob("*") if p.is_file()} == before


def test_one_music_update_preserves_codex_options_and_all_other_sources(prepared_case):
    obj = build(prepared_case)
    codex = {p: p.read_bytes() for p in (obj.root / "codex_voice").rglob("*") if p.is_file()}
    assert obj.apply("1234abcd")["verified"]
    assert [c for c in obj.ha.calls if c[0] in ("reload", "update")] == [("reload",), ("update",)]
    assert {p: p.read_bytes() for p in codex} == codex
    assert obj.status()["verified"]
    assert "SECRET-MARKER" not in b"".join(p.read_bytes() for p in obj.journal.path.iterdir()).decode()


@pytest.mark.parametrize("fault", ["reload", "update"])
def test_uncertain_submit_is_never_retried_even_if_runtime_reached_target(prepared_case, fault):
    obj = build(prepared_case, FakeHa(uncertain=fault))
    with pytest.raises(release.CheckError, match="mutation_outcome_uncertain"):
        obj.apply("1234abcd")
    calls = list(obj.ha.calls)
    status = obj.status()
    assert status["outcome_uncertain"] and not status["verified"]
    with pytest.raises(release.CheckError, match="journal_exists_use_status"):
        obj.apply("1234abcd")
    assert [c for c in obj.ha.calls if c[0] in ("reload", "update")] == [
        c for c in calls if c[0] in ("reload", "update")]


def test_mismatched_catalog_stops_before_update(prepared_case):
    obj = build(prepared_case, FakeHa(wrong_catalog=True))
    with pytest.raises(release.CheckError, match="target_catalog_not_confirmed"):
        obj.apply("1234abcd")
    assert ("reload",) in obj.ha.calls and ("update",) not in obj.ha.calls


def test_version_bump_uses_update_under_the_supervisor_version_guard(prepared_case):
    ha = FakeHa()
    ha.mutate("reload")
    with pytest.raises(release.CheckError, match="supervisor_rebuild_version_changed"):
        ha.mutate("rebuild")
    assert ha.apps[release.MUSIC]["version"] == "0.1.9"
    ha.calls.clear()
    obj = build(prepared_case, ha)
    assert obj.apply("1234abcd")["verified"]
    assert ("rebuild",) not in ha.calls
    assert [c for c in ha.calls if c[0] == "update"] == [("update",)]


def test_cli_rebuild_is_refused_without_any_external_command():
    calls = []
    ha = release.Ha(run=lambda *a, **kw: calls.append(a))
    ha.namespace = "apps"
    with pytest.raises(release.CheckError, match="mutation_refused"):
        ha.mutate("rebuild")
    assert calls == []


@pytest.mark.parametrize("fault", ["source", "extra", "version", "stopped", "options_unavailable", "protection_unavailable",
                                 "empty_options", "partial_options", "codex_empty_options", "codex_partial_options"])
def test_baseline_failures_never_create_journal_or_send_mutation(prepared_case, fault):
    obj = build(prepared_case)
    if fault == "source":
        (obj.root / "music_alexa/companion.py").write_text("changed")
    elif fault == "extra":
        (obj.root / "music_alexa/private-extra").write_text("MUST-NOT-BE-READ")
    elif fault == "version":
        obj.ha.apps[release.MUSIC]["version"] = "0.1.99"
    elif fault == "stopped":
        obj.ha.apps[release.MUSIC]["state"] = "stopped"
    elif fault == "options_unavailable":
        del obj.ha.apps[release.MUSIC]["options"]
    elif fault in {"empty_options", "partial_options", "codex_empty_options", "codex_partial_options"}:
        slug = "local_codex_voice" if fault.startswith("codex_") else release.MUSIC
        obj.ha.apps[slug]["options"] = {} if fault.endswith("empty_options") else {"private": "SECRET-MARKER"}
    else:
        del obj.ha.apps[release.MUSIC]["protected"]
    with pytest.raises(release.CheckError):
        obj.apply("1234abcd")
    assert not obj.journal.path.exists()
    assert not any(c[0] in ("reload", "update") for c in obj.ha.calls)


def test_backup_cannot_be_assumed_from_a_missing_or_wrong_slug(prepared_case):
    obj = build(prepared_case)
    obj.ha.backup = lambda slug: (_ for _ in ()).throw(release.CheckError("backup_not_verified"))
    with pytest.raises(release.CheckError, match="backup_not_verified"):
        obj.prepare("1234abcd")
    assert not obj.journal.path.exists()


def test_boundary_drift_after_prepare_refuses_stage(prepared_case):
    obj = build(prepared_case)
    obj.prepare("1234abcd")
    obj.ha.apps[release.MUSIC]["options"]["private"] = "CHANGED"
    with pytest.raises(release.CheckError, match="app_boundaries_changed"):
        obj.stage()
    assert not obj.journal.exists("stage-intent.json")


def test_corrupt_public_backup_refuses_stage_and_status(prepared_case):
    obj = build(prepared_case)
    obj.prepare("1234abcd")
    (obj.journal.path / "backup-companion.py.bin").write_bytes(b"tampered")
    with pytest.raises(release.CheckError, match="journal_backup_mismatch"):
        obj.stage()
    with pytest.raises(release.CheckError, match="journal_backup_mismatch"):
        obj.status()


def test_crash_during_stage_records_intent_and_never_resumes(prepared_case, monkeypatch):
    obj = build(prepared_case)
    obj.prepare("1234abcd")
    monkeypatch.setattr(release.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("synthetic crash")))
    with pytest.raises(OSError):
        obj.stage()
    assert obj.journal.exists("stage-intent.json")
    assert not obj.journal.exists("stage-ack.json")
    assert obj.status()["outcome_uncertain"]
    with pytest.raises(release.CheckError):
        obj.apply("1234abcd")
    assert not any(c[0] in ("reload", "update") for c in obj.ha.calls)


def test_success_marker_does_not_hide_later_runtime_or_source_drift(prepared_case):
    obj = build(prepared_case)
    obj.apply("1234abcd")
    obj.ha.apps[release.MUSIC]["state"] = "stopped"
    assert not obj.status()["verified"]
    obj.ha.apps[release.MUSIC]["state"] = "started"
    (obj.root / "music_alexa/companion.py").write_bytes(b"changed later")
    assert not obj.status()["verified"]


def test_corrupt_ack_is_not_treated_as_success(prepared_case):
    obj = build(prepared_case)
    obj.apply("1234abcd")
    (obj.journal.path / "update-ack.json").write_text('{"package_sha":"wrong"}')
    with pytest.raises(release.CheckError, match="journal_binding_mismatch"):
        obj.status()


def test_backup_revoked_after_prepare_blocks_all_source_changes(prepared_case):
    obj = build(prepared_case)
    obj.prepare("1234abcd")
    obj.ha.backup = lambda slug: (_ for _ in ()).throw(release.CheckError("backup_not_verified"))
    with pytest.raises(release.CheckError, match="backup_not_verified"):
        obj.stage()
    assert not obj.journal.exists("stage-intent.json")


def test_status_never_emits_unvalidated_provider_values(prepared_case):
    obj = build(prepared_case)
    obj.prepare("1234abcd")
    obj.ha.apps[release.MUSIC]["version"] = "SECRET-MARKER"
    with pytest.raises(release.CheckError, match="ha_info_invalid"):
        obj.status()


def test_default_manifest_is_bound_to_reviewed_content(prepared_case, monkeypatch):
    source, manifest, payload, journal = prepared_case
    monkeypatch.setattr(release, "BASELINE_MANIFEST_SHA", "0" * 64)
    with pytest.raises(release.CheckError, match="baseline_manifest_binding_mismatch"):
        release.Release(source, manifest, payload, "a" * 64, journal, ha=FakeHa())


def test_cli_mutation_uses_one_proven_namespace_and_never_falls_back():
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args == ["ha", "apps", "info", release.MUSIC, "--raw-json"]:
            return SimpleNamespace(returncode=1, stdout=b"")
        if args == ["ha", "addons", "info", release.MUSIC, "--raw-json"]:
            return SimpleNamespace(returncode=0, stdout=b'{"result":"ok","data":{}}')
        if args[-1] == "--help":
            return SimpleNamespace(returncode=0, stdout=b"update --raw-json --no-progress")
        raise subprocess.TimeoutExpired(args, 900)
    ha = release.Ha(run=run)
    ha.info(release.MUSIC)
    ha.confirm_contract()
    with pytest.raises(release.CheckError, match="mutation_outcome_uncertain"):
        ha.mutate("update")
    update = [a for a in calls if "update" in a and "--help" not in a]
    assert update == [["ha", "addons", "update", release.MUSIC, "--raw-json", "--no-progress"]]


def test_backup_metadata_requires_exact_music_version_and_slug():
    ha = release.Ha(run=lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b'{"result":"ok","data":{"slug":"1234abcd","addons":[{"slug":"local_music_alexa","version":"0.1.9"}]}}'))
    assert ha.backup("1234abcd") == {"slug": "1234abcd", "music_version": "0.1.9"}
    for bad in (None, "?", "../other", "1234ABCd"):
        with pytest.raises(release.CheckError, match="invalid_backup_slug"):
            ha.backup(bad)
    ha.run = lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b'{"data":{"slug":"1234abcd","addons":[{"slug":"local_music_alexa","version":"0.1.7"}]}}')
    with pytest.raises(release.CheckError, match="backup_not_verified"):
        ha.backup("1234abcd")


def test_candidate_zip_checks_exact_inventory_and_all_file_hashes(tmp_path):
    path = tmp_path / "candidate.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name in release.CANDIDATE:
            archive.write(ROOT / "music_alexa" / name, "music_alexa/" + name)
    assert set(release.candidate_files(path, release.sha(path.read_bytes()))) == set(release.CANDIDATE)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("private-extra", b"MUST-NOT-BE-READ")
    with pytest.raises(release.CheckError, match="invalid_package_inventory"):
        release.candidate_files(path, release.sha(path.read_bytes()))


def test_main_never_prints_provider_or_configuration_values(tmp_path, capsys):
    assert release.main(["--source-dir", str(tmp_path), "--package", str(tmp_path / "not-here"),
        "--package-sha256", "a" * 64, "--journal-dir", str(tmp_path / "journal")]) == 1
    text = capsys.readouterr().out
    assert "SECRET" not in text and str(tmp_path) not in text
