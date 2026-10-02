"""Shared path boundary for personal data stored outside Git checkouts."""

from pathlib import Path


class RepositoryPathError(ValueError):
    """Safe diagnostic without the private path."""


def outside_repository(path: Path) -> Path:
    """Resolve symlinks before rejecting paths within any Git checkout."""
    result = Path(path).expanduser().resolve()
    if any((parent / ".git").exists() for parent in (result.parent, *result.parents)):
        raise RepositoryPathError("Persönliche Dateien müssen außerhalb eines Git-Repositorys liegen.")
    return result
