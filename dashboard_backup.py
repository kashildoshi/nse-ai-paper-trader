#!/usr/bin/env python3
"""Live Auto-Refreshing Streamlit Dashboard for NSE Autonomous Paper Trading.

Reads paper_trades.db (SQLite WAL mode) every 10 seconds via @st.fragment(run_every="10s").
Displays Equity, Net PnL (after Indian intraday charges/STT/GST), Win Rate %,
Profit Factor, Peak Capture %, Live Open Positions, Equity Curve, and Pattern PnL charts.

    py -m streamlit run dashboard.py
"""

import os
import sys

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "shared"))
import nse_common as N
from paper_broker import DEFAULT_DB, INITIAL_CAPITAL, PaperBroker


def load_dashboard_data(db_path=DEFAULT_DB):
    """Fetch structured metrics and DataFrames from paper_trades.db (WAL mode)."""
    broker = PaperBroker(db_path=db_path, initial_capital=INITIAL_CAPITAL)
    m = broker.get_metrics()

    open_df = pd.DataFrame(m["open_positions"])
    if not open_df.empty:
        open_df["unrealized_pnl"] = (open_df["last_price"] - open_df["entry_price"]) * open_df["qty"]
        open_df["trail_status"] = open_df["trail_active"].map(lambda x: "ACTIVE" if x else "ARMED @ +0.5%")

    trades_df = pd.DataFrame(m["closed_trades"])
    snaps_df = pd.DataFrame(m["equity_snapshots"])

    pat_rows = list((m.get("pattern_stats") or {}).values())
    pat_df = pd.DataFrame(pat_rows)
    return {
        "metrics": m,
        "open_df": open_df,
        "trades_df": trades_df,
        "snaps_df": snaps_df,
        "pat_df": pat_df,
    }


def _render_charts(snaps_df, pat_df):
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Equity Curve (INR)")
        if snaps_df.empty:
            st.info("No equity snapshots recorded yet.")
        else:
            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=snaps_df["timestamp"],
                    y=snaps_df["equity"],
                    mode="lines+markers",
                    name="Equity",
                    fill="tozeroy",
                    line=dict(color="#16a34a", width=2.5),
                )
            )
            fig.add_hline(y=INITIAL_CAPITAL, line_dash="dash", line_color="#64748b", annotation_text="Initial ₹1,00,000")
            fig.update_layout(
                height=340,
                margin=dict(l=10, r=10, t=20, b=10),
                yaxis_title="Equity (₹)",
                xaxis_title="Time (IST)",
            )
            st.plotly_chart(fig, use_container_width=True)

    with col_right:
        st.subheader("Pattern Net PnL & Peak Capture")
        if pat_df.empty:
            st.info("No closed trades yet for pattern breakdown.")
        else:
            pat_df_sorted = pat_df.sort_values("net_pnl", ascending=False)
            colors = ["#16a34a" if v >= 0 else "#dc2626" for v in pat_df_sorted["net_pnl"]]
            fig_pat = go.Figure(
                data=[
                    go.Bar(
                        x=pat_df_sorted["pattern"],
                        y=pat_df_sorted["net_pnl"],
                        marker_color=colors,
                        text=[N.inr(v, symbol="₹") for v in pat_df_sorted["net_pnl"]],
                        textposition="auto",
                        name="Net PnL (₹)",
                    )
                ]
            )
            fig_pat.update_layout(
                height=340,
                margin=dict(l=10, r=10, t=20, b=10),
                yaxis_title="Net PnL (₹)",
                xaxis_title="Pattern",
            )
            st.plotly_chart(fig_pat, use_container_width=True)


def render_live_fragment(db_path=DEFAULT_DB):
    @st.fragment(run_every="10s")
    def _live_panel():
        data = load_dashboard_data(db_path=db_path)
        m = data["metrics"]
        now = N.now_ist()

        st.caption(
            "Last refreshed: %s | NSE %s | SQLite WAL (%s)"
            % (now.strftime("%Y-%m-%d %H:%M:%S IST"), N.market_state(now).upper(), os.path.basename(db_path))
        )

        # KPI Cards
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Equity", N.inr(m["equity"], symbol="₹"), delta="%+.2f%%" % m["return_pct"])
        k2.metric("Net PnL (after Taxes/STT)", N.inr(m["net_pnl"], symbol="₹"),
                  delta="Charges: -%s" % N.inr(m["total_charges"], symbol="₹"), delta_color="off")
        k3.metric("Win Rate", "%.1f%%" % m["win_rate_pct"], delta="%dW / %dL" % (m["wins"], m["losses"]), delta_color="off")
        k4.metric("Profit Factor", "%.2f" % m["profit_factor"])
        k5.metric("Peak Capture", "%.1f%%" % m["avg_peak_capture_pct"])

        st.divider()
        _render_charts(data["snaps_df"], data["pat_df"])

        st.divider()
        st.subheader("Live Open Positions (%d)" % len(m["open_positions"]))
        if data["open_df"].empty:
            st.info("No open intraday positions currently.")
        else:
            cols = [
                "symbol", "pattern", "qty", "entry_price", "last_price", "high_water",
                "current_stop", "trail_status", "target_price", "unrealized_pnl", "win_prob_pct", "entry_time"
            ]
            view_open = data["open_df"][[c for c in cols if c in data["open_df"].columns]].copy()
            st.dataframe(view_open, use_container_width=True, hide_index=True)

        st.subheader("Closed Trades & Intraday Charges Ledger (%d)" % len(m["closed_trades"]))
        if data["trades_df"].empty:
            st.info("No closed trades logged yet. Run `py nse_autonomous_runner.py --demo-session` to populate sample trades.")
        else:
            tcols = [
                "symbol", "pattern", "qty", "entry_price", "exit_price", "high_water",
                "final_stop", "exit_reason", "gross_pnl", "charges", "net_pnl", "peak_capture_pct", "exit_time"
            ]
            view_closed = data["trades_df"][[c for c in tcols if c in data["trades_df"].columns]].copy()
            st.dataframe(view_closed.iloc[::-1], use_container_width=True, hide_index=True)

        if not data["pat_df"].empty:
            st.subheader("Pattern Performance Summary")
            st.dataframe(data["pat_df"], use_container_width=True, hide_index=True)

    _live_panel()


def main():
    st.set_page_config(page_title="NSE Autonomous Paper Trader", layout="wide")
    st.title("NSE Intraday Autonomous Paper Trading & Trailing Stop Insights")

    with st.sidebar:
        st.header("System Rules")
        st.markdown(
            "- **Capital:** ₹1,00,000.00 (Paper SQLite WAL)\n"
            "- **Slippage:** 0.03% per side\n"
            "- **Charges:** Brokerage + 0.025% Sell STT + NSE Txn + SEBI + 0.003% Buy Stamp + 18% GST\n"
            "- **Trailing SL:** Arms at **+0.5%**, trails **0.6%** below `high_water` (ratchets up only)\n"
            "- **Safety Cutoff:** **15:20 IST** square-off\n"
            "- **Entry Gate:** `trader-risk` PASS & `trader-timemachine` > 55%"
        )
        if st.button("Run Offline Demo Session"):
            from nse_autonomous_runner import run_demo_session
            run_demo_session(db_path=DEFAULT_DB)
            st.success("Demo session completed and saved to paper_trades.db.")
        if st.button("Reset Paper Account (₹1,00,000)"):
            PaperBroker(db_path=DEFAULT_DB).reset(INITIAL_CAPITAL)
            st.warning("Paper account reset to ₹1,00,000.00.")

    render_live_fragment(db_path=DEFAULT_DB)


if __name__ == "__main__":
    main()
