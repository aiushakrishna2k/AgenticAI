"""Detect recurring transactions (salary, bills, subscriptions) and raise missed/late/high alerts."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median

from .models import INCOME_CATEGORY, Transaction

CADENCES = {  # name: (min gap days, max gap days, nominal days)
    "weekly": (6, 8, 7),
    "monthly": (25, 35, 30),
    "quarterly": (85, 97, 91),
    "annual": (355, 375, 365),
}
# Commitments (bills, rent, subscriptions, income) may vary a lot - energy bills swing
# seasonally (e.g. £95 winter vs £68 summer). Discretionary spend (a monthly Amazon order,
# takeaways) must be near-identical to count as recurring, otherwise it's just habit.
COMMITMENT_CATEGORIES = {
    INCOME_CATEGORY, "Rent/Mortgage", "Council Tax", "Utilities", "Internet & Phone", "Subscriptions",
    "Insurance", "Health", "Fees & Interest",
}
COMMITMENT_TOLERANCE = 0.35
DISCRETIONARY_TOLERANCE = 0.10
AMOUNT_TOLERANCE = COMMITMENT_TOLERANCE  # backwards-compatible name
HIGH_BILL_FACTOR = 1.20   # alert when a bill is >20% above its typical amount


@dataclass
class RecurringGroup:
    merchant: str
    direction: str
    kind: str                      # income | bill | subscription
    cadence: str
    typical_amount_pence: int
    occurrences: int
    months: list[str]
    last_date: str
    category: str
    tentative: bool = False
    status: str = "detected"       # detected | confirmed | dismissed
    txn_ids: list[int] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.merchant, self.direction)

    @property
    def next_expected(self) -> str:
        nominal = CADENCES[self.cadence][2]
        return (date.fromisoformat(self.last_date) + timedelta(days=nominal)).isoformat()


@dataclass
class RecurringAlert:
    level: str        # warning | info
    message: str
    merchant: str


def _classify_cadence(gaps: list[int]) -> str | None:
    if not gaps:
        return None
    g = median(gaps)
    for name, (lo, hi, _) in CADENCES.items():
        if lo <= g <= hi:
            return name
    return None


def detect_recurring(
    txns: list[Transaction],
    overrides: dict[tuple[str, str], str] | None = None,
) -> list[RecurringGroup]:
    """Group by merchant + direction and keep groups with a regular cadence and stable amount.

    Thresholds: >=3 distinct months (>=2 marks the group 'tentative' when little history
    is loaded), median gap within a known cadence, every amount within +-20% of the median.
    User 'confirmed' overrides force a group in; 'dismissed' groups are returned with that
    status so the UI can show them, but callers should ignore them.
    """
    overrides = overrides or {}
    groups: dict[tuple[str, str], list[Transaction]] = defaultdict(list)
    for t in txns:
        if t.is_transfer:
            continue
        groups[(t.merchant_normalized, t.direction)].append(t)

    all_months = {t.month for t in txns}
    result: list[RecurringGroup] = []
    for key, items in groups.items():
        items.sort(key=lambda t: t.date)
        status = overrides.get(key, "detected")
        months = sorted({t.month for t in items})
        dates = [date.fromisoformat(t.date) for t in items]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        cadence = _classify_cadence(gaps)
        amounts = [t.amount_pence for t in items]
        # Judge stability on history excluding the newest payment, so an unusually high
        # latest bill keeps its group (and triggers a "higher than usual" alert instead).
        history = amounts[:-1] if len(amounts) >= 3 else amounts
        med = median(history)
        commitment = items[-1].category in COMMITMENT_CATEGORIES or key[1] == "credit"
        tol = COMMITMENT_TOLERANCE if commitment else DISCRETIONARY_TOLERANCE
        stable = med > 0 and all(abs(a - med) <= tol * med for a in history)

        min_months = 3 if len(all_months) >= 3 else 2
        qualifies = cadence is not None and stable and (
            len(months) >= min_months or (cadence in ("quarterly", "annual") and len(items) >= 2)
        )
        if cadence == "weekly":
            qualifies = qualifies and len(items) >= 4
        if not qualifies and status != "confirmed":
            continue
        cadence = cadence or "monthly"

        direction = key[1]
        if direction == "credit":
            kind = "income"
        elif len(set(amounts)) == 1:
            kind = "subscription"
        else:
            kind = "bill"

        result.append(RecurringGroup(
            merchant=key[0], direction=direction, kind=kind, cadence=cadence,
            typical_amount_pence=int(med), occurrences=len(items), months=months,
            last_date=items[-1].date, category=items[-1].category,
            tentative=len(months) < 3 and status != "confirmed", status=status,
            txn_ids=[t.id for t in items if t.id is not None],
        ))
    result.sort(key=lambda g: (g.direction != "credit", -g.typical_amount_pence))
    return result


def apply_recurring_flags(txns: list[Transaction], groups: list[RecurringGroup]) -> None:
    active = {g.key for g in groups if g.status != "dismissed"}
    for t in txns:
        t.is_recurring = (t.merchant_normalized, t.direction) in active and not t.is_transfer


def recurring_alerts(groups: list[RecurringGroup], txns: list[Transaction], as_of: str) -> list[RecurringAlert]:
    """Missed/late payments and unusually high bills, evaluated at `as_of` (ISO date).

    `as_of` should normally be the latest transaction date loaded, because statements are
    historical - using today's date would flag everything as missed.
    """
    alerts: list[RecurringAlert] = []
    as_of_d = date.fromisoformat(as_of)
    by_key: dict[tuple[str, str], list[Transaction]] = defaultdict(list)
    for t in txns:
        by_key[(t.merchant_normalized, t.direction)].append(t)

    for g in groups:
        if g.status == "dismissed" or g.cadence not in ("monthly", "weekly"):
            continue
        grace = 5 if g.cadence == "monthly" else 2
        expected = date.fromisoformat(g.next_expected)
        if as_of_d > expected + timedelta(days=grace):
            what = "Salary/income" if g.kind == "income" else g.kind.capitalize()
            alerts.append(RecurringAlert(
                "warning",
                f"{what} from {g.merchant} not seen since {g.last_date} (expected around {g.next_expected}).",
                g.merchant,
            ))
        latest = max(by_key[g.key], key=lambda t: t.date)
        if g.kind == "bill" and latest.amount_pence > g.typical_amount_pence * HIGH_BILL_FACTOR:
            pct = round(100 * (latest.amount_pence / g.typical_amount_pence - 1))
            alerts.append(RecurringAlert(
                "info", f"{g.merchant} bill on {latest.date} is {pct}% higher than usual.", g.merchant,
            ))
        if g.kind == "income" and latest.amount_pence < g.typical_amount_pence * 0.8:
            alerts.append(RecurringAlert(
                "warning", f"Latest payment from {g.merchant} ({latest.date}) is lower than usual.", g.merchant,
            ))
    return alerts
