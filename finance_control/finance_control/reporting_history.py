"""Validated reporting policy; source bookings and account ownership stay untouched."""

from datetime import date

from .budget import _month
from .core import valid_identifier, valid_person_id


def validate(value):
    """Validate the optional policy without accepting labels in place of stable IDs."""
    required = {"schema_version", "joint_from_month", "prior_person_id"}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {"available_from_by_account", "balance_account_ids",
                                        "retrospective_actual_years"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not valid_person_id(value["prior_person_id"])):
        raise ValueError("invalid reporting history")
    result = dict(value, joint_from_month=_month(value["joint_from_month"], "joint_from_month"))
    if "retrospective_actual_years" in value:
        years = value["retrospective_actual_years"]
        if (not isinstance(years, list)
                or any(type(year) is not int or not 1 <= year <= 9999 for year in years)
                or len(years) != len(set(years))):
            raise ValueError("invalid retrospective actual years")
        result["retrospective_actual_years"] = list(years)
    if "balance_account_ids" in value:
        account_ids = value["balance_account_ids"]
        if (not isinstance(account_ids, list)
                or any(not valid_identifier(account_id) for account_id in account_ids)
                or len(account_ids) != len(set(account_ids))):
            raise ValueError("invalid reporting balance accounts")
        result["balance_account_ids"] = list(account_ids)
    if "available_from_by_account" in value:
        coverage = value["available_from_by_account"]
        if not isinstance(coverage, dict):
            raise ValueError("invalid reporting coverage")
        normalized = {}
        for account, day in coverage.items():
            if not valid_identifier(account) or not isinstance(day, str):
                raise ValueError("invalid reporting coverage")
            try:
                canonical = date.fromisoformat(day).isoformat()
            except ValueError as error:
                raise ValueError("invalid reporting coverage") from error
            if canonical != day:
                raise ValueError("invalid reporting coverage")
            normalized[account] = canonical
        result["available_from_by_account"] = normalized
    return result


def scope_for_month(store, month, history=None):
    """Select whole accounts by owner before the inclusive household cutoff."""
    _month(month, "month")
    policy = validate(history) if history is not None else None
    individual = policy is not None and month < policy["joint_from_month"]
    accounts = list(store.db.execute("SELECT id,owner FROM accounts ORDER BY id"))
    if policy is not None and not store.db.execute(
            "SELECT 1 FROM persons WHERE id=?", (policy["prior_person_id"],)).fetchone():
        raise ValueError("reporting history prior person is unknown")
    selected = [row for row in accounts
                if not individual or row["owner"] == policy["prior_person_id"]]
    people = ([policy["prior_person_id"]] if individual else
              [row["id"] for row in store.db.execute("SELECT id FROM persons ORDER BY id")])
    result = {
        "mode": "individual" if individual else "household",
        "joint_from_month": policy["joint_from_month"] if policy else None,
        "prior_person_id": policy["prior_person_id"] if policy else None,
        "included_account_ids": [row["id"] for row in selected],
        "included_person_ids": people if individual else [*people, "JOINT"],
    }
    if policy is not None and "balance_account_ids" in policy:
        balance_accounts = policy["balance_account_ids"]
        checking_accounts = {
            row["id"] for row in store.db.execute(
                "SELECT id FROM accounts WHERE kind='CHECKING'")
        }
        if set(balance_accounts) - checking_accounts:
            raise ValueError("reporting balance account must be a known checking account")
        allowed_accounts = {row["id"] for row in selected}
        result["balance_account_ids"] = [
            account_id for account_id in balance_accounts if account_id in allowed_accounts
        ]
        result["balance_accounts_excluded_by_scope"] = (
            len(result["balance_account_ids"]) != len(balance_accounts)
        )
    if policy and "available_from_by_account" in policy:
        coverage = policy["available_from_by_account"]
        known_accounts = {row["id"] for row in accounts}
        if set(coverage) - known_accounts:
            raise ValueError("reporting coverage account is unknown")
        result["available_from_by_account"] = {
            row["id"]: coverage[row["id"]] for row in selected if row["id"] in coverage
        }
    return result


def month_quality(month, scope, *, unclassified_count=0):
    """Report explicitly known source gaps, never infer completeness from activity."""
    _month(month, "month")
    warnings = []
    for account, available_from in scope.get("available_from_by_account", {}).items():
        if month + "-01" < available_from:
            warnings.append({
                "code": "incomplete_source_coverage", "account_id": account,
                "available_from": available_from,
                "message": (f"{account}: Importierte Kontobuchungen sind erst ab "
                            f"{date.fromisoformat(available_from).strftime('%d.%m.%Y')} verfügbar. "
                            "Dieser Monat ist unvollständig; fehlende Buchungen sind keine Nullbeträge."),
            })
    if unclassified_count:
        warnings.append({
            "code": "classification_open", "count": unclassified_count,
            "message": f"{unclassified_count} Buchungen sind noch nicht eindeutig klassifiziert.",
        })
    status = ("incomplete" if any(w["code"] == "incomplete_source_coverage" for w in warnings)
              else "partial" if warnings else "known")
    return {"status": status, "warnings": warnings}
