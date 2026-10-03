"""Validation and compatibility helpers for household profiles."""

import copy
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from . import budget

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)
_SECRET_KEY = re.compile(
    r"password|passwd|secret|token|api.?key|access.?key|private.?key|credential|"
    r"(?<![a-z])(?:pin|tan)(?![a-z])|auth|bearer|session",
    re.IGNORECASE,
)
_ACCOUNT_KINDS = {"CHECKING", "SAVINGS", "CREDIT_CARD", "DEPOT"}
_CURRENCIES = {"EUR"}
_TEMPLATE_NAMES = ("individual", "couple-shared", "couple-separate", "household-shared")


def _object(value, fields, name):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} has invalid fields")


def _id(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{name} must be a stable ASCII identifier")
    return value


def _label(value, name, maximum=120):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f"{name} is invalid")
    return value.strip()


def _decimal(value, name, *, positive=False, signed=False,
             maximum=Decimal("999999999999.99"), quantum=Decimal("0.01")):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be decimal text") from exc
    if (not parsed.is_finite() or (parsed <= 0 if positive else (parsed < 0 and not signed))
            or abs(parsed) > maximum or parsed != parsed.quantize(quantum)):
        raise ValueError(f"{name} is outside the supported range")
    return format(parsed, ".4f" if quantum == Decimal("0.0001") else ".2f")


def _reject_secrets(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = key.casefold().replace("_", "") if isinstance(key, str) else ""
            if normalized == "value" or (isinstance(key, str) and _SECRET_KEY.search(key)):
                raise ValueError("profile contains a prohibited secret-like field")
            _reject_secrets(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_secrets(nested)


def validate_profile(value):
    """Validate and return a canonical schema-version-2 household profile."""
    _reject_secrets(value)
    fields = {"schema_version", "household_id", "name", "people", "accounts",
              "categories", "budget", "goals", "providers", "database"}
    _object(value, fields, "profile")
    if value["schema_version"] != 2:
        raise ValueError("profile schema_version must be 2")
    if value["database"] != "finance.sqlite":
        raise ValueError("profile database must be finance.sqlite")
    household_id = _id(value["household_id"], "household_id")
    name = _label(value["name"], "name")
    people = value["people"]
    if not isinstance(people, list) or not 1 <= len(people) <= 10:
        raise ValueError("profile people must contain 1 to 10 people")
    checked_people = []
    person_ids, labels = set(), set()
    for raw in people:
        _object(raw, {"id", "label"}, "person")
        person_id = _id(raw["id"], "person id")
        if person_id.casefold() == "joint":
            raise ValueError("person id JOINT is reserved")
        label = _label(raw["label"], "person label", 80)
        if person_id in person_ids or label.casefold() in labels:
            raise ValueError("person ids and labels must be unique")
        person_ids.add(person_id)
        labels.add(label.casefold())
        checked_people.append({"id": person_id, "label": label})

    accounts = value["accounts"]
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 100:
        raise ValueError("profile accounts must contain 1 to 100 accounts")
    checked_accounts, account_ids, opening_dates = [], set(), set()
    for raw in accounts:
        _object(raw, {"id", "display_name", "institution", "kind", "currency",
                      "opening", "opening_date", "shares"}, "account")
        account_id = _id(raw["id"], "account id")
        if account_id in account_ids:
            raise ValueError("account ids must be unique")
        account_ids.add(account_id)
        kind, currency = raw["kind"], raw["currency"]
        if kind not in _ACCOUNT_KINDS or currency not in _CURRENCIES:
            raise ValueError("account kind or currency is unsupported")
        try:
            opening_date = date.fromisoformat(raw["opening_date"])
        except (TypeError, ValueError) as exc:
            raise ValueError("account opening_date must use YYYY-MM-DD") from exc
        if opening_date.isoformat() != raw["opening_date"]:
            raise ValueError("account opening_date must use YYYY-MM-DD")
        opening_dates.add(opening_date)
        shares = raw["shares"]
        if not isinstance(shares, dict) or not 1 <= len(shares) <= len(people):
            raise ValueError("account shares must reference one or more people")
        checked_shares = {}
        for person_id, share in shares.items():
            if person_id not in person_ids:
                raise ValueError("account share references an unknown person")
            checked_shares[person_id] = _decimal(
                share, "account share", positive=True, maximum=Decimal("1.00"),
                quantum=Decimal("0.0001"))
        if sum((Decimal(item) for item in checked_shares.values()), Decimal()) != 1:
            raise ValueError("account shares must sum to 1.0000")
        checked_accounts.append({
            "id": account_id,
            "display_name": _label(raw["display_name"], "account display_name"),
            "institution": _label(raw["institution"], "account institution"),
            "kind": kind,
            "currency": currency,
            "opening": _decimal(raw["opening"], "account opening", signed=True),
            "opening_date": opening_date.isoformat(),
            "shares": checked_shares,
        })
    if len(opening_dates) != 1:
        raise ValueError("profile accounts must share one opening_date")

    categories = value["categories"]
    if (not isinstance(categories, list) or not categories
            or any(not isinstance(item, str) or not item.strip() for item in categories)
            or len({item.casefold() for item in categories}) != len(categories)):
        raise ValueError("profile categories must be nonempty and unique")
    checked_categories = [_label(item, "category", 80) for item in categories]
    if not isinstance(value["budget"], dict):
        raise TypeError("profile budget must be a valid plan")
    checked_budget = budget._validate_plan(value["budget"])
    checking_ids = {account["id"] for account in checked_accounts
                    if account["kind"] == "CHECKING"}
    for account_id in checked_budget.get("liquidity_accounts", []):
        if account_id not in checking_ids:
            raise ValueError("budget liquidity account must reference a checking account")
    for item in checked_budget["items"]:
        if item.get("account_id") not in (None, *checking_ids):
            raise ValueError("budget item must reference a checking account")
    for cycle in checked_budget.get("payday_cycles", []):
        if cycle["account_id"] not in checking_ids:
            raise ValueError("budget payday cycle must reference a checking account")
    for transfer in checked_budget.get("pending_transfers", []):
        if (transfer["from_account_id"] not in checking_ids
                or transfer["to_account_id"] not in checking_ids):
            raise ValueError("budget transfer must reference checking accounts")
    split_ids = {entry["party_id"] for entry in checked_budget.get("household_split", [])}
    if split_ids - person_ids:
        raise ValueError("budget household split references an unknown person")
    if checked_budget.get("actual_allocations"):
        raise ValueError("initial budget cannot reference transactions")

    goals = value["goals"]
    if not isinstance(goals, list) or len(goals) > 100:
        raise ValueError("profile goals must contain at most 100 goals")
    checked_goals, goal_ids = [], set()
    for raw in goals:
        _object(raw, {"id", "label", "target_amount", "currency"}, "goal")
        goal_id = _id(raw["id"], "goal id")
        if goal_id in goal_ids or raw["currency"] not in _CURRENCIES:
            raise ValueError("goal ids must be unique and currency supported")
        goal_ids.add(goal_id)
        checked_goals.append({"id": goal_id, "label": _label(raw["label"], "goal label"),
                              "target_amount": _decimal(
                                  raw["target_amount"], "goal target_amount", positive=True),
                              "currency": raw["currency"]})
    providers = value["providers"]
    if not isinstance(providers, list) or len(providers) > 20:
        raise ValueError("profile providers must be a list of optional descriptors")
    checked_providers, provider_ids = [], set()
    for raw in providers:
        if not isinstance(raw, dict) or set(raw) - {"id", "label", "enabled", "kind"}:
            raise ValueError("provider descriptor has invalid fields")
        provider_id = _id(raw.get("id"), "provider id")
        if provider_id in provider_ids:
            raise ValueError("provider ids must be unique")
        provider_ids.add(provider_id)
        descriptor = {"id": provider_id}
        if "label" in raw:
            descriptor["label"] = _label(raw["label"], "provider label")
        if "kind" in raw:
            descriptor["kind"] = _id(raw["kind"], "provider kind")
        if "enabled" in raw:
            if raw["enabled"] is not False:
                raise ValueError("provider activation is not part of initialization")
            descriptor["enabled"] = raw["enabled"]
        checked_providers.append(descriptor)
    return {"schema_version": 2, "household_id": household_id, "name": name,
            "people": checked_people, "accounts": checked_accounts,
            "categories": checked_categories, "budget": checked_budget,
            "goals": checked_goals, "providers": checked_providers,
            "database": "finance.sqlite"}


def normalize_profile(value):
    """Return v2 profile data; legacy v1 identities remain stable and on disk untouched."""
    if isinstance(value, dict) and value.get("schema_version") == 1:
        _reject_secrets(value)
        if (set(value) != {"schema_version", "name", "template", "people", "categories",
                           "database", "providers"}
                or value.get("database") != "finance.sqlite"
                or value.get("template") not in _TEMPLATE_NAMES[:3]
                or not isinstance(value.get("people"), list)):
            raise ValueError("legacy profile is invalid")
        labels = value["people"]
        if len(labels) not in (1, 2) or ((len(labels) == 1) != (value["template"] == "individual")):
            raise ValueError("legacy profile people are invalid")
        people = [{"id": "ANDREAS", "label": _label(labels[0], "person label", 80)}]
        if len(labels) == 2:
            people.append({"id": "ERLENE", "label": _label(labels[1], "person label", 80)})
        if (not isinstance(value.get("categories"), list)
                or not isinstance(value.get("providers"), list)):
            raise ValueError("legacy profile lists are invalid")
        return {"schema_version": 2, "household_id": "HOUSEHOLD", "name": _label(value["name"], "name"),
                "people": people, "legacy_template": value["template"], "accounts": [],
                "categories": [_label(item, "category", 80) for item in value["categories"]],
                "budget": None, "goals": [], "providers": copy.deepcopy(value["providers"]),
                "database": "finance.sqlite"}
    return validate_profile(value)


def template_profile(template, profile_name="Mein Haushalt"):
    if template not in _TEMPLATE_NAMES:
        raise ValueError("Unbekannte Vorlage.")
    count = {"individual": 1, "couple-shared": 2, "couple-separate": 2,
             "household-shared": 3}[template]
    people = [{"id": f"person-{index}", "label": f"Person {index}"}
              for index in range(1, count + 1)]
    if template == "individual":
        accounts = [{"id": "account-1", "display_name": "Girokonto Person 1",
                     "institution": "SYNTHETIC", "kind": "CHECKING", "currency": "EUR",
                     "opening": "0.00", "opening_date": "2026-01-01",
                     "shares": {"person-1": "1.00"}}]
    elif template == "couple-separate":
        accounts = [{"id": f"account-{index}", "display_name": f"Girokonto Person {index}",
                     "institution": "SYNTHETIC", "kind": "CHECKING", "currency": "EUR",
                     "opening": "0.00", "opening_date": "2026-01-01",
                     "shares": {f"person-{index}": "1.00"}} for index in (1, 2)]
    else:
        accounts = [{"id": "account-shared", "display_name": "Gemeinsames Girokonto",
                     "institution": "SYNTHETIC", "kind": "CHECKING", "currency": "EUR",
                     "opening": "0.00", "opening_date": "2026-01-01",
                     "shares": {person["id"]: format(Decimal(1) / count, ".4f")
                                for person in people[:-1]}}]
        accounts[0]["shares"][people[-1]["id"]] = format(
            Decimal(1) - sum((Decimal(value) for value in accounts[0]["shares"].values()), Decimal()), ".4f")
    profile = {"schema_version": 2, "household_id": "household-1", "name": profile_name,
               "people": people, "accounts": accounts,
               "categories": ["Einkommen", "Wohnen", "Lebensmittel", "Mobilität", "Rücklage"],
               "budget": {"title": "Budgetvorlage", "start_month": "2026-01",
                          "notes": "Synthetische Beispielbeträge; alle Positionen unbestätigt.",
                          "items": [
                              {"id": f"example-{number}", "label": label, "kind": kind,
                               "amount": amount, "start_month": "2026-01", "end_month": None,
                               "source": "synthetic:onboarding", "confirmed": False}
                              for number, (label, kind, amount) in enumerate(zip(
                                  ("Einkommen", "Wohnen", "Lebensmittel", "Mobilität", "Rücklage"),
                                  ("income", "fixed", "variable", "variable", "fixed"),
                                  ("3000.00", "1100.00", "400.00", "200.00", "300.00")), 1)]},
               "goals": [{"id": "goal-1", "label": "Synthetisches Sparziel",
                          "target_amount": "1000.00", "currency": "EUR"}],
               "providers": [], "database": "finance.sqlite"}
    return validate_profile(profile)
