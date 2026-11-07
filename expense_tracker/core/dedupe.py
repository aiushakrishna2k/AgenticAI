"""Duplicate protection and transfer detection (avoids double counting).

Paying the credit card from the current account appears twice:
  * debit account:  "BARCLAYCARD PAYMENT"   -£500  (money leaves)
  * credit card:    "PAYMENT RECEIVED"      +£500  (card balance reduced)
The underlying purchases are already counted on the card statement, so both
legs are flagged is_transfer=True and excluded from income and spending.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date

from .models import TRANSFER_CATEGORY, Transaction

# Credit-card side: credits that are repayments, not refunds.
CARD_REPAYMENT_RE = re.compile(
    r"PAYMENT\s+(RECEIVED|THANK\s*YOU|-\s*THANK)|DIRECT\s+DEBIT\s+PAYMENT|THANK\s+YOU\s+FOR\s+YOUR\s+PAYMENT"
    r"|^PAYMENT\b|\bDD\s+PAYMENT\b|AUTOPAY|FASTER\s+PAYMENT\s+RECEIVED",
    re.IGNORECASE,
)
# Debit-account side: payments made to a credit card.
CARD_PAYMENT_OUT_RE = re.compile(
    r"BARCLAYCARD|AMEX|AMERICAN\s+EXPRESS|CREDIT\s+CARD|MBNA|CAPITAL\s+ONE|HSBC\s+CARD|NATWEST\s+CARD"
    r"|LLOYDS\s+CARD|HALIFAX\s+CREDIT|VIRGIN\s+MONEY\s+CC|TESCO\s+BANK\s+CC|SANTANDER\s+CC|MONZO\s+FLEX"
    r"|\bCC\s+PAYMENT\b|CARD\s+REPAYMENT|JOHN\s+LEWIS\s+FINANCE|VANQUIS|AQUA\s+CARD|ZOPA\s+CARD",
    re.IGNORECASE,
)
MATCH_WINDOW_DAYS = 5


def txn_hash(t: Transaction, statement_hint: str = "") -> str:
    """Stable identity for a transaction. Includes the running balance when present so that two
    genuinely identical purchases on the same day (e.g. two £3.20 coffees) aren't collapsed."""
    key = "|".join([
        t.account_type, t.date, str(t.amount_pence), t.direction,
        re.sub(r"\s+", " ", t.description.upper().strip()),
        "" if t.balance_pence is None else str(t.balance_pence), statement_hint,
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def assign_hashes(txns: list[Transaction]) -> None:
    """Hash every txn; disambiguate identical rows *within* one statement by occurrence index."""
    seen: dict[str, int] = {}
    for t in txns:
        base = txn_hash(t)
        n = seen.get(base, 0)
        seen[base] = n + 1
        t.txn_hash = base if n == 0 else txn_hash(t, statement_hint=f"#{n}")


def _days_between(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def detect_transfers(txns: list[Transaction]) -> int:
    """Flag card repayments (both legs) as transfers. Mutates txns; returns number newly flagged.

    User decisions are respected: rows with category_source == 'user' are left alone.
    """
    changed = 0

    def mark(t: Transaction) -> None:
        nonlocal changed
        if t.category_source == "user":
            return
        if not t.is_transfer or t.category != TRANSFER_CATEGORY:
            changed += 1
        t.is_transfer = True
        t.category = TRANSFER_CATEGORY
        t.category_source = "rule"

    # Card repayments received on credit card statements are always transfers.
    card_credits = [t for t in txns if t.account_type == "credit" and t.direction == "credit"
                    and CARD_REPAYMENT_RE.search(t.description)]
    for t in card_credits:
        mark(t)

    # Debit-account payments to a card: transfer when the matching card credit exists
    # (same amount within +-5 days). Without the card statement loaded the payment is a
    # genuine outflow as far as we can tell, so it stays counted.
    unmatched = list(card_credits)
    for t in sorted(txns, key=lambda x: x.date):
        if t.account_type != "debit" or t.direction != "debit":
            continue
        if not CARD_PAYMENT_OUT_RE.search(t.description):
            continue
        partner = next((c for c in unmatched if c.amount_pence == t.amount_pence
                        and _days_between(c.date, t.date) <= MATCH_WINDOW_DAYS), None)
        if partner is not None:
            unmatched.remove(partner)
            mark(t)
    return changed
