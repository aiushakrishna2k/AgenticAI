import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make ui_common importable

import pandas as pd
import streamlit as st

from ui_common import gbp_column, get_conn, month_label, page_setup, sidebar_alerts, to_df, txns

page_setup("Transactions", "🧾")

from expense_tracker.core import db, importer, llm  # noqa: E402
from expense_tracker.core.models import CATEGORIES, TRANSFER_CATEGORY  # noqa: E402

sidebar_alerts()
conn = get_conn()
st.title("🧾 Transactions")

for kind, text in st.session_state.pop("tx_flash", []):
    getattr(st, kind)(text)

rows = txns()
if not rows:
    st.info("No transactions yet - upload a statement first.")
    st.page_link("pages/1_Upload.py", label="Upload statements", icon="📄")
    st.stop()

df = to_df(rows)
months = sorted(df["date"].dt.strftime("%Y-%m").unique(), reverse=True)

f1, f2, f3, f4 = st.columns([2, 3, 2, 3])
month = f1.selectbox("Month", ["All"] + months, format_func=lambda m: m if m == "All" else month_label(m))
cats = f2.multiselect("Category", CATEGORIES)
acct = f3.selectbox("Account", ["All", "Credit card", "Debit/current"])
search = f4.text_input("Search description", placeholder="e.g. tesco")

view = df
if month != "All":
    view = view[view["date"].dt.strftime("%Y-%m") == month]
if cats:
    view = view[view["category"].isin(cats)]
if acct != "All":
    view = view[view["account"] == acct]
if search:
    view = view[view["description"].str.contains(search, case=False, regex=False)]

out_total = view.loc[(view["direction"] == "Out") & (view["category"] != TRANSFER_CATEGORY), "amount"].sum()
in_total = view.loc[(view["direction"] == "In") & (view["category"] != TRANSFER_CATEGORY), "amount"].sum()
st.caption(f"{len(view)} transactions · out £{out_total:,.2f} · in £{in_total:,.2f} (transfers excluded)")

edited = st.data_editor(
    view.set_index("id"), key="tx-editor", width="stretch", height=560,
    disabled=["date", "description", "merchant", "account", "direction", "amount", "recurring", "source"],
    column_config={
        "date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
        "description": st.column_config.TextColumn("Description", width="large"),
        "merchant": st.column_config.TextColumn("Merchant (normalised)"),
        "account": "Account",
        "direction": "In/Out",
        "amount": gbp_column("Amount"),
        "category": st.column_config.SelectboxColumn(
            "Category", options=CATEGORIES, required=True,
            help="Set to 'Transfers' to exclude a row from income and spending (e.g. moving money to savings)."),
        "recurring": st.column_config.CheckboxColumn("Recurring", help="Detected automatically - manage on Summary"),
        "source": st.column_config.TextColumn("Categorised by", help="rule / llm (Claude) / user / default"),
    },
)

changes = edited["category"][edited["category"] != view.set_index("id")["category"]]
learn = st.checkbox("Remember my changes: apply to every transaction from the same merchant and use for "
                    "future imports", value=True)
if st.button(f"Save {len(changes)} change(s)", type="primary", disabled=changes.empty):
    by_id = {t.id: t for t in rows}
    n_bulk = 0
    for txn_id, cat in changes.items():
        t = by_id[int(txn_id)]
        db.update_transaction(conn, t.id, category=cat, category_source="user", is_transfer=cat == TRANSFER_CATEGORY)
        if learn:
            db.upsert_user_rule(conn, t.merchant_normalized, cat)
            n_bulk += db.set_category_for_merchant(conn, t.merchant_normalized, cat)
    conn.execute("UPDATE transactions SET is_transfer = (category = ?)", (TRANSFER_CATEGORY,))
    conn.commit()
    importer.refresh_derived(conn)
    msg = f"Saved {len(changes)} change(s)."
    if learn:
        msg += f" Updated {n_bulk} matching transaction(s) and saved rule(s) for future imports."
    st.session_state["tx_flash"] = [("success", msg)]
    st.rerun()

st.download_button("Download CSV", view.to_csv(index=False).encode(), "transactions.csv", "text/csv")

with st.expander("My categorisation rules"):
    rules = db.get_user_rules(conn)
    if not rules:
        st.caption("No rules yet. Rules are created when you correct a category above with 'Remember' ticked.")
    for r in rules:
        c1, c2 = st.columns([6, 1])
        c1.write(f"`{r.pattern}` → **{r.category}**")
        if c2.button("Delete", key=f"rule-{r.id}"):
            db.delete_rule(conn, r.id)
            st.rerun()
    with st.form("new-rule", clear_on_submit=True):
        c1, c2 = st.columns(2)
        pattern = c1.text_input("Description contains", placeholder="e.g. PURE GYM")
        category = c2.selectbox("Category", CATEGORIES)
        if st.form_submit_button("Add rule") and pattern.strip():
            db.upsert_user_rule(conn, pattern.strip(), category)
            st.rerun()
    if st.button("Re-apply rules to all transactions",
                 help="Re-categorises everything you haven't edited by hand"
                      + (" (unknown merchants are sent to Claude)" if llm.is_configured() else "")):
        n = importer.recategorize_all(conn)
        st.session_state["tx_flash"] = [("success", f"Re-categorised - {n} transaction(s) changed.")]
        st.rerun()
