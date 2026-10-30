"""Incremental Music identity release; the default operation is read-only.

The reviewed baseline and five pinned public candidate files are mandatory.
Existing journals are inspected with status, never resumed or overwritten.
Only Music is updated; Codex, options, owners and access settings are never set.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import zipfile

_spec = importlib.util.spec_from_file_location("lumi_identity_preflight", Path(__file__).with_name("preflight.py"))
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)
CheckError = preflight.CheckError

MUSIC = "local_music_alexa"
TARGET_VERSION = "0.1.10"
BASELINE_MANIFEST_SHA = "d8475be843e3681c69bc8ac08435d985df7598d2896eeee4fea433a56c9f6e30"
LIMIT = 2 * 1024 * 1024
CANDIDATE = {
    "Dockerfile": "7e19dc7dd1cd2e9b189e8152e5590833e23fb8a9083e18af50c30759fbe3e8fa",
    "companion.py": "adf8423ccdec8454e9ce844c79a2ad5b748342e1cb9d716f8b52dbb902513f2e",
    "config.yaml": "6a9f4ca7802f359741b9d09fdfa11f16a1974486886b027195992a5ec1253ca3",
    "lumi_identity.py": "a13043e5ec79675bdd00d5bca4aba6bfcf0311258059253afc8eaf7d4df4cb78",
    "lumi_relay.py": "5fe9a3e729544ccbd9fb47df301bbe4499f03c030f79fc41660128f2180977c6",
}
REQUIRED_BOUNDARIES = ("options", "network", "boot", "protected", "watchdog", "hostname")
OPTIONAL_BOUNDARIES = ("privileged", "full_access", "host_network", "host_pid", "host_ipc",
                       "host_dbus", "devices", "map", "ingress", "ingress_port")
VISIBLE_OPTIONS = {
    "codex_voice": {"authorized_user_ids", "lumi_memory_enabled", "lumi_memory_owner_keys",
                    "lumi_notion_workspace_enabled"},
    "music_alexa": {"app_username", "app_password"},
}


def require(ok, code):
    if not ok:
        raise CheckError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def no_links(path):
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_refused")
    return path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode()


def boundaries(info, app):
    require(isinstance(info, dict) and all(k in info for k in REQUIRED_BOUNDARIES)
            and isinstance(info["options"], dict) and type(info["protected"]) is bool,
            "app_boundaries_unavailable")
    # Supervisor returns an empty options mapping to an unprivileged caller.
    # Key presence establishes visibility without exposing any option value.
    require(app in VISIBLE_OPTIONS and VISIBLE_OPTIONS[app] <= set(info["options"]),
            "app_options_not_visible")
    # Only a digest escapes this function; options and private values are not
    # persisted, printed or placed in a model environment.
    fields = {k: info[k] for k in REQUIRED_BOUNDARIES}
    fields.update({k: {"present": k in info, "value": info.get(k)} for k in OPTIONAL_BOUNDARIES})
    return sha(canonical(fields))


def candidate_files(package, expected_sha):
    path = no_links(package)
    require(re.fullmatch("[0-9a-f]{64}", expected_sha or "") is not None,
            "invalid_package_hash")
    require(path.is_file() and path.stat().st_size <= LIMIT, "invalid_package")
    raw = path.read_bytes()
    require(len(raw) <= LIMIT and sha(raw) == expected_sha, "package_hash_mismatch")
    result = {}
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        require(len(entries) == len(CANDIDATE)
                and {e.filename for e in entries} == {"music_alexa/" + n for n in CANDIDATE}
                and sum(e.file_size for e in entries) <= LIMIT, "invalid_package_inventory")
        for entry in entries:
            require(not entry.is_dir() and not stat.S_ISLNK(entry.external_attr >> 16)
                    and not entry.flag_bits & 1 and 0 < entry.file_size <= preflight.LIMIT,
                    "invalid_package_member")
            name = entry.filename.removeprefix("music_alexa/")
            value = archive.read(entry)
            require(sha(value.replace(b"\r\n", b"\n")) == CANDIDATE[name], "candidate_member_mismatch")
            result[name] = value
    return result


class Ha:
    def __init__(self, *, run=subprocess.run):
        self.run = run
        self.namespace = None

    def _read(self, args, *, timeout=30):
        try:
            result = self.run(["ha", *args, "--raw-json"], capture_output=True,
                              timeout=timeout, check=False)
            require(result.returncode == 0 and len(result.stdout) <= LIMIT, "ha_read_failed")
            value = json.loads(result.stdout, object_pairs_hook=preflight.unique)
            require(isinstance(value, dict) and value.get("result", "ok") == "ok", "ha_read_failed")
            data = value.get("data", value)
            require(isinstance(data, dict), "ha_read_invalid")
            return data
        except CheckError:
            raise
        except Exception:
            raise CheckError("ha_read_failed") from None

    def info(self, slug):
        if self.namespace is not None:
            return self._read([self.namespace, "info", slug])
        for namespace in ("apps", "addons"):
            try:
                info = self._read([namespace, "info", slug])
            except CheckError:
                continue
            self.namespace = namespace
            return info
        raise CheckError("ha_info_unavailable")

    def confirm_contract(self):
        require(self.namespace in ("apps", "addons"), "namespace_not_proven")
        try:
            result = self.run(["ha", self.namespace, "update", "--help"],
                              capture_output=True, timeout=30, check=False)
            text = result.stdout.decode("utf-8", errors="replace")
            require(result.returncode == 0 and len(text) <= 131072
                    and all(s in text for s in ("update", "--raw-json", "--no-progress")),
                    "update_contract_unverified")
        except CheckError:
            raise
        except Exception:
            raise CheckError("update_contract_unverified") from None

    def backup(self, slug):
        require(re.fullmatch("[0-9a-f]{8}", slug or "") is not None, "invalid_backup_slug")
        data = self._read(["backups", "info", slug])
        require(data.get("slug") == slug and isinstance(data.get("addons"), list), "backup_not_verified")
        matches = [a for a in data["addons"] if isinstance(a, dict) and a.get("slug") == MUSIC]
        require(len(matches) == 1 and matches[0].get("version") == "0.1.9", "backup_not_verified")
        return {"slug": slug, "music_version": "0.1.9"}

    def mutate(self, operation):
        # No fallback, retry, options setter or update of any other app exists.
        require(self.namespace in ("apps", "addons"), "namespace_not_proven")
        args = (["ha", "store", "reload", "--raw-json"] if operation == "reload" else
                ["ha", self.namespace, "update", MUSIC, "--raw-json", "--no-progress"]
                if operation == "update" else None)
        require(args is not None, "mutation_refused")
        try:
            result = self.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=900 if operation == "update" else 300, check=False)
            require(result.returncode == 0 and len(result.stdout) <= 32768, "mutation_outcome_uncertain")
            value = json.loads(result.stdout, object_pairs_hook=preflight.unique)
            require(isinstance(value, dict) and value.get("result") == "ok", "mutation_outcome_uncertain")
        except Exception:
            raise CheckError("mutation_outcome_uncertain") from None


class Journal:
    def __init__(self, path):
        self.path = no_links(path)

    def create(self):
        require(not self.path.exists(), "journal_exists_use_status")
        self.path.mkdir(mode=0o700)

    def save(self, name, value, *, binary=False):
        require(Path(name).name == name, "invalid_journal_name")
        target = no_links(self.path / name)
        raw = value if binary else canonical(value)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        self._sync_directory()

    def load(self, name, *, binary=False):
        require(self.path.is_dir(), "journal_missing_or_invalid")
        if os.name == "posix":
            require(stat.S_IMODE(self.path.stat().st_mode) == 0o700, "journal_not_private")
        path = no_links(self.path / name)
        require(path.is_file() and path.stat().st_size <= LIMIT, "journal_missing_or_invalid")
        if os.name == "posix":
            require(stat.S_IMODE(path.stat().st_mode) == 0o600, "journal_not_private")
        raw = path.read_bytes()
        return raw if binary else json.loads(raw, object_pairs_hook=preflight.unique)

    def exists(self, name):
        return no_links(self.path / name).exists()

    def _sync_directory(self):
        if os.name == "posix":
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


class Release:
    def __init__(self, source_dir, manifest, payload, package_sha, journal_dir, *, ha=None):
        self.root = no_links(source_dir).resolve(strict=True)
        self.manifest, self.payload, self.package_sha = copy.deepcopy(manifest), payload, package_sha
        require(sha(canonical(manifest)) == BASELINE_MANIFEST_SHA, "baseline_manifest_binding_mismatch")
        self.ha = ha if ha is not None else Ha()
        self.journal = Journal(journal_dir)
        require(self.root not in self.journal.path.parents and self.journal.path != self.root,
                "journal_inside_sources")
        require(set(payload) == set(CANDIDATE) and all(
            sha(raw.replace(b"\r\n", b"\n")) == CANDIDATE[name] for name, raw in payload.items()),
            "candidate_member_mismatch")
        self.target_files = copy.deepcopy(manifest["files"])
        self.target_files["music_alexa"].update({name: sha(raw) for name, raw in payload.items()})

    def current(self, *, music_version="0.1.9", require_started=True):
        result = {}
        for app, (slug, expected) in preflight.APPS.items():
            info = self.ha.info(slug)
            version = music_version if app == "music_alexa" else expected
            require(info.get("version") == version
                    and (not require_started or info.get("state") == "started"), "app_state_mismatch")
            result[app] = {"version": version, "boundaries": boundaries(info, app)}
        return result

    def audit(self, patched=False):
        files = self.target_files if patched else self.manifest["files"]
        missing, changed, extra = preflight.inspect_sources(self.root, files)
        require(not missing and not changed and not extra, "source_inventory_mismatch")

    def plan(self):
        self.audit()
        self.current()
        return {"read_only": True, "baseline_match": True, "codex_unchanged": True,
                "music_from": "0.1.9", "music_to": TARGET_VERSION,
                "changed_public_files": sorted(CANDIDATE), "no_install_or_restart": True}

    def prepare(self, backup_slug):
        require(not self.journal.path.exists(), "journal_exists_use_status")
        self.plan()
        self.ha.confirm_contract()
        backup = self.ha.backup(backup_slug)
        current = self.current()
        self.audit()
        old = {name: (self.root / "music_alexa" / name).read_bytes()
               for name in CANDIDATE if name in self.manifest["files"]["music_alexa"]}
        self.journal.create()
        self.journal.save("prepare-intent.json", {"package_sha": self.package_sha,
                            "source_root": str(self.root), "namespace": self.ha.namespace})
        for name, raw in old.items():
            self.journal.save("backup-" + name + ".bin", raw, binary=True)
        self.audit()
        require(self.current() == current, "app_boundaries_changed")
        self.journal.save("prepared.json", {"package_sha": self.package_sha,
                          "baseline_manifest_sha": sha(canonical(self.manifest)), "apps": current,
                          "backup": backup, "old_files": {n: sha(v) for n, v in old.items()}})
        return {"prepared": True, "sources_changed": False, "backup_verified": True}

    def bound(self):
        value = self.journal.load("prepared.json")
        intent = self.journal.load("prepare-intent.json")
        require(value.get("package_sha") == self.package_sha
                and value.get("baseline_manifest_sha") == sha(canonical(self.manifest))
                and intent.get("package_sha") == self.package_sha
                and intent.get("source_root") == str(self.root)
                and intent.get("namespace") in ("apps", "addons"), "journal_binding_mismatch")
        expected_names = set(CANDIDATE) - {"lumi_identity.py"}
        require(set(value.get("old_files", {})) == expected_names, "journal_backup_mismatch")
        for name, digest in value["old_files"].items():
            raw = self.journal.load("backup-" + name + ".bin", binary=True)
            require(sha(raw) == digest and self.manifest["files"]["music_alexa"][name] in {
                digest, sha(raw.replace(b"\r\n", b"\n"))}, "journal_backup_mismatch")
        return value

    def _preserved(self, saved, *, music_version="0.1.9", require_started=True):
        now = self.current(music_version=music_version, require_started=require_started)
        expected = copy.deepcopy(saved["apps"])
        expected["music_alexa"]["version"] = music_version
        require(now == expected, "app_boundaries_changed")

    def stage(self):
        saved = self.bound()
        self.audit()
        self._preserved(saved)
        require(not self.journal.exists("stage-intent.json"), "stage_already_attempted_use_status")
        self.ha.confirm_contract()
        require(self.ha.namespace == self.journal.load("prepare-intent.json")["namespace"], "namespace_changed")
        require(self.ha.backup(saved["backup"]["slug"]) == saved["backup"], "backup_not_verified")
        self.journal.save("stage-intent.json", {"package_sha": self.package_sha})
        for name, raw in sorted(self.payload.items()):
            target = no_links(self.root / "music_alexa" / name)
            old_digest = saved["old_files"].get(name)
            if old_digest is None:
                require(not target.exists(), "new_file_already_exists")
            else:
                require(target.is_file() and sha(target.read_bytes()) == old_digest, "source_changed_before_replace")
            self.journal.save("stage-" + name + "-intent.json", {"candidate_sha": sha(raw)})
            temp = no_links(target.with_name(".lumi-identity-" + name + ".tmp"))
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if old_digest is None:
                os.link(temp, target)  # Exclusive, atomic creation of the new module.
                temp.unlink()
            else:
                os.replace(temp, target)
            if os.name == "posix":
                parent_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            self.journal.save("stage-" + name + "-ack.json", {"candidate_sha": sha(raw)})
        self.audit(True)
        self._preserved(saved)
        self.journal.save("stage-ack.json", {"package_sha": self.package_sha})

    def submit(self):
        saved = self.bound()
        self.audit(True)
        self._preserved(saved)
        require(self.journal.load("stage-ack.json") == {"package_sha": self.package_sha}, "stage_not_confirmed")
        require(not self.journal.exists("reload-intent.json") and not self.journal.exists("update-intent.json"),
                "submission_already_attempted_use_status")
        self.ha.confirm_contract()
        require(self.ha.namespace == self.journal.load("prepare-intent.json")["namespace"], "namespace_changed")
        self.journal.save("reload-intent.json", {"package_sha": self.package_sha})
        self.ha.mutate("reload")
        self.journal.save("reload-ack.json", {"package_sha": self.package_sha})
        self.audit(True)
        self._preserved(saved)
        require(self.ha.info(MUSIC).get("version_latest") == TARGET_VERSION, "target_catalog_not_confirmed")
        self.journal.save("update-intent.json", {"package_sha": self.package_sha})
        self.ha.mutate("update")
        self.journal.save("update-ack.json", {"package_sha": self.package_sha})
        self.audit(True)
        self._preserved(saved, music_version=TARGET_VERSION)
        self.journal.save("verified.json", {"package_sha": self.package_sha})
        return {"verified": True, "codex_unchanged": True, "options_preserved": True,
                "music_version": TARGET_VERSION, "speech_tested": False}

    def apply(self, backup_slug):
        # A second apply never repeats even a partially completed stage/submit.
        self.prepare(backup_slug)
        self.stage()
        return self.submit()

    def status(self):
        saved = self.bound()
        for name in ("stage", "reload", "update", "verified"):
            marker = name + ".json" if name == "verified" else name + "-ack.json"
            if self.journal.exists(marker):
                require(self.journal.load(marker) == {"package_sha": self.package_sha}, "journal_binding_mismatch")
        files_ok = True
        try:
            self.audit(self.journal.exists("stage-ack.json"))
        except CheckError:
            files_ok = False
        info = {app: self.ha.info(slug) for app, (slug, _) in preflight.APPS.items()}
        require(all(isinstance(x.get("version"), str)
                    and re.fullmatch(r"\d+\.\d+\.\d+", x["version"])
                    and x.get("state") in {"started", "stopped", "starting", "stopping", "error", "unknown"}
                    for x in info.values()), "ha_info_invalid")
        result = {"read_only": True, "source_verified": files_ok,
                  "apps": {app: {"version": x.get("version"), "state": x.get("state")} for app, x in info.items()},
                  "options_preserved": all(boundaries(info[a], a) == saved["apps"][a]["boundaries"] for a in info),
                  "reload_acknowledged": self.journal.exists("reload-ack.json"),
                  "update_acknowledged": self.journal.exists("update-ack.json"),
                  "verified": self.journal.exists("verified.json"), "no_retry_performed": True}
        result["outcome_uncertain"] = any(self.journal.exists(op + "-intent.json")
                                          and not self.journal.exists(op + "-ack.json")
                                          for op in ("stage", "reload", "update"))
        # A marker is not a substitute for current source/runtime verification.
        result["verified"] = (result["verified"] and files_ok and result["options_preserved"]
                              and not result["outcome_uncertain"] and all(
                                  x.get("state") == "started" and x.get("version") ==
                                  (TARGET_VERSION if a == "music_alexa" else "0.1.14") for a, x in info.items()))
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "prepare", "stage", "submit", "apply", "status"), nargs="?", default="plan")
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("baseline.json"))
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--package-sha256", required=True)
    parser.add_argument("--journal-dir", type=Path, required=True)
    parser.add_argument("--backup-slug")
    args = parser.parse_args(argv)
    try:
        payload = candidate_files(args.package, args.package_sha256)
        release = Release(args.source_dir, preflight.read_manifest(args.manifest), payload,
                          args.package_sha256, args.journal_dir)
        if args.operation in ("prepare", "stage", "submit", "apply"):
            require(os.name == "posix" and os.geteuid() == 0, "ha_server_root_required")
        result = (getattr(release, args.operation)(args.backup_slug)
                  if args.operation in ("prepare", "apply") else getattr(release, args.operation)())
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, CheckError) else "release_stopped"
        print(json.dumps({"ok": False, "error": code, "no_automatic_retry": True}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
