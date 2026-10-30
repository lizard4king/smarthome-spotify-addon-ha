"""Read-only HA source check before the incremental Lumi identity update.

No backups, updates, restarts, option writes or identity-window operations.
Only the two known add-ons and files listed in the public baseline are read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

APPS = {"codex_voice": ("local_codex_voice", "0.1.14"),
        "music_alexa": ("local_music_alexa", "0.1.9")}
LIMIT = 512 * 1024


class CheckError(RuntimeError):
    """Public error code, never provider output or configuration values."""


def unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise CheckError("duplicate_json_key")
        result[key] = value
    return result


def read_manifest(path):
    raw = Path(path).read_bytes()
    if len(raw) > LIMIT:
        raise CheckError("manifest_too_large")
    value = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {"schema", "source_revision", "files"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or not re.fullmatch(r"[0-9a-f]{40}", str(value["source_revision"]))
            or not isinstance(value["files"], dict) or set(value["files"]) != set(APPS)):
        raise CheckError("invalid_manifest")
    for app, entries in value["files"].items():
        if not isinstance(entries, dict) or not 1 <= len(entries) <= 128:
            raise CheckError("invalid_manifest")
        for name, digest in entries.items():
            if not isinstance(name, str):
                raise CheckError("invalid_manifest_path")
            p = PurePosixPath(name)
            if (str(p) != name or p.is_absolute() or ".." in p.parts or "\\" in name
                    or ":" in name or not p.parts or name.startswith(".") and name != ".dockerignore"):
                raise CheckError("invalid_manifest_path")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise CheckError("invalid_manifest_hash")
    return value


def ha_info(slug, *, run=subprocess.run):
    # Namespace fallback is read-only; no mutation is retried under another name.
    for namespace in ("apps", "addons"):
        try:
            result = run(["ha", namespace, "info", slug, "--raw-json"],
                         capture_output=True, timeout=30, check=False)
        except Exception:
            raise CheckError("ha_cli_unavailable") from None
        if result.returncode != 0:
            continue
        try:
            if len(result.stdout) > 2 * 1024 * 1024:
                raise ValueError
            value = json.loads(result.stdout, object_pairs_hook=unique)
            if not isinstance(value, dict) or value.get("result", "ok") != "ok":
                continue
            data = value.get("data", value)
            version, state = data["version"], data["state"]
            if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ValueError
            if state not in {"started", "stopped", "starting", "error", "unknown"}:
                raise ValueError
            # Drop every other field, including options, before returning.
            return {"version": version, "state": state}
        except Exception:
            raise CheckError("ha_info_invalid") from None
    raise CheckError("ha_info_unavailable")


def inspect_sources(root, files):
    root = Path(root).resolve(strict=True)
    missing, changed, extra_count = [], [], 0
    for app, entries in files.items():
        folder = root / app
        if folder.is_symlink() or not folder.is_dir() or folder.resolve() != folder:
            raise CheckError("source_directory_invalid")
        for name, expected in entries.items():
            target = folder.joinpath(*PurePosixPath(name).parts)
            if any(p.is_symlink() for p in (target, *target.parents) if p != root and root in p.parents):
                raise CheckError("source_symlink_refused")
            if not target.exists():
                missing.append(app + "/" + name)
                continue
            if not target.is_file() or target.stat().st_size > LIMIT:
                raise CheckError("source_file_invalid")
            raw = target.read_bytes()
            if len(raw) > LIMIT:
                raise CheckError("source_file_invalid")
            # Public baseline deliberately normalizes CRLF only; no whitespace,
            # syntax, options or configuration semantics are normalized.
            if expected not in {hashlib.sha256(raw).hexdigest(),
                                hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()}:
                changed.append(app + "/" + name)
        for p in folder.rglob("*"):
            relative = p.relative_to(folder)
            if "__pycache__" in relative.parts:
                continue
            if p.is_symlink():
                raise CheckError("source_symlink_refused")
            if p.is_file() and relative.as_posix() not in entries:
                # Unknown files may contain private material: never open them or
                # print their names. Extra source inventory requires inspection.
                extra_count += 1
    return missing, changed, extra_count


def check(root, manifest, *, info=ha_info):
    versions = {app: info(slug) for app, (slug, _) in APPS.items()}
    missing, changed, extra_count = inspect_sources(root, manifest["files"])
    healthy = all(versions[app] == {"version": expected, "state": "started"}
                  for app, (_, expected) in APPS.items())
    return {"read_only": True, "baseline_match": healthy and not missing and not changed and not extra_count,
            "apps": versions, "source_revision": manifest["source_revision"],
            "missing_files": missing, "changed_files": changed, "extra_file_count": extra_count,
            "options_read_into_output": False, "no_install_or_restart": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("baseline.json"))
    args = parser.parse_args(argv)
    try:
        value = check(args.source_dir, read_manifest(args.manifest))
    except Exception as error:
        code = str(error) if isinstance(error, CheckError) else "preflight_failed"
        print(json.dumps({"read_only": True, "baseline_match": False, "error": code,
                          "no_install_or_restart": True}))
        return 1
    print(json.dumps(value, sort_keys=True))
    return 0 if value["baseline_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
