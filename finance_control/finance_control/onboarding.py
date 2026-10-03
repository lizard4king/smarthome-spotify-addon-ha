"""Create a local household workspace from a template or validated profile."""

import json
import os
import shutil
import tempfile
from pathlib import Path

from . import budget
from .core import Store
from .household import _TEMPLATE_NAMES, template_profile, validate_profile
from .import_preview import outside_repository

TEMPLATES = _TEMPLATE_NAMES


def load_profile_file(path):
    """Read a user-selected profile only from outside a Git checkout."""
    source = outside_repository(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return validate_profile(value)
    except Exception:  # noqa: BLE001 - do not leak parser details or a private path
        # Do not echo profile content or its private source path.
        raise ValueError("Profil ungültig oder nicht lesbar.") from None


def initialize(data_directory, template=None, profile_name="Mein Haushalt", *, profile=None):
    """Create the complete database and profile before publishing the new directory."""
    if (template is None) == (profile is None):
        raise ValueError("Genau eine Vorlage oder ein Profil ist erforderlich.")
    if profile is None:
        profile = template_profile(template, profile_name)
    else:
        profile = validate_profile(profile)
    if not isinstance(data_directory, (str, os.PathLike)) or not str(data_directory).strip():
        raise ValueError("Ein expliziter Datenordner ist erforderlich.")
    target = outside_repository(data_directory)
    if target.exists() or target.is_symlink():
        raise FileExistsError("Der Datenordner muss neu sein.")
    parent = target.parent
    if not parent.is_dir():
        raise ValueError("Der Elternordner muss vorhanden sein.")
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=parent))
    try:
        store = Store(temporary / "finance.sqlite")
        try:
            for account in profile["accounts"]:
                shares = account["shares"]
                owner = next(iter(shares)) if len(shares) == 1 else "JOINT"
                store.add_account(account["id"], owner, shares, account["opening"],
                                  account["opening_date"], institution=account["institution"],
                                  kind=account["kind"], currency=account["currency"],
                                  display_name=account["display_name"],
                                  household=profile["household_id"])
            saved = budget.save(store, {"revision": 0, "plan": profile["budget"]})
        finally:
            store.close()
        (temporary / "profile.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (temporary / "HINWEISE.txt").write_text(
            "Dieser Ordner enthält künftig persönliche Finanzdaten. Lokal und außerhalb von Git aufbewahren.\n"
            "Sicherungen nur konsistent erstellen und geschützt ablegen; die aktive SQLite-Datei nicht synchronisieren.\n"
            "Zugangsdaten und TAN niemals hier speichern. Bankzugriffe sind nicht aktiviert.\n",
            encoding="utf-8")
        if target.exists() or target.is_symlink():
            raise FileExistsError("Der Datenordner muss neu sein.")
        temporary.rename(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"data_directory": str(target), "database": str(target / "finance.sqlite"),
            "profile": str(target / "profile.json"),
            "template": template, "accounts": len(profile["accounts"]),
            "budget_revision": saved["revision"], "providers": profile["providers"]}
