"""Description normalisation and categorisation.

Order of precedence for each transaction:
  1. existing user correction (category_source == 'user') is never overwritten
  2. user rules (saved from UI corrections)
  3. default rules (data/default_rules.yaml) - most specific (longest) match wins
  4. cached Claude decision for this merchant + direction
  5. Claude (one batched call for all still-unknown merchants)
  6. fallback: credit into a debit account -> Salary/Income, else Other
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

import yaml

from .models import CATEGORIES, INCOME_CATEGORY, TRANSFER_CATEGORY, Rule, Transaction

DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "default_rules.yaml"

# Bank-inserted prefixes/suffixes that are noise for merchant identity.
_NOISE_TOKENS = {
    "CARD", "PAYMENT", "PAYMENTS", "TO", "FROM", "CONTACTLESS", "VIS", "VISA", "DEBIT", "DD", "SO", "FPI",
    "FPO", "BGC", "DEB", "POS", "CHQ", "TFR", "BP", "CPT", "PURCHASE", "ON", "DIRECT", "STANDING", "ORDER",
    "FASTER", "BILL", "PAYMT", "PYMT", "REF", "GBR", "GB", "UK", "LTD", "PLC", "LIMITED", "WWW", "COM",
    "CO", "INT", "APPLEPAY", "GPAY", "CLP", "MASTERCARD", "MC", "TRANSFER",
}
# Tokens kept even though they look like noise when they *are* the merchant.
_KEEP_IF_ONLY = {"BP"}
_MONTHS = r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*"
_DATE_RE = re.compile(rf"\b\d{{1,2}}[/\-. ]?(?:{_MONTHS}|\d{{1,2}})(?:[/\-. ]?\d{{2,4}})?\b")
_REF_RE = re.compile(r"\b(?:REF|REFERENCE|ID|AUTH)[:\s]*\S+")
_CARD_RE = re.compile(r"[*X]{2,}\d{2,4}|\*\d{3,}")


def normalize_description(desc: str) -> str:
    """Reduce a raw statement description to a stable merchant key.

    'CARD PAYMENT TO TESCO STORES 3021 ON 12 MAR' -> 'TESCO STORES'
    """
    s = desc.upper()
    s = _CARD_RE.sub(" ", s)
    s = _REF_RE.sub(" ", s)
    s = _DATE_RE.sub(" ", s)
    s = re.sub(r"\.(?:CO\.UK|COM|NET|ORG|IO|UK)\b", " ", s)  # NETFLIX.COM -> NETFLIX
    s = re.sub(r"[^A-Z0-9&.'+ ]", " ", s)
    tokens = [t.strip(".'") for t in s.split()]
    tokens = [t for t in tokens if t and not any(ch.isdigit() for ch in t)]
    kept = [t for t in tokens if t not in _NOISE_TOKENS]
    if not kept:
        kept = [t for t in tokens if t in _KEEP_IF_ONLY] or tokens[:1]
    return " ".join(kept[:3]) or desc.upper().strip()[:30]


def _compile(pattern: str, match_type: str) -> re.Pattern:
    if match_type == "regex":
        return re.compile(pattern, re.IGNORECASE)
    return re.compile(rf"(?<![A-Z0-9]){re.escape(pattern.upper())}(?![A-Z0-9])")


@lru_cache(maxsize=1)
def load_default_rules(path: str = str(DEFAULT_RULES_PATH)) -> tuple[Rule, ...]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    rules = []
    for category, patterns in data.items():
        if category not in CATEGORIES:
            raise ValueError(f"default_rules.yaml: unknown category {category!r}")
        for p in patterns:
            rules.append(Rule(pattern=str(p).upper(), category=category, source="default"))
    return tuple(rules)


@lru_cache(maxsize=4096)
def _rx(pattern: str, match_type: str) -> re.Pattern:
    return _compile(pattern, match_type)


def match_rules(text: str, rules: Iterable[Rule]) -> Rule | None:
    """Return the best matching rule: highest priority, then longest pattern."""
    best: Rule | None = None
    for r in rules:
        try:
            hit = _rx(r.pattern, r.match_type).search(text)
        except re.error:
            continue
        if hit and (best is None or (r.priority, len(r.pattern)) > (best.priority, len(best.pattern))):
            best = r
    return best


def rule_category(txn: Transaction, user_rules: list[Rule], default_rules: Iterable[Rule]) -> tuple[str, str] | None:
    haystack = f"{txn.merchant_normalized} | {txn.description.upper()}"
    for rules, source in ((user_rules, "user_rule"), (default_rules, "rule")):
        r = match_rules(haystack, rules)
        if r:
            return r.category, ("rule" if source == "rule" else "user")
    return None


def fallback_category(txn: Transaction) -> str:
    if txn.direction == "credit" and txn.account_type == "debit":
        return INCOME_CATEGORY
    return "Other"


LLMCategorizer = Callable[[list[dict]], dict[tuple[str, str], str]]


def categorize(
    txns: list[Transaction],
    user_rules: list[Rule],
    cache: dict[tuple[str, str], str],
    llm: LLMCategorizer | None = None,
    default_rules: Iterable[Rule] | None = None,
) -> dict[tuple[str, str], str]:
    """Categorise txns in place. Returns new merchant->category decisions from the LLM (to cache)."""
    default_rules = load_default_rules() if default_rules is None else default_rules
    unknown: dict[tuple[str, str], Transaction] = {}

    for t in txns:
        if not t.merchant_normalized:
            t.merchant_normalized = normalize_description(t.description)
        if t.category_source == "user":
            continue
        hit = rule_category(t, user_rules, default_rules)
        if hit:
            t.category, t.category_source = hit
            continue
        key = (t.merchant_normalized, t.direction)
        if key in cache:
            t.category, t.category_source = cache[key], "llm"
            continue
        unknown.setdefault(key, t)

    learned: dict[tuple[str, str], str] = {}
    if unknown and llm is not None:
        items = [{"merchant": m, "direction": d, "example": t.description, "account_type": t.account_type}
                 for (m, d), t in unknown.items()]
        try:
            learned = {k: v for k, v in llm(items).items() if v in CATEGORIES}
        except Exception as exc:  # LLM is best-effort; rules + fallback still work
            print(f"[categorizer] LLM categorisation failed, using fallback: {exc}")
            learned = {}

    for t in txns:
        if t.category_source == "user":
            continue
        key = (t.merchant_normalized, t.direction)
        if key in unknown:
            if key in learned:
                t.category, t.category_source = learned[key], "llm"
            else:
                t.category, t.category_source = fallback_category(t), "default"
    for t in txns:  # invariant: a row is a transfer iff its category is Transfers
        t.is_transfer = t.category == TRANSFER_CATEGORY
    return learned
