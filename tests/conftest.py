import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from expense_tracker.core import db  # noqa: E402
from expense_tracker.core.models import Transaction  # noqa: E402


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    """Tests never hit the real API."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def make_txn(date, desc, pounds, direction="debit", account_type="debit", **kw) -> Transaction:
    from expense_tracker.core.categorizer import normalize_description
    from expense_tracker.core.models import to_pence

    return Transaction(date=date, description=desc, amount_pence=to_pence(pounds), direction=direction,
                       account_type=account_type, merchant_normalized=normalize_description(desc), **kw)


@pytest.fixture
def six_months():
    """Synthetic Jan-Jun 2026 current account + credit card history."""
    rows = []
    for m in range(1, 7):
        mm = f"2026-{m:02d}"
        rows += [
            make_txn(f"{mm}-25", "BGC ACME WIDGETS LTD SALARY", "3200.00", "credit"),
            make_txn(f"{mm}-01", "SO LANDLORD RENT", "1250.00"),
            make_txn(f"{mm}-05", "DD BT GROUP PLC", "35.99"),
            make_txn(f"{mm}-07", "DD OCTOPUS ENERGY", ["95.10", "88.40", "80.00", "72.55", "70.10", "68.00"][m - 1]),
            make_txn(f"{mm}-12", "NETFLIX.COM", "10.99", account_type="credit"),
            make_txn(f"{mm}-0{m}", "TESCO STORES 3021 LONDON", f"{40 + m}.50", account_type="credit"),
        ]
    from expense_tracker.core.categorizer import categorize

    categorize(rows, [], {})  # as the real pipeline does before recurring detection
    return rows
