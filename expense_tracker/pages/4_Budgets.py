import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make ui_common importable

import pandas as pd
import streamlit as st

from ui_common import STATUS_ICON, gbp_column, get_conn, latest_month, month_label, page_setup, sidebar_alerts, txns

page_setup("Budgets", "🎯")

from expense_tracker.core import db  # noqa: E402
from expense_tracker.core.analytics import budget_status, suggest_budgets  # noqa: E402
from expense_tracker.core.models import SPEND_CATEGORIES, fmt_gbp, to_pence  # noqa: E402

sidebar_alerts()
conn = get_conn()
st.title("🎯 Budgets")
st.caption("Set a monthly budget for each spending category. You'll get a warning when a category reaches its "
           "alert threshold and an alert when it goes over.")

for kind, text in st.session_state.pop("b_flash", []):
    getattr(st, kind)(text)

rows = txns()
budgets = db.get_budgets(conn)
suggested = suggest_budgets(rows)

# ------------------------------------------------------------- budget editor
st.subheader("Monthly budget per category")
table = pd.DataFrame([{
    "Category": c,
    "Suggested": suggested.get(c, 0) / 100 if suggested else None,
    "Budget": budgets[c].monthly_limit_pence / 100 if c in budgets else None,
    "Alert at %": budgets[c].alert_threshold_pct if c in budgets else 80,
} for c in SPEND_CATEGORIES])
if suggested:
    st.caption("**Suggested** = your average spend over the last 3 months, rounded up to the next £10. "
               "Leave Budget empty for categories you don't want to track.")

if st.button("Fill empty budgets with suggestions", disabled=not suggested):
    for c, p in suggested.items():
        if c in SPEND_CATEGORIES and c not in budgets and p > 0:
            db.set_budget(conn, c, p)
    st.session_state["b_flash"] = [("success", "Suggested budgets applied - adjust them below if needed.")]
    st.rerun()

edited = st.data_editor(
    table, hide_index=True, width="stretch", key="budget-editor", height=38 * (len(table) + 1),
    disabled=["Category", "Suggested"],
    column_config={
        "Suggested": gbp_column("Suggested (avg)"),
        "Budget": gbp_column("Monthly budget", min_value=0.0, step=10.0),
        "Alert at %": st.column_config.NumberColumn("Warn at %", min_value=10, max_value=100, step=5,
                                                    help="Warn when spending reaches this % of the budget"),
    },
)
if st.button("Save budgets", type="primary"):
    n = 0
    for r in edited.to_dict("records"):
        if r["Budget"] is None or pd.isna(r["Budget"]):
            if r["Category"] in budgets:
                db.delete_budget(conn, r["Category"])
            continue
        thr = 80 if pd.isna(r["Alert at %"]) else int(r["Alert at %"])
        db.set_budget(conn, r["Category"], to_pence(round(float(r["Budget"]), 2)), thr)
        n += 1
    st.session_state.pop("ask_budgets", None)
    st.session_state["b_flash"] = [("success", f"Saved {n} budget(s).")]
    st.rerun()

# -------------------------------------------------------------- progress view
if not rows or not budgets:
    st.stop()

st.divider()
months = sorted({t.month for t in rows}, reverse=True)
month = st.selectbox("Progress for", months, format_func=month_label)
status = budget_status(rows, budgets, month)
total_budget = sum(b.limit_pence for b in status)
total_spent = sum(b.spent_pence for b in status)
c1, c2, c3 = st.columns(3)
c1.metric("Total budgeted", fmt_gbp(total_budget))
c2.metric("Spent in budgeted categories", fmt_gbp(total_spent))
c3.metric("Remaining", fmt_gbp(total_budget - total_spent))

for b in status:
    label = {"ok": "on track", "warning": "nearing limit", "over": "over budget"}[b.status]
    left, right = st.columns([3, 2])
    left.markdown(f"**{b.category}** · {STATUS_ICON[b.status]} {label}")
    left.progress(min(1.0, b.pct_used / 100))
    right.markdown(
        f"{fmt_gbp(b.spent_pence)} of {fmt_gbp(b.limit_pence)} ({b.pct_used:.0f}%)  \n"
        + (f":red[over by {fmt_gbp(-b.remaining_pence)}]" if b.status == "over"
           else f"{fmt_gbp(b.remaining_pence)} left"))
