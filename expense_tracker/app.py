"""Home page. Run with:  streamlit run expense_tracker/app.py"""
import streamlit as st

from ui_common import get_conn, latest_month, month_label, page_setup, sidebar_alerts, txns

page_setup("Home")

from expense_tracker.core import db, importer, llm  # noqa: E402
from expense_tracker.core.analytics import monthly_savings  # noqa: E402
from expense_tracker.core.models import fmt_gbp  # noqa: E402

sidebar_alerts()
st.title("💷 Expense Tracker")
st.caption("Upload UK credit/debit card PDF statements → auto-categorised spending, recurring bills, "
           "monthly savings and budget alerts. Amounts in GBP.")

conn = get_conn()
rows = txns()

if not rows:
    st.info("Start by uploading your statements.")
    st.page_link("pages/1_Upload.py", label="Upload statements", icon="📄")
else:
    m = latest_month(rows)
    s = {x.month: x for x in monthly_savings(rows)}[m]
    st.subheader(month_label(m))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Income", fmt_gbp(s.income_pence))
    c2.metric("Spending", fmt_gbp(s.spending_pence))
    c3.metric("Savings", fmt_gbp(s.savings_pence))
    c4.metric("Savings rate", "—" if s.savings_rate is None else f"{s.savings_rate:.0%}")

    cols = st.columns(4)
    cols[0].page_link("pages/1_Upload.py", label="Upload", icon="📄")
    cols[1].page_link("pages/2_Transactions.py", label="Transactions", icon="🧾")
    cols[2].page_link("pages/3_Summary.py", label="Summary", icon="📊")
    cols[3].page_link("pages/4_Budgets.py", label="Budgets", icon="🎯")

st.divider()
st.subheader("Imported statements")
stmts = db.list_statements(conn)
if not stmts:
    st.caption("None yet.")
for s in stmts:
    c1, c2 = st.columns([6, 1])
    acct = "Credit card" if s["account_type"] == "credit" else "Debit/current"
    c1.write(f"**{s['file_name']}** · {s['bank'] or 'Unknown bank'} · {acct} · "
             f"{s['period_start'] or '?'} → {s['period_end'] or '?'} · {s['n_transactions']} transactions")
    if c2.button("Delete", key=f"del-{s['id']}"):
        db.delete_statement(conn, s["id"])
        importer.refresh_derived(conn)
        st.rerun()

with st.expander("Settings"):
    st.write(f"Claude model: `{llm.MODEL}` · "
             f"{'✅ API key found' if llm.is_configured() else '❌ no API key (offline mode)'}")
    st.write(f"Database: `{db.DEFAULT_DB_PATH}`")
