"""Monthly aggregates, savings and budget checks. Pure Python; all money in pence.

Accounting rules
  * transfers (is_transfer) are excluded from both income and spending
  * debit (money out)            -> spending in its category
  * credit in 'Salary/Income'    -> income
  * any other credit             -> refund: reduces spending in its category
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .models import INCOME_CATEGORY, Budget, Transaction


def monthly_category_totals(txns: list[Transaction]) -> dict[str, dict[str, int]]:
    """{month: {category: net spend pence}} - income categories excluded."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in txns:
        if t.is_transfer:
            continue
        if t.direction == "debit":
            out[t.month][t.category] += t.amount_pence
        elif t.category != INCOME_CATEGORY:
            out[t.month][t.category] -= t.amount_pence
    return {m: dict(c) for m, c in sorted(out.items())}


@dataclass
class MonthSummary:
    month: str
    income_pence: int
    spending_pence: int

    @property
    def savings_pence(self) -> int:
        return self.income_pence - self.spending_pence

    @property
    def savings_rate(self) -> float | None:
        return None if self.income_pence <= 0 else self.savings_pence / self.income_pence


def monthly_savings(txns: list[Transaction]) -> list[MonthSummary]:
    income: dict[str, int] = defaultdict(int)
    spend: dict[str, int] = defaultdict(int)
    months: set[str] = set()
    for t in txns:
        months.add(t.month)
        if t.is_transfer:
            continue
        if t.direction == "credit" and t.category == INCOME_CATEGORY:
            income[t.month] += t.amount_pence
        elif t.direction == "debit":
            spend[t.month] += t.amount_pence
        else:  # refund
            spend[t.month] -= t.amount_pence
    return [MonthSummary(m, income[m], spend[m]) for m in sorted(months)]


@dataclass
class BudgetStatus:
    category: str
    spent_pence: int
    limit_pence: int
    threshold_pct: int

    @property
    def pct_used(self) -> float:
        return 0.0 if self.limit_pence <= 0 else 100.0 * self.spent_pence / self.limit_pence

    @property
    def status(self) -> str:
        if self.limit_pence <= 0:
            return "over" if self.spent_pence > 0 else "ok"
        if self.spent_pence > self.limit_pence:
            return "over"
        if self.pct_used >= self.threshold_pct:
            return "warning"
        return "ok"

    @property
    def remaining_pence(self) -> int:
        return self.limit_pence - self.spent_pence


def budget_status(txns: list[Transaction], budgets: dict[str, Budget], month: str) -> list[BudgetStatus]:
    totals = monthly_category_totals(txns).get(month, {})
    rows = [BudgetStatus(cat, max(0, totals.get(cat, 0)), b.monthly_limit_pence, b.alert_threshold_pct)
            for cat, b in budgets.items()]
    order = {"over": 0, "warning": 1, "ok": 2}
    rows.sort(key=lambda r: (order[r.status], -r.pct_used))
    return rows


def budget_alerts(txns: list[Transaction], budgets: dict[str, Budget], month: str) -> list[BudgetStatus]:
    return [r for r in budget_status(txns, budgets, month) if r.status != "ok"]


def suggest_budgets(txns: list[Transaction], last_n_months: int = 3) -> dict[str, int]:
    """Average spend per category over the last N months with data, rounded up to the next £10."""
    totals = monthly_category_totals(txns)
    months = sorted(totals)[-last_n_months:]
    if not months:
        return {}
    sums: dict[str, int] = defaultdict(int)
    for m in months:
        for cat, v in totals[m].items():
            sums[cat] += max(0, v)
    out = {}
    for cat, s in sums.items():
        avg = s / len(months)
        out[cat] = int(-(-avg // 1000) * 1000)  # ceil to £10 (1000p)
    return out
