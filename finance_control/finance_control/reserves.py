"""Pure, cent-exact reserve binding; never creates bank transactions.

Inputs are validated budget plans. Cent remainders belong to the first months
of a due-date anchored cycle, even when the projection begins mid-cycle.
"""

from decimal import ROUND_HALF_UP, Decimal, localcontext

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")


def _money(value):
    return None if value is None else format(value, ".2f")


def _total(values):
    return None if any(value is None for value in values) else sum(values, _ZERO)


def _contribution(item, period):
    from .budget import _item_active, _month_distance

    if "accrual" not in item:
        return Decimal(item["amount"]) if _item_active(item, period) else _ZERO
    if (period < item["accrual"]["from_month"]
            or (item["end_month"] is not None and period > item["end_month"])):
        return _ZERO
    interval = item.get("interval_months", 1)
    cents = int(Decimal(item["amount"]) * 100)
    quotient, remainder = divmod(cents, interval)
    # Phase zero is the first month after the previous due month.
    phase = (_month_distance(item["start_month"], period) + interval - 1) % interval
    return Decimal(quotient + (phase < remainder)) / 100


def calculate(plan):
    """Project binding separately from the original expense/payment schedule."""
    # Up to 200 maximal item amounts multiplied by a maximal factor require
    # more than the default 28 significant digits before cent rounding.
    with localcontext() as context:
        context.prec = 64
        return _calculate(plan)


def _calculate(plan):
    from .budget import _item_active, _shift_month

    pots = plan.get("reserve_pots", [])
    first = plan["start_month"]
    horizon = plan.get("horizon_months", 12)
    last = _shift_month(first, horizon - 1)
    basis = sum((Decimal(item["amount"]) / item.get("interval_months", 1)
                 for item in plan["items"] if item["kind"] in {"fixed", "variable"}
                 and item.get("accrual", {}).get("from_month", item["start_month"]) <= last
                 and (item["end_month"] is None or item["end_month"] >= first)), _ZERO)
    setting = plan.get("safety_reserve")
    threshold = (None if setting is None else Decimal(setting["amount"])
                 if setting["mode"] == "fixed" else basis * Decimal(setting["factor"]))
    if threshold is not None:
        threshold = threshold.quantize(_CENT, rounding=ROUND_HALF_UP)
    balances = {pot["id"]: (None if pot["opening_amount"] is None
                            else Decimal(pot["opening_amount"])) for pot in pots}
    funders = {(item["accrual"]["pot_id"] if "accrual" in item else item["pot_id"]): item
               for item in plan["items"] if "accrual" in item or item["kind"] == "saving"}
    rows = []
    warnings = []
    for pot in pots:
        if balances[pot["id"]] is None:
            warnings.append(f"Anfangsbestand unbekannt: {pot['purpose']}")
    for offset in range(horizon):
        period = _shift_month(first, offset)
        pot_rows = []
        for pot in pots:
            pot_id = pot["id"]
            item = funders.get(pot_id)
            opening = balances[pot_id]
            contribution = _contribution(item, period) if item is not None else _ZERO
            due = (Decimal(item["amount"]) if item is not None and "accrual" in item
                   and _item_active(item, period) else _ZERO)
            if opening is None:
                release = gap = closing = None
            else:
                available = opening + contribution
                release = min(available, due)
                gap = due - release
                closing = available - release
                if gap:
                    warnings.append(f"Finanzierungslücke {period}: {pot['purpose']} ({gap:.2f} EUR)")
            balances[pot_id] = closing
            pot_rows.append({"pot_id": pot_id, "opening": _money(opening),
                             "contribution": _money(contribution), "due_amount": _money(due),
                             "release": _money(release), "funding_gap": _money(gap),
                             "closing": _money(closing)})
        row = {"period": period, "pots": pot_rows}
        for field, total_name in (("closing", "bound_total"),
                                  ("contribution", "contribution_total"),
                                  ("release", "release_total"),
                                  ("funding_gap", "funding_gap_total")):
            row[total_name] = _money(_total([
                None if entry[field] is None else Decimal(entry[field]) for entry in pot_rows]))
        rows.append(row)
    return {"scope": "whole_plan", "pots": [pot.copy() for pot in pots],
            "safety_threshold": _money(threshold),
            "monthly_expense_basis": _money(basis.quantize(_CENT, rounding=ROUND_HALF_UP)),
            "rows": rows, "warnings": warnings}


def liquidity_at(reserve_row, bank_balance, safety_threshold):
    """Attach an explicit bank basis; unknown bindings remain unknown."""
    with localcontext() as context:
        context.prec = 64
        return _liquidity_at(reserve_row, bank_balance, safety_threshold)


def _liquidity_at(reserve_row, bank_balance, safety_threshold):
    result = {"bank_balance": bank_balance, "free_available": None,
              "safety_threshold": safety_threshold, "safety_shortfall": None,
              "below_reserve": None, "unavailable_reason": None}
    if bank_balance is None:
        result["unavailable_reason"] = "missing_bank_balance"
    elif reserve_row is None or reserve_row.get("bound_total") is None:
        result["unavailable_reason"] = "unknown_pot_balance"
    else:
        bank = Decimal(bank_balance)
        free = bank - Decimal(reserve_row["bound_total"])
        result["bank_balance"] = _money(bank)
        result["free_available"] = _money(free)
        if safety_threshold is not None:
            threshold = Decimal(safety_threshold)
            result["safety_shortfall"] = _money(max(threshold - free, _ZERO))
            result["below_reserve"] = free < threshold
    return result
