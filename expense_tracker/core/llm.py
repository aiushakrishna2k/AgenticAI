"""Claude integration: statement extraction and merchant categorisation.

Uses structured outputs (output_config.format = JSON schema) so responses are always valid
JSON, and server-side refusal fallbacks (fallbacks="default") so a rare policy decline is
retried on another model inside the same call.
"""
from __future__ import annotations

import json
import os

from .models import ACCOUNT_TYPES, CATEGORIES, ExtractionResult, Statement, Transaction, to_pence
from .pdf_parser import mask_sensitive

MODEL = os.getenv("EXPENSE_TRACKER_MODEL", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


_client = None


def get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()
    return _client


def _nullable(t: str) -> dict:
    return {"anyOf": [{"type": t}, {"type": "null"}]}


def _call_json(system: str, user: str, schema: dict, effort: str, client=None) -> dict:
    """Run one structured-output request and return the parsed JSON object."""
    import anthropic

    client = client or get_client()
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise LLMError("Anthropic API key is invalid - check ANTHROPIC_API_KEY in .env") from e
    except anthropic.RateLimitError as e:
        raise LLMError("Rate limited by the Anthropic API - wait a minute and retry.") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("Could not reach the Anthropic API - check your internet connection.") from e

    if msg.stop_reason == "refusal":
        raise LLMError("Claude declined to process this request.")
    if msg.stop_reason == "max_tokens":
        raise LLMError("Response was truncated (statement too long). Try splitting the PDF.")
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if not text:
        raise LLMError("Empty response from Claude.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"Claude returned invalid JSON: {e}") from e


# ----------------------------------------------------------------- extraction
EXTRACTION_SYSTEM = """You extract transactions from UK bank and credit card statements (GBP).

Rules:
- Return EVERY transaction row exactly once. Do not include balance brought/carried forward,
  opening/closing balance lines, sub-totals, or summary boxes as transactions.
- Dates are UK format DD/MM/YYYY (or '12 Mar'). Output ISO YYYY-MM-DD. When a row has no year,
  infer it from the statement period (a December row in a statement ending in January belongs
  to the previous year).
- amount is always a positive number in pounds (e.g. 12.30). direction is "debit" for money out
  / purchases / fees / interest charged, and "credit" for money in / payments received / refunds.
  Use column headings (Paid out / Paid in, Money out / Money in, Debit / Credit) and CR/DR
  suffixes to decide. On credit card statements, purchases are debits and payments/refunds
  (often marked CR or shown negative) are credits.
- description: the merchant/payee text as printed, joined onto one line, without the date or amounts.
- balance: running balance on that row if printed, else null.
- account_type: "credit" for a credit card statement, "debit" for a current/debit account.
- opening_balance / closing_balance: the statement's previous and new balance in pounds as
  positive numbers (for a credit card: amount owed), or null if not shown. If an overdrawn
  current-account balance is shown (OD / negative) return it as a negative number.
- Account numbers are masked with *; never try to reconstruct them."""

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "bank": {"type": "string"},
        "account_type": {"type": "string", "enum": list(ACCOUNT_TYPES)},
        "period_start": _nullable("string"),
        "period_end": _nullable("string"),
        "opening_balance": _nullable("number"),
        "closing_balance": _nullable("number"),
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "description": {"type": "string"},
                    "amount": {"type": "number"},
                    "direction": {"type": "string", "enum": ["debit", "credit"]},
                    "balance": _nullable("number"),
                },
                "required": ["date", "description", "amount", "direction", "balance"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["bank", "account_type", "period_start", "period_end", "opening_balance",
                 "closing_balance", "transactions"],
    "additionalProperties": False,
}


def _opt_pence(v) -> int | None:
    return None if v is None else to_pence(v)


def build_extraction(data: dict, file_name: str, fhash: str, account_type_hint: str | None) -> ExtractionResult:
    """Validate Claude's JSON and turn it into domain objects (separate for testability)."""
    from datetime import date

    acct = account_type_hint or data.get("account_type") or "debit"
    stmt = Statement(
        file_name=file_name, file_hash=fhash, account_type=acct, bank=data.get("bank") or "",
        period_start=data.get("period_start"), period_end=data.get("period_end"),
        opening_balance_pence=_opt_pence(data.get("opening_balance")),
        closing_balance_pence=_opt_pence(data.get("closing_balance")),
    )
    txns: list[Transaction] = []
    for row in data.get("transactions", []):
        try:
            iso = date.fromisoformat(row["date"]).isoformat()
            amount = to_pence(abs(float(row["amount"])))
        except (KeyError, ValueError, TypeError):
            continue  # drop malformed rows; reconciliation will flag the gap
        if amount == 0:
            continue
        txns.append(Transaction(
            date=iso, description=" ".join(str(row.get("description", "")).split()),
            amount_pence=amount, direction=row.get("direction", "debit"), account_type=acct,
            balance_pence=_opt_pence(row.get("balance")),
        ))
    return ExtractionResult(statement=stmt, transactions=txns, method="llm")


def extract_statement(pages: list[str], file_name: str, fhash: str,
                      account_type_hint: str | None = None, client=None) -> ExtractionResult:
    body = "\n\n".join(f"=== Page {i} ===\n{mask_sensitive(p)}" for i, p in enumerate(pages, 1))
    hint = f"\nThe user says this is a {account_type_hint} account statement." if account_type_hint else ""
    user = f"Extract all transactions from this statement.{hint}\n\n<statement>\n{body}\n</statement>"
    data = _call_json(EXTRACTION_SYSTEM, user, EXTRACTION_SCHEMA, effort="medium", client=client)
    return build_extraction(data, file_name, fhash, account_type_hint)


# ------------------------------------------------------------- categorisation
CATEGORY_SYSTEM = f"""You categorise UK personal bank/credit-card transactions.
Allowed categories: {", ".join(CATEGORIES)}.
- "credit" direction means money received. Employer names / BACS credits on a current account are
  usually Salary/Income; credits from a shop are refunds and take that shop's spending category.
- "Transfers" is only for moving money between the user's own accounts (e.g. savings pots,
  "TO A/C", ISA top-ups). Credit card repayments made from a current account should be "Other"
  (the app reclassifies them automatically once the matching card statement is loaded).
- Use "Other" only when you genuinely cannot tell."""


def categorize_merchants(items: list[dict], client=None) -> dict[tuple[str, str], str]:
    """items: [{merchant, direction, example, account_type}] -> {(merchant, direction): category}"""
    if not items:
        return {}
    schema = {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "category": {"type": "string", "enum": CATEGORIES},
                    },
                    "required": ["id", "category"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }
    lines = [
        {"id": i, "merchant": it["merchant"], "direction": it["direction"],
         "account": it.get("account_type", ""), "example": mask_sensitive(it.get("example", ""))}
        for i, it in enumerate(items)
    ]
    user = ("Categorise each merchant. Return one result per id.\n\n" + json.dumps(lines, indent=0))
    data = _call_json(CATEGORY_SYSTEM, user, schema, effort="low", client=client)
    out: dict[tuple[str, str], str] = {}
    for r in data.get("results", []):
        i = r.get("id")
        if isinstance(i, int) and 0 <= i < len(items) and r.get("category") in CATEGORIES:
            out[(items[i]["merchant"], items[i]["direction"])] = r["category"]
    return out
