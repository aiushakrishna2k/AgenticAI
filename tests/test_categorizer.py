from expense_tracker.core.categorizer import categorize, load_default_rules, normalize_description
from expense_tracker.core.models import CATEGORIES, Rule

from .conftest import make_txn


def test_normalize_strips_bank_noise():
    assert normalize_description("CARD PAYMENT TO TESCO STORES 3021 ON 12 MAR") == "TESCO STORES"
    assert normalize_description("CONTACTLESS PRET A MANGER LONDON GB") == "PRET A MANGER"
    assert normalize_description("DD OCTOPUS ENERGY REF:A1B2C3") == "OCTOPUS ENERGY"
    assert normalize_description("NETFLIX.COM 866-579-7172") == "NETFLIX"


def test_normalize_is_stable_across_months():
    a = normalize_description("TESCO STORES 3021 LONDON 05/01")
    b = normalize_description("TESCO STORES 4410 LONDON 12/02")
    assert a == b


def test_default_rules_valid_categories():
    rules = load_default_rules()
    assert rules and all(r.category in CATEGORIES for r in rules)


def _cat(desc, direction="debit", account_type="debit", **kw):
    t = make_txn("2026-03-01", desc, "10.00", direction, account_type)
    categorize([t], user_rules=kw.get("user_rules", []), cache=kw.get("cache", {}), llm=kw.get("llm"))
    return t


def test_default_rules():
    assert _cat("CARD PAYMENT TO TESCO STORES 3021").category == "Groceries"
    assert _cat("DD BT GROUP PLC").category == "Internet & Phone"
    assert _cat("DD THAMES WATER").category == "Utilities"
    assert _cat("TFL TRAVEL CH").category == "Transport"
    assert _cat("NETFLIX.COM").category == "Subscriptions"
    assert _cat("DD LONDON BOROUGH OF HACKNEY COUNCIL TAX").category == "Council Tax"


def test_most_specific_rule_wins():
    assert _cat("TESCO PETROL 1234").category == "Fuel"
    assert _cat("AMAZON PRIME UK").category == "Subscriptions"
    assert _cat("AMAZON MARKETPLACE").category == "Shopping"


def test_word_boundaries():
    # 'BT' must not match inside 'DEBT', 'EE' not inside 'COFFEE'
    t = _cat("DEBT COLLECTOR SERVICES")
    assert t.category != "Internet & Phone"
    assert _cat("BLACKSHEEP COFFEE").category != "Internet & Phone"


def test_user_rule_beats_default():
    rule = Rule(pattern="TESCO", category="Shopping", source="user", priority=100)
    assert _cat("TESCO STORES 3021", user_rules=[rule]).category == "Shopping"


def test_user_edited_rows_are_never_overwritten():
    t = make_txn("2026-03-01", "TESCO STORES", "5", category="Eating Out", category_source="user")
    categorize([t], [], {})
    assert t.category == "Eating Out"


def test_llm_called_once_per_unknown_merchant_and_cached_used():
    calls = []

    def fake_llm(items):
        calls.append(items)
        return {(i["merchant"], i["direction"]): "Entertainment" for i in items}

    txns = [make_txn("2026-03-01", "ZZYZX ESCAPE ROOMS", "20"), make_txn("2026-03-09", "ZZYZX ESCAPE ROOMS", "20"),
            make_txn("2026-03-02", "TESCO STORES", "5")]
    learned = categorize(txns, [], {}, llm=fake_llm)
    assert len(calls) == 1 and len(calls[0]) == 1
    assert [t.category for t in txns] == ["Entertainment", "Entertainment", "Groceries"]
    assert learned == {("ZZYZX ESCAPE ROOMS", "debit"): "Entertainment"}

    # second run uses cache, no LLM call
    t2 = make_txn("2026-04-01", "ZZYZX ESCAPE ROOMS", "20")
    categorize([t2], [], learned, llm=fake_llm)
    assert len(calls) == 1 and t2.category == "Entertainment"


def test_llm_failure_falls_back():
    def broken(items):
        raise RuntimeError("boom")

    debit = _cat("UNKNOWN MERCHANT XYZ", llm=broken)
    credit = _cat("ACME WIDGETS", direction="credit", llm=broken)
    assert debit.category == "Other"
    assert credit.category == "Salary/Income"


def test_llm_invalid_category_ignored():
    t = _cat("UNKNOWN MERCHANT XYZ", llm=lambda items: {(i["merchant"], i["direction"]): "Bananas" for i in items})
    assert t.category == "Other"


def test_transfer_category_sets_flag():
    rule = Rule(pattern="SAVINGS POT", category="Transfers", source="user", priority=100)
    t = _cat("TO SAVINGS POT", user_rules=[rule])
    assert t.is_transfer
