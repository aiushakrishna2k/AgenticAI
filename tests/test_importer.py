"""End-to-end: real PDF bytes -> pdfplumber -> heuristic parse -> categorise -> save -> analytics."""
from pathlib import Path

from expense_tracker.core import db, importer
from expense_tracker.core.analytics import budget_alerts, monthly_category_totals, monthly_savings
from expense_tracker.core.models import Budget

from .pdf_maker import make_pdf

FIX = Path(__file__).parent / "fixtures"


def _pdf(name):
    return make_pdf((FIX / name).read_text().splitlines())


def _import(conn, name, acct):
    res = importer.parse_pdf(_pdf(name), name, account_type_hint=acct, use_llm=False)
    importer.prepare(conn, res, use_llm=False)
    return res, importer.save(conn, res)


def test_end_to_end(conn):
    res, out = _import(conn, "current_account.txt", "debit")
    assert res.method == "heuristic"
    assert out["inserted"] == 5

    # Before the card statement is loaded, the card payment counts as an outflow.
    txns = db.get_transactions(conn)
    pay = next(t for t in txns if "BARCLAYCARD" in t.description)
    assert not pay.is_transfer

    _, out2 = _import(conn, "credit_card.txt", "credit")
    assert out2["inserted"] == 4
    txns = db.get_transactions(conn)
    pay = next(t for t in txns if "BARCLAYCARD" in t.description)
    rcv = next(t for t in txns if "PAYMENT RECEIVED" in t.description)
    assert pay.is_transfer and rcv.is_transfer   # both legs now excluded

    cats = {t.description: t.category for t in txns}
    assert cats["DD BT GROUP PLC"] == "Internet & Phone"
    assert cats["CARD PAYMENT TO TESCO STORES 3021"] == "Groceries"
    assert cats["BGC ACME WIDGETS LTD SALARY"] == "Salary/Income"
    assert cats["NETFLIX.COM"] == "Subscriptions"

    march = monthly_category_totals(txns)["2026-03"]
    assert march["Shopping"] == 0                # purchase and refund cancel out
    assert march["Groceries"] == 4250
    s = {m.month: m for m in monthly_savings(txns)}["2026-03"]
    assert s.income_pence == 320000
    assert s.spending_pence == 125000 + 3599 + 4250   # transfers excluded

    db.set_budget(conn, "Groceries", 4000)
    assert [a.category for a in budget_alerts(txns, db.get_budgets(conn), "2026-03")] == ["Groceries"]


def test_reimport_same_file_is_rejected(conn):
    _import(conn, "current_account.txt", "debit")
    _, out = _import(conn, "current_account.txt", "debit")
    assert out["duplicate_file"] is True
    assert len(db.get_transactions(conn)) == 5


def test_user_correction_learned_as_rule(conn):
    _import(conn, "current_account.txt", "debit")
    db.upsert_user_rule(conn, "TESCO STORES", "Shopping")
    importer.recategorize_all(conn, use_llm=False)
    t = next(t for t in db.get_transactions(conn) if "TESCO" in t.description)
    assert t.category == "Shopping"
