"""Build an exclusive deterministic ZIP of the five reviewed Music files."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import zipfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("lumi_identity_release", HERE / "install.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def build(root):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, expected in sorted(release.CANDIDATE.items()):
            path = release.no_links(Path(root) / "music_alexa" / name)
            release.require(path.is_file() and path.stat().st_size <= release.preflight.LIMIT,
                            "candidate_source_invalid")
            raw = path.read_bytes().replace(b"\r\n", b"\n")
            release.require(hashlib.sha256(raw).hexdigest() == expected, "candidate_member_mismatch")
            entry = zipfile.ZipInfo("music_alexa/" + name, (2026, 10, 26, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, raw)
    return output.getvalue()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=HERE.parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw = build(args.repo_root)
        path = release.no_links(args.output)
        with path.open("xb") as stream:
            stream.write(raw)
        digest = hashlib.sha256(raw).hexdigest()
        release.candidate_files(path, digest)
    except Exception:
        print(json.dumps({"ok": False, "error": "package_build_failed_no_overwrite"}))
        return 1
    print(json.dumps({"ok": True, "package_sha256": digest, "members": 5}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
