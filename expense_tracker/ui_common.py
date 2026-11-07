"""Shared Streamlit helpers: DB connection, sidebar alerts, formatting, chart palette."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from expense_tracker.core import db, llm  # noqa: E402
from expense_tracker.core.analytics import budget_alerts  # noqa: E402
from expense_tracker.core.models import Transaction, fmt_gbp  # noqa: E402
from expense_tracker.core.recurring import detect_recurring, recurring_alerts  # noqa: E402

# Validated categorical palette (fixed order - colour follows the entity, never its rank).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"]
OTHER_GREY = "#898781"
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
STATUS_ICON = {"ok": "✅", "warning": "⚠️", "over": "🚨"}


def page_setup(title: str, icon: str = "💷") -> None:
    st.set_page_config(page_title=f"{title} · Expense Tracker", page_icon=icon, layout="wide")


@st.cache_resource
def get_conn():
    return db.connect()


def txns() -> list[Transaction]:
    return db.get_transactions(get_conn())


def to_df(rows: list[Transaction]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["id", "date", "description", "merchant", "account", "direction", "amount",
                                     "category", "recurring", "source"])
    return pd.DataFrame([{
        "id": t.id, "date": pd.to_datetime(t.date), "description": t.description, "merchant": t.merchant_normalized,
        "account": "Credit card" if t.account_type == "credit" else "Debit/current",
        "direction": "Out" if t.direction == "debit" else "In",
        "amount": t.amount_pence / 100, "category": t.category, "recurring": t.is_recurring,
        "source": t.category_source,
    } for t in rows])


def latest_month(rows: list[Transaction]) -> str | None:
    return max((t.month for t in rows), default=None)


def month_label(m: str) -> str:
    return pd.Period(m, freq="M").strftime("%b %Y")


def sidebar_alerts() -> None:
    """Budget + recurring alerts for the latest month, shown on every page."""
    conn = get_conn()
    rows = txns()
    with st.sidebar:
        if not llm.is_configured():
            st.caption("🔌 Claude not configured - using offline parser + rules. Add ANTHROPIC_API_KEY to .env.")
        if not rows:
            st.info("No statements imported yet.")
            return
        month = latest_month(rows)
        st.markdown(f"**Alerts · {month_label(month)}**")
        budgets = db.get_budgets(conn)
        if not budgets:
            st.warning("No budgets set yet.")
            st.page_link("pages/4_Budgets.py", label="Set budgets →", icon="🎯")
        for a in budget_alerts(rows, budgets, month):
            msg = (f"{STATUS_ICON[a.status]} **{a.category}**: {fmt_gbp(a.spent_pence)} of "
                   f"{fmt_gbp(a.limit_pence)} ({a.pct_used:.0f}%)")
            (st.error if a.status == "over" else st.warning)(msg)
        groups = detect_recurring(rows, db.get_recurring_overrides(conn))
        for ra in recurring_alerts(groups, rows, as_of=max(t.date for t in rows)):
            (st.warning if ra.level == "warning" else st.info)(f"🔁 {ra.message}")
        if budgets and not budget_alerts(rows, budgets, month):
            st.success("All categories within budget.")


def gbp_column(label: str, **kw):
    return st.column_config.NumberColumn(label, format="£%.2f", **kw)
