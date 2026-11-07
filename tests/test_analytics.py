from expense_tracker.core.analytics import (budget_alerts, budget_status, monthly_category_totals,
                                            monthly_savings, suggest_budgets)
from expense_tracker.core.categorizer import categorize
from expense_tracker.core.models import Budget, fmt_gbp, to_pence

from .conftest import make_txn


def test_money_helpers():
    assert to_pence("1,234.56") == 123456
    assert to_pence("£0.10") == 10
    assert to_pence(0.1 + 0.2) == 30           # float noise rounded away
    assert fmt_gbp(123456) == "£1,234.56"
    assert fmt_gbp(-5) == "-£0.05"


def _categorised(txns):
    categorize(txns, [], {})
    return txns


def test_monthly_category_totals(six_months):
    totals = monthly_category_totals(_categorised(six_months))
    jan = totals["2026-01"]
    assert jan["Groceries"] == 4150
    assert jan["Internet & Phone"] == 3599
    assert jan["Subscriptions"] == 1099
    assert "Salary/Income" not in jan


def test_savings(six_months):
    s = {m.month: m for m in monthly_savings(_categorised(six_months))}
    jan = s["2026-01"]
    assert jan.income_pence == 320000
    assert jan.spending_pence == 125000 + 3599 + 9510 + 1099 + 4150
    assert jan.savings_pence == 320000 - jan.spending_pence
    assert 0 < jan.savings_rate < 1


def test_transfers_excluded_and_refunds_net():
    txns = _categorised([
        make_txn("2026-03-01", "BGC ACME SALARY", "2000", "credit"),
        make_txn("2026-03-02", "AMAZON MARKETPLACE", "100", account_type="credit"),
        make_txn("2026-03-09", "AMAZON MARKETPLACE", "30", "credit", "credit"),      # refund
        make_txn("2026-03-20", "BARCLAYCARD PAYMENT", "70", "debit", "debit", is_transfer=True),
        make_txn("2026-03-21", "PAYMENT RECEIVED", "70", "credit", "credit", is_transfer=True),
    ])
    for t in txns[-2:]:
        t.is_transfer, t.category = True, "Transfers"
    s = monthly_savings(txns)[0]
    assert s.income_pence == 200000
    assert s.spending_pence == 7000
    assert monthly_category_totals(txns)["2026-03"] == {"Shopping": 7000}


def test_budget_status_thresholds(six_months):
    txns = _categorised(six_months)
    budgets = {
        "Groceries": Budget("Groceries", 4000, 80),           # 41.50 spent > 40 -> over
        "Utilities": Budget("Utilities", 10000, 80),          # 95.10 -> 95% -> warning
        "Subscriptions": Budget("Subscriptions", 5000, 80),   # 10.99 -> ok
    }
    rows = {r.category: r for r in budget_status(txns, budgets, "2026-01")}
    assert rows["Groceries"].status == "over"
    assert rows["Utilities"].status == "warning"
    assert rows["Subscriptions"].status == "ok"
    assert [a.category for a in budget_alerts(txns, budgets, "2026-01")] == ["Groceries", "Utilities"]


def test_suggest_budgets(six_months):
    s = suggest_budgets(_categorised(six_months))
    # Apr-Jun groceries 44.50, 45.50, 46.50 -> avg 45.50 -> ceil to £50
    assert s["Groceries"] == 5000
    assert s["Rent/Mortgage"] == 125000
