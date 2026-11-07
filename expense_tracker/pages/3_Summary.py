import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make ui_common importable

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui_common import (OTHER_GREY, SERIES, STATUS, STATUS_ICON, gbp_column, get_conn, latest_month, month_label,
                       page_setup, sidebar_alerts, txns)

page_setup("Summary", "📊")

from expense_tracker.core import db, importer  # noqa: E402
from expense_tracker.core.analytics import budget_status, monthly_category_totals, monthly_savings  # noqa: E402
from expense_tracker.core.models import fmt_gbp  # noqa: E402
from expense_tracker.core.recurring import detect_recurring, recurring_alerts  # noqa: E402

sidebar_alerts()
conn = get_conn()
st.title("📊 Monthly summary")

rows = txns()
if not rows:
    st.info("No transactions yet - upload a statement first.")
    st.page_link("pages/1_Upload.py", label="Upload statements", icon="📄")
    st.stop()

totals = monthly_category_totals(rows)
savings = {s.month: s for s in monthly_savings(rows)}
months = sorted(savings)
month = st.selectbox("Month", months[::-1], format_func=month_label)

# ------------------------------------------------------------------ KPI tiles
cur = savings[month]
prev = savings.get(months[months.index(month) - 1]) if months.index(month) > 0 else None


def delta(a, b):
    return None if b is None else fmt_gbp(a - b)


k1, k2, k3, k4 = st.columns(4)
k1.metric("Income", fmt_gbp(cur.income_pence), delta(cur.income_pence, prev and prev.income_pence))
k2.metric("Spending", fmt_gbp(cur.spending_pence), delta(cur.spending_pence, prev and prev.spending_pence),
          delta_color="inverse")
k3.metric("Savings", fmt_gbp(cur.savings_pence), delta(cur.savings_pence, prev and prev.savings_pence))
k4.metric("Savings rate", "—" if cur.savings_rate is None else f"{cur.savings_rate:.0%}")

# ------------------------------------------------------------- budget status
budgets = db.get_budgets(conn)
status = budget_status(rows, budgets, month)
flagged = [b for b in status if b.status != "ok"]
if not budgets:
    st.info("No budgets set - set them to get overspend alerts.")
    st.page_link("pages/4_Budgets.py", label="Set budgets", icon="🎯")
for b in flagged:
    text = (f"{STATUS_ICON[b.status]} **{b.category}**: spent {fmt_gbp(b.spent_pence)} of "
            f"{fmt_gbp(b.limit_pence)} ({b.pct_used:.0f}%)")
    if b.status == "over":
        st.error(text + f" - over by {fmt_gbp(-b.remaining_pence)}")
    else:
        st.warning(text + f" - {fmt_gbp(b.remaining_pence)} left")

# --------------------------------------------------------- category this month
st.subheader(f"Spending by category · {month_label(month)}")
this = {c: v for c, v in totals.get(month, {}).items() if v != 0}
if this:
    cat_df = pd.DataFrame(sorted(this.items(), key=lambda kv: -kv[1]), columns=["Category", "pence"])
    cat_df["Spent"] = cat_df["pence"] / 100
    cat_df["Budget"] = cat_df["Category"].map(lambda c: budgets[c].monthly_limit_pence / 100 if c in budgets else None)
    cat_df["Share"] = cat_df["pence"] / max(1, cat_df["pence"].clip(lower=0).sum())
    cat_df["Status"] = cat_df["Category"].map(
        {b.category: f"{STATUS_ICON[b.status]} {b.status}" for b in status}).fillna("no budget")
    left, right = st.columns([3, 2])
    with left:
        fig = go.Figure(go.Bar(
            x=cat_df["Spent"], y=cat_df["Category"], orientation="h", marker_color=SERIES[0],
            marker_line_width=0, hovertemplate="%{y}: £%{x:,.2f}<extra></extra>",
            text=[f"£{v:,.0f}" for v in cat_df["Spent"]], textposition="outside", cliponaxis=False,
        ))
        budget_pts = cat_df.dropna(subset=["Budget"])
        if not budget_pts.empty:
            fig.add_trace(go.Scatter(
                x=budget_pts["Budget"], y=budget_pts["Category"], mode="markers", name="Budget",
                marker=dict(symbol="line-ns", size=18, line=dict(width=2, color="#52514e")),
                hovertemplate="%{y} budget: £%{x:,.2f}<extra></extra>",
            ))
        fig.update_layout(height=max(260, 34 * len(cat_df)), margin=dict(l=0, r=40, t=10, b=10),
                          yaxis=dict(autorange="reversed"), xaxis=dict(tickprefix="£", gridcolor="#e1e0d9"),
                          showlegend=False, bargap=0.35, paper_bgcolor="rgba(0,0,0,0)",
                          plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, width="stretch")
        if not budget_pts.empty:
            st.caption("Vertical tick = budget for that category.")
    with right:
        st.dataframe(cat_df[["Category", "Spent", "Budget", "Share", "Status"]], hide_index=True,
                     width="stretch",
                     column_config={"Spent": gbp_column("Spent"), "Budget": gbp_column("Budget"),
                                    "Share": st.column_config.ProgressColumn("Share", format="percent",
                                                                             min_value=0, max_value=1)})
else:
    st.caption("No spending this month.")

# ------------------------------------------------------- month x category table
st.subheader("Every month by category")
pivot = pd.DataFrame(totals).fillna(0).div(100)
pivot = pivot.reindex(columns=months, fill_value=0)
pivot = pivot.loc[pivot.abs().sum(axis=1) > 0]
pivot["Average"] = pivot.mean(axis=1)
pivot = pivot.sort_values("Average", ascending=False)
pivot.loc["Total spending"] = pivot.sum()
pivot.loc["Income"] = [savings[m].income_pence / 100 for m in months] + [
    sum(savings[m].income_pence for m in months) / 100 / len(months)]
pivot.loc["Savings"] = pivot.loc["Income"] - pivot.loc["Total spending"]
pivot.columns = [month_label(c) if c != "Average" else c for c in pivot.columns]
st.dataframe(pivot.style.format("£{:,.2f}"), width="stretch")
st.download_button("Download summary CSV", pivot.to_csv().encode(), "monthly_summary.csv", "text/csv")

# ------------------------------------------------- stacked monthly spend chart
st.subheader("Spending trend")
spend_cats = pivot.drop(index=["Total spending", "Income", "Savings"])
top = list(spend_cats.index[:7])  # fixed order by overall average; colour follows category
fig = go.Figure()
for i, cat in enumerate(top):
    vals = [totals.get(m, {}).get(cat, 0) / 100 for m in months]
    fig.add_trace(go.Bar(name=cat, x=[month_label(m) for m in months], y=vals, marker_color=SERIES[i],
                         marker_line=dict(width=1, color="rgba(255,255,255,0.9)"),
                         hovertemplate=f"{cat}<br>%{{x}}: £%{{y:,.2f}}<extra></extra>"))
rest = [c for c in spend_cats.index if c not in top]
if rest:
    vals = [sum(totals.get(m, {}).get(c, 0) for c in rest) / 100 for m in months]
    fig.add_trace(go.Bar(name="Everything else", x=[month_label(m) for m in months], y=vals,
                         marker_color=OTHER_GREY, marker_line=dict(width=1, color="rgba(255,255,255,0.9)"),
                         hovertemplate="Everything else<br>%{x}: £%{y:,.2f}<extra></extra>"))
fig.update_layout(barmode="relative", height=380, margin=dict(l=0, r=0, t=10, b=0), bargap=0.4,
                  yaxis=dict(tickprefix="£", gridcolor="#e1e0d9"), legend=dict(orientation="h", y=-0.15),
                  paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", hovermode="closest")
st.plotly_chart(fig, width="stretch")
if rest:
    st.caption(f"'Everything else' = {', '.join(rest)}.")

# --------------------------------------------------------------- savings chart
st.subheader("Income, spending and savings")
labels = [month_label(m) for m in months]
fig = go.Figure()
fig.add_trace(go.Bar(name="Income", x=labels, y=[savings[m].income_pence / 100 for m in months],
                     marker_color=SERIES[0], hovertemplate="Income %{x}: £%{y:,.2f}<extra></extra>"))
fig.add_trace(go.Bar(name="Spending", x=labels, y=[savings[m].spending_pence / 100 for m in months],
                     marker_color=SERIES[1], hovertemplate="Spending %{x}: £%{y:,.2f}<extra></extra>"))
fig.add_trace(go.Scatter(name="Savings", x=labels, y=[savings[m].savings_pence / 100 for m in months],
                         mode="lines+markers", line=dict(width=2, color=SERIES[2]), marker=dict(size=9),
                         hovertemplate="Savings %{x}: £%{y:,.2f}<extra></extra>"))
fig.update_layout(barmode="group", height=340, margin=dict(l=0, r=0, t=10, b=0), bargap=0.35, bargroupgap=0.08,
                  yaxis=dict(tickprefix="£", gridcolor="#e1e0d9", zeroline=True, zerolinecolor="#c3c2b7"),
                  legend=dict(orientation="h", y=-0.15), paper_bgcolor="rgba(0,0,0,0)",
                  plot_bgcolor="rgba(0,0,0,0)", hovermode="x unified")
st.plotly_chart(fig, width="stretch")
total_saved = sum(s.savings_pence for s in savings.values())
st.caption(f"Total saved across {len(months)} month(s): **{fmt_gbp(total_saved)}** "
           f"(average {fmt_gbp(total_saved // len(months))}/month).")

# -------------------------------------------------------- recurring commitments
st.subheader("🔁 Recurring commitments")
overrides = db.get_recurring_overrides(conn)
groups = detect_recurring(rows, overrides)
active = [g for g in groups if g.status != "dismissed"]
if not groups:
    st.caption("Nothing recurring detected yet - this needs at least 2-3 months of statements.")
else:
    monthly_out = sum(g.typical_amount_pence for g in active if g.direction == "debit" and g.cadence == "monthly")
    monthly_in = sum(g.typical_amount_pence for g in active if g.direction == "credit" and g.cadence == "monthly")
    c1, c2, c3 = st.columns(3)
    c1.metric("Expected monthly income", fmt_gbp(monthly_in))
    c2.metric("Fixed monthly outgoings", fmt_gbp(monthly_out))
    c3.metric("Left after fixed costs", fmt_gbp(monthly_in - monthly_out))

    for a in recurring_alerts(groups, rows, as_of=max(t.date for t in rows)):
        (st.warning if a.level == "warning" else st.info)(a.message)

    rec_df = pd.DataFrame([{
        "Merchant": g.merchant, "Type": g.kind.capitalize(), "Category": g.category, "Cadence": g.cadence,
        "Typical amount": g.typical_amount_pence / 100, "Seen": g.occurrences, "Last": g.last_date,
        "Next expected": g.next_expected,
        "Status": g.status + (" (tentative)" if g.tentative and g.status == "detected" else ""),
    } for g in groups])
    st.dataframe(rec_df, hide_index=True, width="stretch",
                 column_config={"Typical amount": gbp_column("Typical amount")})

    with st.form("recurring-override"):
        c1, c2, c3 = st.columns([3, 2, 1])
        choice = c1.selectbox("Merchant", range(len(groups)),
                              format_func=lambda i: f"{groups[i].merchant} ({groups[i].kind})")
        action = c2.selectbox("Mark as", ["confirmed", "dismissed", "reset to auto-detect"])
        if c3.form_submit_button("Apply"):
            g = groups[choice]
            db.set_recurring_override(conn, g.merchant, g.direction,
                                      None if action.startswith("reset") else action)
            importer.refresh_derived(conn)
            st.rerun()
