"""Optional Streamlit dashboard: open positions, equity and recent candidates.

    pip install streamlit
    streamlit run app/reporting/dashboard.py -- --db data/trading.db

Read-only: it never writes to the database or talks to the broker.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(ROOT / "data" / "trading.db"))
    a, _ = p.parse_known_args()
    return a


def load(db: str, strategy: str = "main"):
    """One book's data (main by default; a paper sleeve by name), plus the system state."""
    import pandas as pd

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        q = lambda sql: pd.read_sql_query(sql, conn, params=(strategy,))  # noqa: E731
        return {
            "open": q("SELECT symbol, side, short_k, long_k, expiry, lots, entry_credit, max_loss, margin, mfe, mae, "
                      "entry_ts FROM trades WHERE status='open' AND strategy=? ORDER BY entry_ts"),
            "closed": q("SELECT symbol, side, short_k, long_k, entry_ts, exit_ts, exit_reason, pnl_net, replayed, "
                        "gap_flag FROM trades WHERE status='closed' AND strategy=? ORDER BY exit_ts DESC LIMIT 50"),
            "equity": q("SELECT date, equity, drawdown FROM equity WHERE strategy=? ORDER BY date"),
            "candidates": q("SELECT ts, symbol, side, short_k, long_k, expiry, credit_nat, width, max_loss, p_model, "
                            "ev_ratio, accepted, reject_reason FROM candidates WHERE strategy=? ORDER BY id DESC LIMIT 300"),
            "state": pd.read_sql_query("SELECT key, value, updated_at FROM system_state", conn),
            "strategies": pd.read_sql_query("SELECT DISTINCT strategy FROM equity ORDER BY strategy = 'main' DESC, "
                                            "strategy", conn),
        }
    finally:
        conn.close()


def main():  # pragma: no cover - rendered by streamlit
    import streamlit as st

    db = _args().db
    st.set_page_config(page_title="Credit spreads", layout="wide")
    st.title("Credit spread system")
    if not Path(db).exists():
        st.error(f"Database not found: {db}")
        return
    names = list(load(db)["strategies"].strategy) or ["main"]
    book = st.sidebar.selectbox("Book", names) if len(names) > 1 else "main"
    d = load(db, book)
    if not d["state"].empty:
        st.warning("System state: " + "; ".join(f"{r.key}={r.value}" for r in d["state"].itertuples()))
    eq = d["equity"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Equity", f"{eq['equity'].iloc[-1]:,.0f}" if not eq.empty else "-")
    c2.metric("Max drawdown", f"{eq['drawdown'].max():.1%}" if not eq.empty else "-")
    c3.metric("Open spreads", len(d["open"]))
    if not eq.empty:
        st.subheader("Equity")
        st.line_chart(eq.set_index("date")["equity"])
    st.subheader("Open positions")
    st.dataframe(d["open"], use_container_width=True, hide_index=True)
    st.subheader("Recent closed trades")
    st.dataframe(d["closed"], use_container_width=True, hide_index=True)
    st.subheader("Latest candidates")
    only = st.checkbox("Accepted only", value=False)
    cands = d["candidates"]
    st.dataframe(cands[cands["accepted"] == 1] if only else cands, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
