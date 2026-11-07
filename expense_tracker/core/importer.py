"""Import pipeline: PDF bytes -> extracted + categorised preview -> saved -> derived flags refreshed."""
from __future__ import annotations

from . import db, llm
from .categorizer import categorize, normalize_description
from .dedupe import assign_hashes, detect_transfers
from .models import TRANSFER_CATEGORY, ExtractionResult
from .pdf_parser import extract_pages, file_hash, guess_account_type, guess_bank, heuristic_parse, reconcile
from .recurring import apply_recurring_flags, detect_recurring


def parse_pdf(data: bytes, file_name: str, account_type_hint: str | None = None,
              use_llm: bool | None = None) -> ExtractionResult:
    """Extract transactions from a statement PDF (Claude if configured, else heuristic parser)."""
    pages = extract_pages(data)
    fhash = file_hash(data)
    full = "\n".join(pages)
    use_llm = llm.is_configured() if use_llm is None else use_llm

    if use_llm:
        result = llm.extract_statement(pages, file_name, fhash, account_type_hint)
    else:
        from .models import Statement

        acct = account_type_hint or guess_account_type(full)
        result = ExtractionResult(
            statement=Statement(file_name=file_name, file_hash=fhash, account_type=acct, bank=guess_bank(full)),
            transactions=heuristic_parse(pages, acct), method="heuristic",
        )
    if not result.statement.bank:
        result.statement.bank = guess_bank(full)
    dates = sorted(t.date for t in result.transactions)
    if dates:
        result.statement.period_start = result.statement.period_start or dates[0]
        result.statement.period_end = result.statement.period_end or dates[-1]
    result.reconciled, result.reconcile_message = reconcile(result.statement, result.transactions)
    return result


def prepare(conn, result: ExtractionResult, use_llm: bool | None = None) -> None:
    """Normalise, categorise and hash the extracted rows in place (for the preview)."""
    use_llm = llm.is_configured() if use_llm is None else use_llm
    for t in result.transactions:
        t.account_type = result.statement.account_type
        t.merchant_normalized = normalize_description(t.description)
    learned = categorize(
        result.transactions,
        user_rules=db.get_user_rules(conn),
        cache=db.get_cached_categories(conn),
        llm=llm.categorize_merchants if use_llm else None,
    )
    if learned:
        db.cache_categories(conn, learned)
    detect_transfers(result.transactions)
    assign_hashes(result.transactions)


def save(conn, result: ExtractionResult) -> dict:
    """Persist a confirmed import and refresh cross-statement flags. Returns counts."""
    if db.statement_exists(conn, result.statement.file_hash):
        return {"inserted": 0, "skipped": len(result.transactions), "duplicate_file": True}
    sid = db.insert_statement(conn, result.statement)
    for t in result.transactions:
        t.statement_id = sid
        t.account_type = result.statement.account_type
    inserted, skipped = db.insert_transactions(conn, result.transactions)
    conn.commit()
    refresh_derived(conn)
    return {"inserted": inserted, "skipped": skipped, "duplicate_file": False, "statement_id": sid}


def refresh_derived(conn) -> None:
    """Re-run transfer matching and recurring detection across ALL stored transactions."""
    txns = db.get_transactions(conn)
    for t in txns:  # invariant: transfer iff category is Transfers
        t.is_transfer = t.category == TRANSFER_CATEGORY
    detect_transfers(txns)
    groups = detect_recurring(txns, db.get_recurring_overrides(conn))
    apply_recurring_flags(txns, groups)
    db.save_flags(conn, txns)


def recategorize_all(conn, use_llm: bool | None = None) -> int:
    """Re-apply rules/cache/LLM to every non-user-edited transaction (e.g. after adding a rule)."""
    use_llm = llm.is_configured() if use_llm is None else use_llm
    txns = db.get_transactions(conn)
    before = {t.id: t.category for t in txns}
    learned = categorize(txns, db.get_user_rules(conn), db.get_cached_categories(conn),
                         llm=llm.categorize_merchants if use_llm else None)
    if learned:
        db.cache_categories(conn, learned)
    db.save_flags(conn, txns)
    refresh_derived(conn)
    return sum(1 for t in db.get_transactions(conn) if before.get(t.id) != t.category)
