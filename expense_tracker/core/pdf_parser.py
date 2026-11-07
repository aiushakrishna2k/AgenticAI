"""PDF text extraction, privacy masking, a heuristic fallback parser and balance reconciliation.

Primary extraction is done by Claude (core/llm.py) because every UK bank lays its PDF out
differently. The heuristic parser here is the no-API-key fallback and is best effort.
"""
from __future__ import annotations

import hashlib
import io
import re
from collections import Counter
from datetime import date

from .models import Statement, Transaction, to_pence


class ScannedPDFError(ValueError):
    """Raised when a PDF has no extractable text (image-only scan)."""


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_pages(data: bytes) -> list[str]:
    """Return the text of each page. Tables are rendered by pdfplumber's layout-aware text."""
    import pdfplumber  # imported lazily so core logic is testable without it

    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(layout=True) or ""
            # collapse layout padding but keep column separation (2+ spaces -> 3 spaces)
            lines = [ln.rstrip() for ln in text.splitlines()]
            while lines and not lines[-1]:
                lines.pop()
            margin = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
            text = "\n".join(re.sub(r"(?<=\S) {2,}", "   ", ln[margin:]) for ln in lines)
            pages.append(text)
    if not any(p.strip() for p in pages):
        raise ScannedPDFError(
            "No text found in this PDF - it looks like a scanned image. Scanned statements are not "
            "supported yet; download the statement as a digital PDF from online banking instead."
        )
    return pages


# Long digit runs: card numbers (16), account numbers (8), sort codes (12-34-56), IBANs.
_SORT_CODE_RE = re.compile(r"\b\d{2}-\d{2}-\d{2}\b")
_IBAN_RE = re.compile(r"\bGB\d{2}[A-Z]{4}\d{14}\b")
_LONG_NUM_RE = re.compile(r"\b(?:\d[ -]?){8,19}\b")


def mask_sensitive(text: str) -> str:
    """Mask account/card numbers and sort codes before text leaves the machine (keep last 4)."""
    text = _IBAN_RE.sub(lambda m: "GB**" + "*" * 14 + m.group(0)[-4:], text)
    text = _SORT_CODE_RE.sub("**-**-**", text)

    def _mask(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 8:
            return m.group(0)
        return "*" * (len(digits) - 4) + digits[-4:]

    return _LONG_NUM_RE.sub(_mask, text)


_CREDIT_HINTS = re.compile(
    r"credit\s+limit|minimum\s+payment|available\s+credit|card\s+statement|new\s+balance|"
    r"payment\s+due|credit\s+card|apr\b|interest\s+rate", re.IGNORECASE)
_DEBIT_HINTS = re.compile(
    r"current\s+account|sort\s+code|money\s+in|money\s+out|paid\s+in|paid\s+out|overdraft|"
    r"balance\s+brought\s+forward|debit\s+card", re.IGNORECASE)


def guess_account_type(text: str) -> str:
    c = len(_CREDIT_HINTS.findall(text))
    d = len(_DEBIT_HINTS.findall(text))
    return "credit" if c > d else "debit"


_KNOWN_BANKS = [
    "Barclaycard", "Barclays", "HSBC", "Lloyds", "Halifax", "NatWest", "Santander", "Nationwide",
    "Monzo", "Starling", "Revolut", "TSB", "Metro Bank", "Virgin Money", "American Express",
    "Amex", "Capital One", "MBNA", "First Direct", "Co-operative Bank", "Tesco Bank", "Chase",
    "Royal Bank of Scotland", "Bank of Scotland", "Marcus", "Zopa", "John Lewis",
]


def guess_bank(text: str) -> str:
    head = text[:4000].lower()
    hits = [(head.find(b.lower()), b) for b in _KNOWN_BANKS if b.lower() in head]
    return min(hits)[1] if hits else ""


# ------------------------------------------------------------ heuristic parser
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE_NUM = r"(?P<d1>\d{1,2})[/.-](?P<m1>\d{1,2})(?:[/.-](?P<y1>\d{2,4}))?"
_DATE_TXT = r"(?P<d2>\d{1,2})\s*(?P<m2>[A-Za-z]{3})[a-z]*\.?(?:\s*(?P<y2>\d{2,4}))?"
_LINE_DATE_RE = re.compile(rf"^\s*(?:{_DATE_NUM}|{_DATE_TXT})\b")
_SUMMARY_RE = re.compile(
    r"\b(?:credit limit|overdraft limit|money in|money out|total|minimum payment|payment due|"
    r"available credit|interest rate|previous balance|new balance|statement balance)\b", re.IGNORECASE)
_MONEY_RE = re.compile(r"(?<![\d.])-?£?\d{1,3}(?:,\d{3})*\.\d{2}(?:\s?(?:CR|DR|Cr|Dr))?(?![\d])")


def _mk_date(day: int, month: int, year: int | None, default_year: int) -> str | None:
    y = year if year is not None else default_year
    if y < 100:
        y += 2000
    try:
        return date(y, month, day).isoformat()
    except ValueError:
        return None


def _dominant_year(text: str) -> int:
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", text)]
    return Counter(years).most_common(1)[0][0] if years else date.today().year


def _parse_balance(tok: str) -> int:
    """Signed balance in pence: '-285.28' or '285.28 DR' (overdrawn) -> negative."""
    t = tok.strip()
    neg = t.startswith("-") or t[-2:].upper() == "DR"
    pence = to_pence(re.sub(r"(?i)\s?(CR|DR)$", "", t).lstrip("-"))
    return -pence if neg else pence


def _parse_money(tok: str) -> tuple[int, bool | None]:
    """-> (pence, is_credit) ; is_credit None when unmarked."""
    t = tok.strip()
    marker = None
    if t[-2:].upper() in ("CR", "DR"):
        marker = t[-2:].upper() == "CR"
        t = t[:-2].strip()
    neg = t.startswith("-")
    pence = to_pence(t.lstrip("-"))
    if neg and marker is None:
        marker = True  # negative amounts on UK card statements are usually credits
    return pence, marker


def heuristic_parse(pages: list[str], account_type: str) -> list[Transaction]:
    """Best-effort line parser: a line starting with a date and ending in one or two amounts."""
    full = "\n".join(pages)
    year = _dominant_year(full)
    txns: list[Transaction] = []
    prev_balance: int | None = None
    last_date: str | None = None
    for line in full.splitlines():
        if not _MONEY_RE.search(line) or _SUMMARY_RE.search(line):
            continue
        m = _LINE_DATE_RE.match(line)
        if m:
            if m.group("d1"):
                iso = _mk_date(int(m.group("d1")), int(m.group("m1")),
                               int(m.group("y1")) if m.group("y1") else None, year)
            else:
                mon = _MONTHS.get(m.group("m2").lower()[:3])
                iso = _mk_date(int(m.group("d2")), mon, int(m.group("y2")) if m.group("y2") else None,
                               year) if mon else None
            if not iso:
                continue
            last_date = iso
            rest = line[m.end():]
        elif last_date and re.match(r"^\s{2,}\S", line):
            rest = line  # continuation line sharing the previous date (common on current accounts)
        else:
            continue

        monies = list(_MONEY_RE.finditer(rest))
        if not monies:
            continue
        if not m and account_type == "debit" and len(monies) != 2:
            continue  # continuation rows on current accounts carry exactly amount + balance
        desc = re.sub(r"\s{2,}", " ", rest[:monies[0].start()]).strip(" -")
        if not desc or re.search(r"balance\s+(brought|carried)\s+forward|opening balance|closing balance",
                                 desc, re.IGNORECASE):
            prev_balance = _parse_balance(monies[-1].group(0))
            continue

        amount, is_credit = _parse_money(monies[0].group(0))
        balance = None
        if len(monies) >= 2:
            balance = _parse_balance(monies[-1].group(0))
            if is_credit is None and prev_balance is not None:
                delta = balance - prev_balance
                if account_type == "debit":
                    is_credit = delta > 0
                else:
                    is_credit = delta < 0
            prev_balance = balance
        if is_credit is None:
            is_credit = bool(re.search(r"\b(payment received|refund|thank you|salary|credit)\b", desc, re.I))
        txns.append(Transaction(
            date=last_date, description=desc, amount_pence=amount,
            direction="credit" if is_credit else "debit", account_type=account_type, balance_pence=balance,
        ))
    return txns


# ---------------------------------------------------------------- reconcile
def reconcile(statement: Statement, txns: list[Transaction]) -> tuple[bool | None, str]:
    """Check opening/closing balances against the extracted rows.

    debit account: opening + credits - debits == closing
    credit card  : opening + debits - credits == closing   (balance = amount owed)
    """
    o, c = statement.opening_balance_pence, statement.closing_balance_pence
    if o is None or c is None:
        return None, "Statement balances not found - could not reconcile. Please eyeball the rows."
    credits = sum(t.amount_pence for t in txns if t.direction == "credit")
    debits = sum(t.amount_pence for t in txns if t.direction == "debit")
    expected = o + credits - debits if statement.account_type == "debit" else o + debits - credits
    diff = c - expected
    if diff == 0:
        return True, "Balances reconcile exactly."
    return False, (f"Balances do not reconcile: expected closing {expected / 100:,.2f}, statement says "
                   f"{c / 100:,.2f} (difference {diff / 100:,.2f}). Some rows may be missing or mis-signed.")
