"""Small, fail-closed Google Drive adapter for repository-external data.

Credentials are read only through the public ``invoice_mail_archive.accounts``
API.  The Google client imports are deliberately late: importing Finance
Control does not require either optional package and performs no I/O.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:|[\\/])")


class DriveApiError(RuntimeError):
    """A stable, secret-free error returned by the adapter."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DriveFile:
    relative_path: str
    size_bytes: int
    md5_checksum: str


def _relative_path(value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or _ABSOLUTE_PATH.match(value):
        raise DriveApiError("drive_path_must_be_relative")
    value = value.replace("\\", "/")
    if not value and allow_empty:
        return ""
    if not value or value.endswith("/"):
        raise DriveApiError("drive_path_invalid")
    parts = PurePosixPath(value).parts
    if not parts or any(part in (".", "..", "") for part in parts):
        raise DriveApiError("drive_path_invalid")
    return "/".join(parts)


def _quote_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _default_service_factory(account_id: str, vault_path, key_path):
    """Return a factory so neither the vault nor Google is touched eagerly."""

    def create_service():
        try:
            from invoice_mail_archive.accounts import configured_vault
        except ImportError:
            raise DriveApiError("drive_account_api_unavailable") from None
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError:
            raise DriveApiError("drive_client_unavailable") from None

        try:
            profile = configured_vault(vault_path, key_path).get(account_id)
            credential_info = json.loads(profile.password)
            if not isinstance(credential_info, dict):
                raise TypeError
            credentials = Credentials.from_authorized_user_info(
                credential_info, scopes=[DRIVE_SCOPE]
            )
            return build("drive", "v3", credentials=credentials, cache_discovery=False)
        except DriveApiError:
            raise
        except Exception:  # noqa: BLE001 - redact all supplier/provider errors
            # Vault paths, account contents, tokens and provider errors must not
            # become part of an application-facing exception.
            raise DriveApiError("drive_credentials_unavailable") from None

    return create_service


def _default_media_factory(payload: bytes):
    try:
        from googleapiclient.http import MediaIoBaseUpload
    except ImportError:
        raise DriveApiError("drive_client_unavailable") from None
    return MediaIoBaseUpload(
        io.BytesIO(payload), mimetype="application/octet-stream", resumable=False
    )


def _default_file_media_factory(source: Path):
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        raise DriveApiError("drive_client_unavailable") from None
    return MediaFileUpload(
        str(source),
        mimetype="application/octet-stream",
        chunksize=8 * 1024 * 1024,
        resumable=True,
    )


class GoogleDriveApi:
    """Access files below one configured Drive folder.

    ``service`` and ``media_factory`` are dependency-injection hooks for
    synthetic tests. Production credentials still enter exclusively through
    ``invoice_mail_archive.accounts.configured_vault``. The default account is
    ``google_drive``; vault paths default to ``IMA_ACCOUNT_VAULT`` and
    ``IMA_ACCOUNT_KEY_FILE`` via that public API.
    """

    def __init__(
        self,
        root_folder_id: str,
        *,
        account_id: str = "google_drive",
        vault_path=None,
        key_path=None,
        service=None,
        service_factory: Callable[[], object] | None = None,
        media_factory: Callable[[bytes], object] | None = None,
        file_media_factory: Callable[[Path], object] | None = None,
    ) -> None:
        if not isinstance(root_folder_id, str) or not root_folder_id.strip():
            raise DriveApiError("drive_root_folder_required")
        if service is not None and service_factory is not None:
            raise DriveApiError("drive_service_configuration_invalid")
        self._root_folder_id = root_folder_id.strip()
        self._service = service
        self._service_factory = service_factory or _default_service_factory(
            account_id, vault_path, key_path
        )
        self._media_factory = media_factory or _default_media_factory
        self._file_media_factory = file_media_factory or _default_file_media_factory
        self._folder_cache = {"": self._root_folder_id}

    @property
    def service(self):
        if self._service is None:
            try:
                self._service = self._service_factory()
            except DriveApiError:
                raise
            except Exception:  # noqa: BLE001 - redact injected provider errors
                raise DriveApiError("drive_service_unavailable") from None
        return self._service

    def _execute(self, request, code: str):
        try:
            return request.execute()
        except DriveApiError:
            raise
        except Exception:  # noqa: BLE001 - redact Drive client errors
            raise DriveApiError(code) from None

    def _children(self, parent_id: str, name: str | None = None) -> list[dict]:
        terms = [f"'{_quote_query(parent_id)}' in parents", "trashed = false"]
        if name is not None:
            terms.insert(0, f"name = '{_quote_query(name)}'")
        files = []
        page_token = None
        while True:
            arguments = {
                "q": " and ".join(terms),
                "fields": (
                    "nextPageToken, incompleteSearch, "
                    "files(id, name, size, md5Checksum, mimeType)"
                ),
                "pageSize": 1000 if name is None else 2,
            }
            if page_token:
                arguments["pageToken"] = page_token
            answer = self._execute(
                self.service.files().list(**arguments), "drive_list_failed"
            )
            if answer.get("incompleteSearch"):
                raise DriveApiError("drive_list_incomplete")
            files.extend(answer.get("files") or [])
            if name is not None and len(files) > 1:
                raise DriveApiError("drive_duplicate_name")
            page_token = answer.get("nextPageToken")
            if not page_token:
                return files

    def _child(self, parent_id: str, name: str) -> dict | None:
        found = self._children(parent_id, name)
        return found[0] if found else None

    def _folder_id(self, relative: str, *, create: bool = False) -> str:
        if not relative:
            return self._root_folder_id
        current = self._root_folder_id
        traversed = ""
        for segment in relative.split("/"):
            traversed = f"{traversed}/{segment}".strip("/")
            if traversed in self._folder_cache:
                current = self._folder_cache[traversed]
                continue
            child = self._child(current, segment)
            if child is None:
                if not create:
                    raise DriveApiError("drive_folder_not_found")
                child = self._execute(
                    self.service.files().create(
                        body={
                            "name": segment,
                            "mimeType": FOLDER_MIME_TYPE,
                            "parents": [current],
                        },
                        fields="id, name, mimeType",
                    ),
                    "drive_folder_create_failed",
                )
                self._verify_unique_create(current, segment, child.get("id"))
            if child.get("mimeType") != FOLDER_MIME_TYPE:
                raise DriveApiError("drive_path_not_folder")
            child_id = child.get("id")
            if not child_id:
                raise DriveApiError("drive_invalid_response")
            current = child_id
            self._folder_cache[traversed] = current
        return current

    def _resolve_file(self, relative: str, *, create_parents: bool = False):
        clean = _relative_path(relative)
        path = PurePosixPath(clean)
        parent_relative = "" if str(path.parent) == "." else str(path.parent)
        parent_id = self._folder_id(parent_relative, create=create_parents)
        child = self._child(parent_id, path.name)
        if child is not None and child.get("mimeType") == FOLDER_MIME_TYPE:
            raise DriveApiError("drive_path_is_folder")
        return clean, parent_id, child

    def _verify_unique_create(self, parent_id: str, name: str, created_id) -> None:
        if not created_id:
            raise DriveApiError("drive_invalid_response")
        try:
            found = self._children(parent_id, name)
        except DriveApiError as error:
            if error.code == "drive_duplicate_name":
                self._delete_created(created_id)
            raise
        if len(found) != 1 or found[0].get("id") != created_id:
            self._delete_created(created_id)
            raise DriveApiError("drive_created_item_not_unique")

    def _delete_created(self, file_id) -> None:
        try:
            self.service.files().delete(fileId=file_id).execute()
        except Exception:  # noqa: BLE001,S110 - best-effort rollback
            # Preserve the original verification failure and never expose a
            # provider response that might contain request metadata.
            pass

    def list_files(self, relative_folder: str = "") -> tuple[DriveFile, ...]:
        clean = _relative_path(relative_folder, allow_empty=True)
        folder_id = self._folder_id(clean)
        result = []
        names: set[str] = set()
        for item in self._children(folder_id):
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise DriveApiError("drive_invalid_response")
            if name in names:
                raise DriveApiError("drive_duplicate_name")
            names.add(name)
            if item.get("mimeType") == FOLDER_MIME_TYPE:
                continue
            result.append(
                DriveFile(
                    relative_path=f"{clean}/{name}".strip("/"),
                    size_bytes=self._size(item),
                    md5_checksum=str(item.get("md5Checksum") or "").lower(),
                )
            )
        return tuple(sorted(result, key=lambda entry: entry.relative_path))

    def read_bytes(self, relative: str) -> bytes:
        try:
            _, _, item = self._resolve_file(relative)
        except DriveApiError as error:
            if error.code == "drive_folder_not_found":
                raise DriveApiError("drive_file_not_found") from None
            raise
        if item is None:
            raise DriveApiError("drive_file_not_found")
        payload = self._execute(
            self.service.files().get_media(fileId=item["id"]), "drive_read_failed"
        )
        if not isinstance(payload, bytes):
            raise DriveApiError("drive_invalid_response")
        return payload

    def upload_bytes(self, relative: str, payload: bytes) -> DriveFile:
        if not isinstance(payload, bytes):
            raise DriveApiError("drive_upload_requires_bytes")
        try:
            media = self._media_factory(payload)
        except DriveApiError:
            raise
        except Exception:  # noqa: BLE001 - redact media-provider errors
            raise DriveApiError("drive_upload_failed") from None
        return self._upload(
            relative,
            media,
            expected_size=len(payload),
            expected_md5=hashlib.md5(payload).hexdigest(),
        )

    def _upload(
        self,
        relative: str,
        media,
        *,
        expected_size: int,
        expected_md5: str,
    ) -> DriveFile:
        clean, parent_id, existing = self._resolve_file(relative, create_parents=True)
        if existing is not None:
            raise DriveApiError("drive_file_exists")
        name = PurePosixPath(clean).name
        try:
            created = self._execute(
                self.service.files().create(
                    body={"name": name, "parents": [parent_id]},
                    media_body=media,
                    fields="id, name, size, md5Checksum, mimeType",
                ),
                "drive_upload_failed",
            )
        except DriveApiError:
            raise
        except Exception:  # noqa: BLE001 - redact Drive client errors
            raise DriveApiError("drive_upload_failed") from None

        created_id = created.get("id")
        self._verify_unique_create(parent_id, name, created_id)
        actual_size = self._size(created)
        actual_md5 = str(created.get("md5Checksum") or "").lower()
        if actual_size != expected_size or actual_md5 != expected_md5:
            self._delete_created(created_id)
            raise DriveApiError("drive_upload_verification_failed")
        return DriveFile(clean, actual_size, actual_md5)

    def upload_file(self, relative: str, source: str | Path) -> DriveFile:
        try:
            source_path = Path(source)
            if not source_path.is_file():
                raise OSError
            size, checksum = self._file_metadata(source_path)
            media = self._file_media_factory(source_path)
        except DriveApiError:
            raise
        except (OSError, TypeError, ValueError):
            raise DriveApiError("drive_source_unreadable") from None
        except Exception:  # noqa: BLE001 - redact media-provider errors
            raise DriveApiError("drive_upload_failed") from None
        return self._upload(relative, media, expected_size=size, expected_md5=checksum)

    @staticmethod
    def _file_metadata(source: Path) -> tuple[int, str]:
        size = 0
        digest = hashlib.md5()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                size += len(block)
                digest.update(block)
        return size, digest.hexdigest()

    @staticmethod
    def _size(item: dict) -> int:
        try:
            size = int(item.get("size"))
        except (TypeError, ValueError):
            raise DriveApiError("drive_invalid_response") from None
        if size < 0:
            raise DriveApiError("drive_invalid_response")
        return size
