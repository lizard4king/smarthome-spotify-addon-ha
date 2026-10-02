"""JSON persistence with validation, recovery, and atomic replacement."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from smarthome.models import SmartHomeState, default_state


class StateStore:
    """Load and safely persist the complete smart-home state."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.last_recovery_backup: Path | None = None

    def load(self) -> SmartHomeState:
        """Load state or create defaults when the file is missing or invalid."""

        if not self.path.exists():
            state = default_state()
            self.save(state)
            return state

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Der JSON-Wurzelwert muss ein Objekt sein.")
            return SmartHomeState.from_dict(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            self._backup_corrupt_file()
            state = default_state()
            self.save(state)
            return state

    def save(self, state: SmartHomeState) -> None:
        """Persist state using a temporary file and atomic replacement."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                json.dump(state.to_dict(), temporary_file, ensure_ascii=False, indent=2)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
                temporary_path = Path(temporary_file.name)
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def _backup_corrupt_file(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{timestamp}")
        try:
            os.replace(self.path, backup)
            self.last_recovery_backup = backup
        except OSError:
            self.last_recovery_backup = None

