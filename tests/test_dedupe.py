from expense_tracker.core import db
from expense_tracker.core.dedupe import assign_hashes, detect_transfers

from .conftest import make_txn


def test_card_payment_both_legs_flagged():
    out = make_txn("2026-03-20", "BARCLAYCARD PAYMENT", "500.00", "debit", "debit")
    inn = make_txn("2026-03-22", "PAYMENT RECEIVED - THANK YOU", "500.00", "credit", "credit")
    purchase = make_txn("2026-03-10", "TESCO STORES", "500.00", "debit", "credit")
    detect_transfers([out, inn, purchase])
    assert out.is_transfer and inn.is_transfer
    assert not purchase.is_transfer
    assert out.category == inn.category == "Transfers"


def test_card_payment_without_card_statement_stays_counted():
    out = make_txn("2026-03-20", "BARCLAYCARD PAYMENT", "500.00", "debit", "debit")
    detect_transfers([out])
    assert not out.is_transfer


def test_amount_and_window_must_match():
    out = make_txn("2026-03-01", "AMEX PAYMENT", "300.00", "debit", "debit")
    inn_late = make_txn("2026-03-20", "PAYMENT RECEIVED", "300.00", "credit", "credit")
    detect_transfers([out, inn_late])
    assert not out.is_transfer and inn_late.is_transfer


def test_refund_on_card_is_not_transfer():
    refund = make_txn("2026-03-05", "AMAZON REFUND", "20.00", "credit", "credit")
    detect_transfers([refund])
    assert not refund.is_transfer


def test_user_decision_respected():
    inn = make_txn("2026-03-22", "PAYMENT RECEIVED", "50.00", "credit", "credit",
                   category="Shopping", category_source="user")
    detect_transfers([inn])
    assert not inn.is_transfer and inn.category == "Shopping"


def test_identical_rows_in_one_statement_kept_but_reimport_deduped(conn):
    a = make_txn("2026-03-01", "COSTA COFFEE", "3.20", account_type="credit")
    b = make_txn("2026-03-01", "COSTA COFFEE", "3.20", account_type="credit")
    assign_hashes([a, b])
    assert a.txn_hash != b.txn_hash
    assert db.insert_transactions(conn, [a, b]) == (2, 0)

    a2 = make_txn("2026-03-01", "COSTA COFFEE", "3.20", account_type="credit")
    b2 = make_txn("2026-03-01", "COSTA COFFEE", "3.20", account_type="credit")
    assign_hashes([a2, b2])
    assert db.insert_transactions(conn, [a2, b2]) == (0, 2)
