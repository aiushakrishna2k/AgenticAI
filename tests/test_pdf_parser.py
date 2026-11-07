from pathlib import Path

from expense_tracker.core.models import Statement
from expense_tracker.core.pdf_parser import guess_account_type, heuristic_parse, mask_sensitive, reconcile

FIXTURES = Path(__file__).parent / "fixtures"


def test_mask_sensitive():
    text = "Card 4929 1234 5678 9012  Sort code 20-45-67 Account 12345678 IBAN GB29NWBK60161331926819"
    out = mask_sensitive(text)
    assert "4929" not in out and "9012" in out
    assert "20-45-67" not in out
    assert "12345678" not in out and "5678" in out
    assert "GB29NWBK" not in out and "6819" in out
    # amounts and dates untouched
    assert mask_sensitive("12/03/2026  TESCO  1,234.56") == "12/03/2026  TESCO  1,234.56"


def test_guess_account_type():
    assert guess_account_type((FIXTURES / "current_account.txt").read_text()) == "debit"
    assert guess_account_type((FIXTURES / "credit_card.txt").read_text()) == "credit"


def test_heuristic_current_account():
    txns = heuristic_parse([(FIXTURES / "current_account.txt").read_text()], "debit")
    got = [(t.date, t.description, t.amount_pence, t.direction) for t in txns]
    assert got == [
        ("2026-03-01", "SO LANDLORD RENT", 125000, "debit"),
        ("2026-03-05", "DD BT GROUP PLC", 3599, "debit"),
        ("2026-03-05", "CARD PAYMENT TO TESCO STORES 3021", 4250, "debit"),
        ("2026-03-20", "BARCLAYCARD PAYMENT", 50000, "debit"),
        ("2026-03-25", "BGC ACME WIDGETS LTD SALARY", 320000, "credit"),
    ]
    stmt = Statement("x", "h", "debit", opening_balance_pence=154321, closing_balance_pence=154321 - 125000
                     - 3599 - 4250 - 50000 + 320000)
    assert reconcile(stmt, txns)[0] is True


def test_heuristic_credit_card():
    txns = heuristic_parse([(FIXTURES / "credit_card.txt").read_text()], "credit")
    got = [(t.date, t.description, t.amount_pence, t.direction) for t in txns]
    assert got == [
        ("2026-02-28", "NETFLIX.COM", 1099, "debit"),
        ("2026-03-02", "AMAZON MARKETPLACE", 2999, "debit"),
        ("2026-03-09", "AMAZON MARKETPLACE REFUND", 2999, "credit"),
        ("2026-03-22", "PAYMENT RECEIVED - THANK YOU", 50000, "credit"),
    ]


def test_reconcile_mismatch_and_missing():
    stmt = Statement("x", "h", "credit", opening_balance_pence=10000, closing_balance_pence=5000)
    ok, msg = reconcile(stmt, [])
    assert ok is False and "difference" in msg
    assert reconcile(Statement("x", "h", "debit"), [])[0] is None
