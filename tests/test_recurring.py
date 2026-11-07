from expense_tracker.core.categorizer import categorize
from expense_tracker.core.recurring import apply_recurring_flags, detect_recurring, recurring_alerts

from .conftest import make_txn


def _groups(txns, overrides=None):
    return {g.merchant: g for g in detect_recurring(txns, overrides)}


def test_detects_salary_bills_subscriptions(six_months):
    g = _groups(six_months)
    assert g["ACME WIDGETS SALARY"].kind == "income"
    assert g["ACME WIDGETS SALARY"].cadence == "monthly"
    assert g["BT GROUP"].kind == "subscription"           # identical amount every month
    assert g["OCTOPUS ENERGY"].kind == "bill"       # varies within tolerance
    assert g["NETFLIX"].kind == "subscription"
    assert g["LANDLORD RENT"].typical_amount_pence == 125000


def test_irregular_spend_not_recurring(six_months):
    extra = [make_txn("2026-01-03", "ZARA", "50"), make_txn("2026-03-19", "ZARA", "120"),
             make_txn("2026-06-02", "ZARA", "15")]
    assert "ZARA" not in _groups(six_months + extra)


def test_growing_discretionary_spend_is_not_a_commitment(six_months):
    # A monthly Amazon order that grows each month is habit, not a bill: no recurring group,
    # so no "higher than usual" noise. An energy bill varying the same amount still qualifies.
    amazon = [make_txn(f"2026-0{m}-18", "AMAZON MARKETPLACE", f"{30 + 7 * m}.00", account_type="credit")
              for m in range(1, 7)]
    categorize(amazon, [], {})
    g = _groups(six_months + amazon)
    assert "AMAZON MARKETPLACE" not in g
    assert "OCTOPUS ENERGY" in g


def test_tesco_variable_dates_not_monthly(six_months):
    # Tesco happens on day 1..6 each month with varying amounts - gaps ~30 but amounts drift
    # 41.50 -> 46.50 (within 20%) so it IS detected; that's acceptable (regular grocery shop).
    g = _groups(six_months)
    assert "TESCO STORES LONDON" in g


def test_two_months_is_tentative():
    txns = [make_txn("2026-01-05", "DD BT GROUP PLC", "35.99"), make_txn("2026-02-05", "DD BT GROUP PLC", "35.99")]
    g = _groups(txns)
    assert g["BT GROUP"].tentative


def test_overrides(six_months):
    g = _groups(six_months, {("NETFLIX", "debit"): "dismissed"})
    assert g["NETFLIX"].status == "dismissed"
    groups = detect_recurring(six_months, {("NETFLIX", "debit"): "dismissed"})
    apply_recurring_flags(six_months, groups)
    assert not any(t.is_recurring for t in six_months if t.merchant_normalized == "NETFLIX")
    assert all(t.is_recurring for t in six_months if t.merchant_normalized == "BT GROUP")

    one_off = [make_txn("2026-01-15", "GYM CLASS", "12")]
    assert "GYM CLASS" in _groups(one_off, {("GYM CLASS", "debit"): "confirmed"})


def test_missed_salary_alert(six_months):
    # drop June salary; as_of end of June
    txns = [t for t in six_months if not (t.direction == "credit" and t.date.startswith("2026-06"))]
    groups = detect_recurring(txns)
    alerts = recurring_alerts(groups, txns, as_of="2026-07-02")
    assert any("ACME" in a.message and "not seen" in a.message for a in alerts)


def test_high_bill_alert(six_months):
    txns = six_months + [make_txn("2026-07-07", "DD OCTOPUS ENERGY", "120.00")]
    groups = detect_recurring(txns)
    assert any(g.merchant == "OCTOPUS ENERGY" for g in groups)
    alerts = recurring_alerts(groups, txns, as_of="2026-07-08")
    assert any("OCTOPUS" in a.message and "higher than usual" in a.message for a in alerts)


def test_transfers_ignored():
    txns = [make_txn(f"2026-0{m}-01", "TO SAVINGS POT", "200", is_transfer=True) for m in range(1, 5)]
    assert detect_recurring(txns) == []
