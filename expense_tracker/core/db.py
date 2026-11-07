"""SQLite persistence (plain sqlite3, no ORM)."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import Budget, Rule, Statement, Transaction

DEFAULT_DB_PATH = Path(os.getenv("EXPENSE_TRACKER_DB") or
                       Path(__file__).resolve().parent.parent / "data" / "expenses.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS statements (
    id INTEGER PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_hash TEXT NOT NULL UNIQUE,
    account_type TEXT NOT NULL CHECK (account_type IN ('credit','debit')),
    bank TEXT,
    period_start TEXT,
    period_end TEXT,
    opening_balance_pence INTEGER,
    closing_balance_pence INTEGER,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    statement_id INTEGER REFERENCES statements(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    description TEXT NOT NULL,
    merchant_normalized TEXT NOT NULL,
    amount_pence INTEGER NOT NULL CHECK (amount_pence >= 0),
    direction TEXT NOT NULL CHECK (direction IN ('debit','credit')),
    account_type TEXT NOT NULL,
    category TEXT NOT NULL,
    category_source TEXT NOT NULL,
    is_transfer INTEGER NOT NULL DEFAULT 0,
    is_recurring INTEGER NOT NULL DEFAULT 0,
    balance_pence INTEGER,
    txn_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_merchant ON transactions(merchant_normalized);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL,
    match_type TEXT NOT NULL DEFAULT 'contains',
    category TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    source TEXT NOT NULL DEFAULT 'user',
    UNIQUE(pattern, match_type)
);
CREATE TABLE IF NOT EXISTS budgets (
    category TEXT PRIMARY KEY,
    monthly_limit_pence INTEGER NOT NULL CHECK (monthly_limit_pence >= 0),
    alert_threshold_pct INTEGER NOT NULL DEFAULT 80
);
-- Claude categorisation cache so a merchant is never sent twice.
CREATE TABLE IF NOT EXISTS merchant_cache (
    merchant_normalized TEXT NOT NULL,
    direction TEXT NOT NULL,
    category TEXT NOT NULL,
    PRIMARY KEY (merchant_normalized, direction)
);
-- User confirm/dismiss decisions on detected recurring groups.
CREATE TABLE IF NOT EXISTS recurring_overrides (
    merchant_normalized TEXT NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('confirmed','dismissed')),
    PRIMARY KEY (merchant_normalized, direction)
);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (and initialise) the database. Use ':memory:' in tests."""
    path = DEFAULT_DB_PATH if path is None else path
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- statements
def statement_exists(conn, file_hash: str) -> bool:
    return conn.execute("SELECT 1 FROM statements WHERE file_hash=?", (file_hash,)).fetchone() is not None


def insert_statement(conn, s: Statement) -> int:
    cur = conn.execute(
        """INSERT INTO statements (file_name, file_hash, account_type, bank, period_start, period_end,
               opening_balance_pence, closing_balance_pence, imported_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (s.file_name, s.file_hash, s.account_type, s.bank, s.period_start, s.period_end,
         s.opening_balance_pence, s.closing_balance_pence, datetime.now().isoformat(timespec="seconds")),
    )
    return cur.lastrowid


def list_statements(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT s.*, COUNT(t.id) AS n_transactions FROM statements s
           LEFT JOIN transactions t ON t.statement_id = s.id
           GROUP BY s.id ORDER BY s.period_start DESC, s.id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def delete_statement(conn, statement_id: int) -> None:
    conn.execute("DELETE FROM statements WHERE id=?", (statement_id,))
    conn.commit()


# -------------------------------------------------------------- transactions
def _row_to_txn(r: sqlite3.Row) -> Transaction:
    return Transaction(
        id=r["id"], statement_id=r["statement_id"], date=r["date"], description=r["description"],
        merchant_normalized=r["merchant_normalized"], amount_pence=r["amount_pence"],
        direction=r["direction"], account_type=r["account_type"], category=r["category"],
        category_source=r["category_source"], is_transfer=bool(r["is_transfer"]),
        is_recurring=bool(r["is_recurring"]), balance_pence=r["balance_pence"], txn_hash=r["txn_hash"],
    )


def insert_transactions(conn, txns: list[Transaction]) -> tuple[int, int]:
    """Insert, skipping duplicates by txn_hash. Returns (inserted, skipped)."""
    inserted = skipped = 0
    for t in txns:
        cur = conn.execute(
            """INSERT OR IGNORE INTO transactions (statement_id, date, description, merchant_normalized,
                   amount_pence, direction, account_type, category, category_source, is_transfer,
                   is_recurring, balance_pence, txn_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.statement_id, t.date, t.description, t.merchant_normalized, t.amount_pence, t.direction,
             t.account_type, t.category, t.category_source, int(t.is_transfer), int(t.is_recurring),
             t.balance_pence, t.txn_hash),
        )
        if cur.rowcount:
            t.id = cur.lastrowid
            inserted += 1
        else:
            skipped += 1
    return inserted, skipped


def get_transactions(conn, month: str | None = None) -> list[Transaction]:
    if month:
        rows = conn.execute("SELECT * FROM transactions WHERE substr(date,1,7)=? ORDER BY date, id", (month,))
    else:
        rows = conn.execute("SELECT * FROM transactions ORDER BY date, id")
    return [_row_to_txn(r) for r in rows.fetchall()]


def update_transaction(conn, txn_id: int, **fields) -> None:
    allowed = {"category", "category_source", "is_transfer", "is_recurring"}
    sets = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k}=?" for k in sets)
    conn.execute(f"UPDATE transactions SET {cols} WHERE id=?", (*sets.values(), txn_id))


def set_category_for_merchant(conn, merchant: str, category: str) -> int:
    """Apply a user correction to every transaction from a merchant (keeps other user edits)."""
    cur = conn.execute(
        "UPDATE transactions SET category=?, category_source='user' WHERE merchant_normalized=?",
        (category, merchant),
    )
    return cur.rowcount


def save_flags(conn, txns: list[Transaction]) -> None:
    """Persist computed flags (transfer / recurring / category) for many rows."""
    conn.executemany(
        "UPDATE transactions SET is_transfer=?, is_recurring=?, category=?, category_source=? WHERE id=?",
        [(int(t.is_transfer), int(t.is_recurring), t.category, t.category_source, t.id) for t in txns if t.id],
    )
    conn.commit()


def months_available(conn) -> list[str]:
    rows = conn.execute("SELECT DISTINCT substr(date,1,7) AS m FROM transactions ORDER BY m").fetchall()
    return [r["m"] for r in rows]


# --------------------------------------------------------------------- rules
def get_user_rules(conn) -> list[Rule]:
    rows = conn.execute("SELECT * FROM rules ORDER BY priority DESC, id DESC").fetchall()
    return [Rule(id=r["id"], pattern=r["pattern"], match_type=r["match_type"], category=r["category"],
                 priority=r["priority"], source=r["source"]) for r in rows]


def upsert_user_rule(conn, pattern: str, category: str, match_type: str = "contains", priority: int = 100) -> None:
    conn.execute(
        """INSERT INTO rules (pattern, match_type, category, priority, source) VALUES (?,?,?,?, 'user')
           ON CONFLICT(pattern, match_type) DO UPDATE SET category=excluded.category, priority=excluded.priority""",
        (pattern.upper(), match_type, category, priority),
    )
    conn.commit()


def delete_rule(conn, rule_id: int) -> None:
    conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    conn.commit()


# ------------------------------------------------------------------- budgets
def get_budgets(conn) -> dict[str, Budget]:
    rows = conn.execute("SELECT * FROM budgets").fetchall()
    return {r["category"]: Budget(r["category"], r["monthly_limit_pence"], r["alert_threshold_pct"]) for r in rows}


def set_budget(conn, category: str, monthly_limit_pence: int, alert_threshold_pct: int = 80) -> None:
    conn.execute(
        """INSERT INTO budgets (category, monthly_limit_pence, alert_threshold_pct) VALUES (?,?,?)
           ON CONFLICT(category) DO UPDATE SET monthly_limit_pence=excluded.monthly_limit_pence,
               alert_threshold_pct=excluded.alert_threshold_pct""",
        (category, int(monthly_limit_pence), int(alert_threshold_pct)),
    )
    conn.commit()


def delete_budget(conn, category: str) -> None:
    conn.execute("DELETE FROM budgets WHERE category=?", (category,))
    conn.commit()


# ------------------------------------------------------------ merchant cache
def get_cached_categories(conn) -> dict[tuple[str, str], str]:
    rows = conn.execute("SELECT * FROM merchant_cache").fetchall()
    return {(r["merchant_normalized"], r["direction"]): r["category"] for r in rows}


def cache_categories(conn, mapping: dict[tuple[str, str], str]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO merchant_cache (merchant_normalized, direction, category) VALUES (?,?,?)",
        [(m, d, c) for (m, d), c in mapping.items()],
    )
    conn.commit()


# ------------------------------------------------------- recurring overrides
def get_recurring_overrides(conn) -> dict[tuple[str, str], str]:
    rows = conn.execute("SELECT * FROM recurring_overrides").fetchall()
    return {(r["merchant_normalized"], r["direction"]): r["status"] for r in rows}


def set_recurring_override(conn, merchant: str, direction: str, status: str | None) -> None:
    if status is None:
        conn.execute("DELETE FROM recurring_overrides WHERE merchant_normalized=? AND direction=?",
                     (merchant, direction))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO recurring_overrides (merchant_normalized, direction, status) VALUES (?,?,?)",
            (merchant, direction, status),
        )
    conn.commit()
