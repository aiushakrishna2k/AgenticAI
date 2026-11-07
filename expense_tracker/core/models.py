"""Domain types and constants. Money is always integer pence (GBP)."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

CATEGORIES: list[str] = [
    "Salary/Income",
    "Rent/Mortgage",
    "Council Tax",
    "Utilities",
    "Internet & Phone",
    "Groceries",
    "Eating Out",
    "Transport",
    "Fuel",
    "Shopping",
    "Subscriptions",
    "Entertainment",
    "Health",
    "Insurance",
    "Travel",
    "Cash Withdrawal",
    "Transfers",
    "Fees & Interest",
    "Other",
]

INCOME_CATEGORY = "Salary/Income"
TRANSFER_CATEGORY = "Transfers"
SPEND_CATEGORIES = [c for c in CATEGORIES if c not in (INCOME_CATEGORY, TRANSFER_CATEGORY)]

ACCOUNT_TYPES = ("credit", "debit")   # credit card | debit card / current account
DIRECTIONS = ("debit", "credit")      # debit = money out, credit = money in


def to_pence(amount) -> int:
    """Convert a £ amount (str/float/Decimal) to integer pence, rounding half-up."""
    d = Decimal(str(amount).replace(",", "").replace("£", "").strip())
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_gbp(pence: int | None) -> str:
    if pence is None:
        return "—"
    sign = "-" if pence < 0 else ""
    p = abs(int(pence))
    return f"{sign}£{p // 100:,}.{p % 100:02d}"


@dataclass
class Transaction:
    date: str                      # ISO YYYY-MM-DD
    description: str
    amount_pence: int              # always positive; see direction
    direction: str                 # "debit" (out) | "credit" (in)
    account_type: str = "debit"    # account the row came from
    merchant_normalized: str = ""
    category: str = "Other"
    category_source: str = "default"  # rule | llm | user | default
    is_transfer: bool = False
    is_recurring: bool = False
    balance_pence: int | None = None
    statement_id: int | None = None
    id: int | None = None
    txn_hash: str = ""

    @property
    def month(self) -> str:
        return self.date[:7]


@dataclass
class Statement:
    file_name: str
    file_hash: str
    account_type: str
    bank: str = ""
    period_start: str | None = None
    period_end: str | None = None
    opening_balance_pence: int | None = None
    closing_balance_pence: int | None = None
    id: int | None = None


@dataclass
class Rule:
    pattern: str
    category: str
    match_type: str = "contains"   # contains | regex
    priority: int = 0
    source: str = "default"        # default | user
    id: int | None = None


@dataclass
class Budget:
    category: str
    monthly_limit_pence: int
    alert_threshold_pct: int = 80


@dataclass
class ExtractionResult:
    statement: Statement
    transactions: list[Transaction] = field(default_factory=list)
    reconciled: bool | None = None    # None = could not check (no balances)
    reconcile_message: str = ""
    method: str = "llm"               # llm | heuristic
