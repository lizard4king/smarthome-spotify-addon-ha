"""Read-only, same-origin local audio; never expose Music Assistant stream URLs.

The configured root must be the same root as the bound filesystem_local provider.
Only POSIX dir-fd traversal is supported: no path component may be a symlink.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePosixPath

from dashboard.music_assistant import MusicAssistantError, _relative_path

AUDIO_TYPES = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
               ".aac": "audio/aac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
               ".wav": "audio/wav", ".aiff": "audio/aiff", ".aif": "audio/aiff"}
CHUNK_SIZE = 64 * 1024


def root_parts(value: str) -> tuple[str, ...]:
    """Validate the absolute POSIX root without resolving any symlinks."""
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or "\\" in value or any(ord(c) < 32 for c in value)
            or any(part in {".", ".."} for part in value.split("/"))):
        raise ValueError("Ungültiger lokaler Musikordner.")
    parts = PurePosixPath(value).parts[1:]
    if not parts:
        raise ValueError("Die Dateisystemwurzel ist kein Musikordner.")
    return parts


class AudioRangeError(ValueError):
    """A single byte range is invalid or outside the file."""


def byte_range(value: str | None, size: int) -> tuple[int, int, int]:
    """Return inclusive start/end and HTTP status; reject multi-range requests."""
    if value is None:
        return 0, size - 1, 200
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value) if len(value) <= 128 else None
    if not match or size <= 0 or not any(match.groups()):
        raise AudioRangeError("Ungültiger Audiobereich.")
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if start >= size or end < start:
            raise AudioRangeError("Audiobereich außerhalb der Datei.")
    else:
        count = int(last)
        if count <= 0:
            raise AudioRangeError("Ungültiger Audiobereich.")
        start, end = max(0, size - count), size - 1
    return start, end, 206


class BrowserAudio:
    def __init__(self, client):
        self.client = client
        self.root = os.getenv("MUSIC_BROWSER_ROOT", "")
        self.enabled = os.getenv("MUSIC_BROWSER_ALLOW_PLAYBACK", "").casefold() == "true"

    def status(self):
        supported = (os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
                     and hasattr(os, "O_DIRECTORY"))
        provider = self.client.library_provider
        valid_provider = isinstance(provider, str) and re.fullmatch(
            r"filesystem_local--[A-Za-z0-9_-]+", provider)
        try:
            root_parts(self.root)
            valid_root = Path(self.root).is_dir() and not Path(self.root).is_symlink()
        except (ValueError, OSError):
            valid_root = False
        available = bool(self.enabled and supported and valid_provider and valid_root
                         and self.client.configured and self.client.valid_config)
        result = {"available": available}
        if not available:
            result["reason"] = "Browserwiedergabe benötigt eine freigegebene lokale Musikquelle auf dem Server."
        return result

    def open_track(self, uri):
        if not self.status()["available"]:
            raise MusicAssistantError("Browserwiedergabe ist nicht eingerichtet.")
        _, local_uri = self.client.resolve_local_track(uri)
        prefix = self.client.library_provider + "://track/"
        if not local_uri.startswith(prefix):
            raise ValueError("Dieser Titel gehört nicht zur lokalen Browser-Musikquelle.")
        # Use the actual item identifier returned by MA, not a URL-decoded user path.
        relative = local_uri[len(prefix):]
        if not _relative_path(relative) or any(part in {"", ".", ".."} for part in relative.split("/")):
            raise ValueError("Ungültiger lokaler Musiktitel.")
        content_type = AUDIO_TYPES.get(Path(relative).suffix.casefold())
        if not content_type:
            raise ValueError("Dieses Audioformat wird im Browser nicht angeboten.")
        directories = []
        file_fd = None
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            # Walk from '/' too: O_NOFOLLOW on a full root string protects only
            # its leaf; each ancestor needs a separate directory-descriptor open.
            directories.append(os.open("/", directory_flags))
            for part in root_parts(self.root):
                directories.append(os.open(part, directory_flags, dir_fd=directories[-1]))
            parts = relative.split("/")
            for part in parts[:-1]:
                directories.append(os.open(part, directory_flags, dir_fd=directories[-1]))
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                              dir_fd=directories[-1])
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise ValueError("Dieser Musiktitel ist keine abspielbare Datei.")
            stream = os.fdopen(file_fd, "rb")
            file_fd = None
            return stream, info.st_size, content_type
        except OSError as exc:
            # Never include the configured root, filenames or OS exception in JSON/logs.
            raise MusicAssistantError("Die lokale Musikdatei ist nicht verfügbar.") from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)
            for descriptor in reversed(directories):
                os.close(descriptor)


def serve_audio(handler, client, uri, *, head=False):
    """Stream a bounded file range without buffering complete audio files."""
    stream, size, content_type = BrowserAudio(client).open_track(uri)
    try:
        try:
            start, end, status_code = byte_range(handler.headers.get("Range"), size)
        except AudioRangeError:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.send_header("Content-Length", "0")
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            return
        remaining = end - start + 1
        handler.send_response(status_code)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(remaining))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "private, no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if status_code == 206:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        handler.end_headers()
        if head:
            return
        stream.seek(start)
        while remaining:
            chunk = stream.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                # A file may have changed after fstat. Never leave a client
                # waiting for the rest of the declared Content-Length.
                handler.close_connection = True
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)
    except OSError:
        # Headers may already have been sent. End this response rather than
        # attempting a second JSON response or logging private OS filenames.
        # This also covers a browser stopping/seeking a previous request.
        handler.close_connection = True
    finally:
        try:
            stream.close()
        except OSError:
            handler.close_connection = True
