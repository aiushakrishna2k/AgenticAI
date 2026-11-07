import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make ui_common importable

import pandas as pd
import streamlit as st

from ui_common import gbp_column, get_conn, month_label, page_setup, sidebar_alerts, txns

page_setup("Upload", "📄")

from expense_tracker.core import db, importer, llm  # noqa: E402
from expense_tracker.core.analytics import budget_alerts  # noqa: E402
from expense_tracker.core.categorizer import normalize_description  # noqa: E402
from expense_tracker.core.dedupe import assign_hashes, detect_transfers  # noqa: E402
from expense_tracker.core.models import CATEGORIES, TRANSFER_CATEGORY, Transaction, fmt_gbp, to_pence  # noqa: E402
from expense_tracker.core.pdf_parser import ScannedPDFError, file_hash, reconcile  # noqa: E402

sidebar_alerts()
conn = get_conn()
st.title("📄 Upload statements")
st.caption("PDF statements from any UK bank. "
           + ("Claude reads the statement layout; only masked statement text is sent (account numbers → last 4)."
              if llm.is_configured() else
              "Offline mode: a best-effort parser is used. Add ANTHROPIC_API_KEY to .env for reliable extraction."))

pending: dict = st.session_state.setdefault("pending", {})

# Messages from the previous run (set just before st.rerun()).
for kind, text in st.session_state.pop("flash", []):
    {"success": st.success, "warning": st.warning, "error": st.error}[kind](text)
if st.session_state.get("ask_budgets") and not db.get_budgets(conn):
    st.info("🎯 You haven't set any budgets yet. Set a monthly budget for each category to get overspend alerts.")
    st.page_link("pages/4_Budgets.py", label="Set budgets now", icon="🎯")

ACCT_OPTIONS = {"Auto-detect": None, "Credit card": "credit", "Debit card / current account": "debit"}
files = st.file_uploader("Statement PDFs", type=["pdf"], accept_multiple_files=True)
acct_choice = st.radio("Account type", list(ACCT_OPTIONS), horizontal=True,
                       help="Applies to the files being extracted now; you can change it per statement below.")

if files and st.button("Extract transactions", type="primary"):
    for f in files:
        data = f.getvalue()
        h = file_hash(data)
        if db.statement_exists(conn, h):
            st.warning(f"**{f.name}** was already imported - skipped.")
            continue
        if h in pending:
            continue
        with st.spinner(f"Reading {f.name}…"):
            try:
                res = importer.parse_pdf(data, f.name, ACCT_OPTIONS[acct_choice])
                importer.prepare(conn, res)
            except ScannedPDFError as e:
                st.error(f"**{f.name}**: {e}")
                continue
            except llm.LLMError as e:
                st.error(f"**{f.name}**: {e}")
                continue
        pending[h] = res

if not pending:
    st.stop()

st.divider()
st.subheader("Review before saving")
st.caption("Check the rows, fix categories or amounts, then save. Nothing is stored until you click Save.")


def result_to_df(res) -> pd.DataFrame:
    return pd.DataFrame([{
        "date": pd.to_datetime(t.date).date(), "description": t.description,
        "amount": t.amount_pence / 100, "direction": "Out" if t.direction == "debit" else "In",
        "category": t.category, "source": t.category_source,
    } for t in res.transactions])


def df_to_txns(df: pd.DataFrame, res, acct: str) -> list[Transaction]:
    orig = {(str(pd.to_datetime(t.date).date()), t.description): t for t in res.transactions}
    out = []
    for r in df.to_dict("records"):
        if pd.isna(r.get("date")) or not r.get("description") or pd.isna(r.get("amount")):
            continue
        d = str(pd.to_datetime(r["date"]).date())
        before = orig.get((d, r["description"]))
        source = r.get("source") or "user"
        if before is None or before.category != r["category"]:
            source = "user"
        out.append(Transaction(
            date=d, description=str(r["description"]).strip(), amount_pence=abs(to_pence(round(float(r["amount"]), 2))),
            direction="debit" if r["direction"] == "Out" else "credit", account_type=acct,
            merchant_normalized=normalize_description(str(r["description"])), category=r["category"],
            category_source=source, is_transfer=r["category"] == TRANSFER_CATEGORY,
            balance_pence=before.balance_pence if before else None,
        ))
    return out


for h, res in list(pending.items()):
    s = res.statement
    with st.container(border=True):
        head, acct_col, btns = st.columns([4, 2, 2])
        head.markdown(f"#### {s.file_name}")
        head.caption(f"{s.bank or 'Unknown bank'} · {s.period_start or '?'} → {s.period_end or '?'} · "
                     f"{len(res.transactions)} rows · extracted by "
                     f"{'Claude' if res.method == 'llm' else 'offline parser'}")
        acct = acct_col.selectbox("Account type", ["credit", "debit"], index=0 if s.account_type == "credit" else 1,
                                  format_func=lambda a: "Credit card" if a == "credit" else "Debit/current",
                                  key=f"acct-{h}")

        edited = st.data_editor(
            result_to_df(res), key=f"ed-{h}", num_rows="dynamic", width="stretch", hide_index=True,
            column_config={
                "date": st.column_config.DateColumn("Date", format="DD/MM/YYYY", required=True),
                "description": st.column_config.TextColumn("Description", required=True, width="large"),
                "amount": gbp_column("Amount", min_value=0.0, required=True),
                "direction": st.column_config.SelectboxColumn("In/Out", options=["Out", "In"], required=True),
                "category": st.column_config.SelectboxColumn("Category", options=CATEGORIES, required=True),
                "source": st.column_config.TextColumn("Categorised by", disabled=True,
                                                      help="rule / llm (Claude) / user / default"),
            },
        )
        rows = df_to_txns(edited, res, acct)
        s.account_type = acct
        ok, msg = reconcile(s, rows)
        if ok is True:
            st.success(f"✅ {msg}")
        elif ok is False:
            st.warning(f"⚠️ {msg}")
        else:
            st.info(f"ℹ️ {msg}")
        out_total = sum(t.amount_pence for t in rows if t.direction == "debit")
        in_total = sum(t.amount_pence for t in rows if t.direction == "credit")
        st.caption(f"Money out {fmt_gbp(out_total)} · Money in {fmt_gbp(in_total)}")

        save_col, discard_col = btns.columns(2)
        if save_col.button("Save", type="primary", key=f"save-{h}"):
            detect_transfers(rows)
            assign_hashes(rows)
            res.transactions = rows
            result = importer.save(conn, res)
            del pending[h]
            flash = st.session_state.setdefault("flash", [])
            if result["duplicate_file"]:
                flash.append(("warning", f"{s.file_name} was already imported."))
            else:
                flash.append(("success", f"✅ Saved {result['inserted']} transactions from {s.file_name}"
                              + (f" ({result['skipped']} duplicates skipped)" if result["skipped"] else "")))
                all_rows = txns()
                budgets = db.get_budgets(conn)
                for m in sorted({t.month for t in rows}):
                    for a in budget_alerts(all_rows, budgets, m):
                        flash.append(("error" if a.status == "over" else "warning",
                                      f"{'🚨' if a.status == 'over' else '⚠️'} {month_label(m)}: **{a.category}** "
                                      f"{fmt_gbp(a.spent_pence)} of {fmt_gbp(a.limit_pence)} budget "
                                      f"({a.pct_used:.0f}%)"))
                if not budgets:
                    st.session_state["ask_budgets"] = True
            st.rerun()
        if discard_col.button("Discard", key=f"discard-{h}"):
            del pending[h]
            st.rerun()
