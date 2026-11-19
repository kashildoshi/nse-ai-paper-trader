import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import requests
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

st.set_page_config(page_title="NSE Pro — AI Trading Terminal", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = "paper_trades.db"
INITIAL_CAPITAL = 100000.0

DEFAULT_NSE_WATCHLIST = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "SBIN", "WIPRO", "BHARTIARTL", "AXISBANK", "LT"
]

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

html, body, [class*="css"], button, input, select, textarea, table {
    font-family: 'Inter', 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, sans-serif !important;
    letter-spacing: normal !important;
}

/* Eliminate Streamlit screen dimming & running spinner flicker during live ticks */
[data-stale="true"], [data-stale="true"] * {
    opacity: 1 !important;
    transition: none !important;
}
[data-testid="stStatusWidget"] {
    visibility: hidden !important;
}
header[data-testid="stHeader"] {
    background: transparent !important;
    height: 2rem !important;
}
.block-container {
    padding-top: 2.35rem !important;
    padding-bottom: 1.5rem !important;
    max-width: 1580px;
}

/* Clean Charcoal & Slate Top Bar */
.pro-topbar {
    background: #161B22;
    border: 1px solid #2A313C;
    border-radius: 10px;
    padding: 0.7rem 1.25rem;
    margin-bottom: 0.75rem;
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 0.75rem;
}
.pro-logo { display: flex; align-items: center; gap: 0.6rem; }
.pro-wordmark { font-size: 1.25rem; font-weight: 700; color: #F0F6FC; }
.pro-badge {
    background: rgba(41, 98, 255, 0.16);
    border: 1px solid rgba(41, 98, 255, 0.4);
    color: #6EA8FE;
    font-size: 0.7rem;
    font-weight: 600;
    padding: 0.16rem 0.55rem;
    border-radius: 5px;
}
.index-strip { display: flex; gap: 0.9rem; align-items: center; flex-wrap: wrap; }
.idx-box {
    background: #0E1117;
    border: 1px solid #262C36;
    border-radius: 7px;
    padding: 0.32rem 0.75rem;
    display: flex;
    align-items: center;
    gap: 0.55rem;
}
.idx-name { font-size: 0.74rem; font-weight: 600; color: #8B949E; }
.idx-val { font-size: 0.84rem; font-weight: 600; color: #F0F6FC; font-variant-numeric: tabular-nums; }
.idx-pos { font-size: 0.76rem; color: #00B386; font-weight: 600; font-variant-numeric: tabular-nums; }
.idx-neg { font-size: 0.76rem; color: #F23645; font-weight: 600; font-variant-numeric: tabular-nums; }

/* Panels & Scrip Header */
.ux-panel {
    background: #161B22;
    border: 1px solid #262C36;
    border-radius: 10px;
    padding: 0.85rem 1rem;
    margin-bottom: 0.7rem;
}
.ux-panel-title {
    font-size: 0.76rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em !important;
    color: #8B949E;
    border-bottom: 1px solid #262C36;
    padding-bottom: 0.42rem;
    margin-bottom: 0.6rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.scrip-bar {
    background: #161B22;
    border: 1px solid #262C36;
    border-radius: 10px;
    padding: 0.75rem 1.1rem;
    margin-bottom: 0.6rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.8rem;
}
.scrip-sym { font-size: 1.25rem; font-weight: 700; color: #F0F6FC; }
.scrip-nse { background: #21262D; color: #8B949E; font-size: 0.7rem; font-weight: 600; padding: 0.16rem 0.48rem; border-radius: 4px; margin-left: 0.45rem; }
.scrip-ltp { font-size: 1.35rem; font-weight: 700; color: #F0F6FC; font-variant-numeric: tabular-nums; }
.ux-green { color: #00B386; font-weight: 600; font-variant-numeric: tabular-nums; }
.ux-red { color: #F23645; font-weight: 600; font-variant-numeric: tabular-nums; }

.ux-kv {
    display: flex;
    justify-content: space-between;
    padding: 0.36rem 0;
    border-bottom: 1px solid #21262D;
    font-size: 0.84rem;
}
.ux-k { color: #8B949E; }
.ux-v { color: #E6EDF3; font-weight: 600; font-variant-numeric: tabular-nums; }

.tag-blue {
    display: inline-block;
    background: rgba(41, 98, 255, 0.15);
    border: 1px solid rgba(41, 98, 255, 0.38);
    color: #6EA8FE;
    font-size: 0.72rem;
    font-weight: 600;
    padding: 0.16rem 0.5rem;
    border-radius: 4px;
    margin: 0 0.3rem 0.35rem 0;
}
.tag-mint {
    display: inline-block;
    background: rgba(0, 179, 134, 0.14);
    border: 1px solid rgba(0, 179, 134, 0.38);
    color: #00B386;
    font-size: 0.72rem;
    font-weight: 600;
    padding: 0.16rem 0.5rem;
    border-radius: 4px;
    margin: 0 0.3rem 0.35rem 0;
}

.dock-summary {
    background: #161B22;
    border: 1px solid #262C36;
    border-radius: 10px;
    padding: 0.8rem 1.2rem;
    margin-bottom: 0.75rem;
    display: flex;
    justify-content: space-around;
    flex-wrap: wrap;
    gap: 1rem;
}
.dock-metric { text-align: center; }
.dock-lbl { font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.04em !important; color: #8B949E; font-weight: 600; }
.dock-val { font-size: 1.22rem; font-weight: 700; margin-top: 0.18rem; font-variant-numeric: tabular-nums; }

div.stButton > button[kind="secondary"] {
    background-color: #161B22 !important;
    border: 1px solid #262C36 !important;
    color: #E6EDF3 !important;
    font-weight: 500 !important;
    font-size: 0.84rem !important;
}
div.stButton > button[kind="secondary"]:hover {
    background-color: #21262D !important;
    border-color: #3B4452 !important;
}
div.stButton > button[kind="primary"] {
    background-color: #2962FF !important;
    border: 1px solid #3B71FE !important;
    color: #FFFFFF !important;
    font-weight: 600 !important;
    font-size: 0.84rem !important;
}
div.stButton > button[kind="primary"]:hover {
    background-color: #1E53E5 !important;
}
</style>
""", unsafe_allow_html=True)

PATTERN_DESCRIPTIONS = {
    "orb": "15m Opening Range Breakout (ORB): Price broke decisively above the 09:15–09:30 IST range high with institutional volume surge.",
    "vwap_reclaim": "VWAP Reclaim: Price absorbed morning dip below VWAP and reclaimed institutional average price with rising volume.",
    "bull_flag": "Bull Flag Continuation: Strong impulsive upward leg followed by tight consolidation and upper-trendline breakout.",
    "ema_crossover": "9/21 EMA Crossover: Fast 9 EMA crossed above 21 EMA with positive RSI momentum."
}

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, pattern TEXT, qty INTEGER,
            entry_price REAL, exit_price REAL, high_water REAL,
            final_stop REAL, target_price REAL, exit_reason TEXT,
            gross_pnl REAL, charges REAL, net_pnl REAL,
            peak_capture_pct REAL, status TEXT DEFAULT 'CLOSED',
            entry_time TEXT, exit_time TEXT,
            entry_reason TEXT, tech_params TEXT, news_catalyst TEXT, timemachine_prob REAL
        )
    """)
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
    for col_name, col_type in [("entry_reason", "TEXT"), ("tech_params", "TEXT"), ("news_catalyst", "TEXT"), ("timemachine_prob", "REAL"), ("target_price", "REAL"), ("side", "TEXT")]:
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
    conn.commit()
    return conn

def load_trades() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM trades", conn)
    conn.close()
    if df.empty:
        return df
    for col in ["net_pnl", "gross_pnl", "charges", "entry_price", "exit_price", "high_water", "final_stop", "target_price", "qty", "timemachine_prob"]:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    for col, default_val in [("symbol", "NSE"), ("pattern", "orb"), ("entry_reason", ""), ("tech_params", ""), ("news_catalyst", ""), ("side", "LONG")]:
        if col not in df.columns:
            df[col] = default_val
        df[col] = df[col].fillna(default_val).astype(str)
    if "status" not in df.columns:
        df["status"] = df["exit_reason"].apply(lambda x: "OPEN" if pd.isna(x) or str(x).strip() == "" else "CLOSED") if "exit_reason" in df.columns else "CLOSED"
    return df

def _fetch_single_quote(ticker: str):
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=5m&range=1d", headers={"User-Agent": "Mozilla/5.0"}, timeout=3)
        meta = r.json()["chart"]["result"][0]["meta"]
        ltp = float(meta.get("regularMarketPrice", 0))
        prev = float(meta.get("previousClose", ltp))
        chg = ltp - prev
        pct = (chg / prev * 100.0) if prev > 0 else 0.0
        return {"ltp": ltp, "chg": chg, "pct": pct}
    except Exception:
        return {"ltp": 0.0, "chg": 0.0, "pct": 0.0}

@st.cache_data(ttl=10)
def fetch_all_market_quotes(symbols: tuple):
    tasks = {"NIFTY 50": "%5ENSEI", "NIFTY BANK": "%5ENSEBANK"}
    for s in symbols:
        clean = s.replace(".NS", "").strip().upper()
        tasks[clean] = f"{clean}.NS"

    results = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        future_map = {ex.submit(_fetch_single_quote, tkr): name for name, tkr in tasks.items()}
        for fut, name in future_map.items():
            results[name] = fut.result()
    return results

@st.cache_data(ttl=6)
def fetch_tv_candles(symbol: str, interval: str = "5m"):
    clean_sym = str(symbol).replace(".NS", "").strip().upper()
    range_map = {"1m": "1d", "5m": "5d", "15m": "5d", "1d": "3mo"}
    rng = range_map.get(interval, "5d")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{clean_sym}.NS?interval={interval}&range={rng}"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=4)
        res = r.json()["chart"]["result"][0]
        meta = res["meta"]
        ts = res.get("timestamp", [])
        q = res["indicators"]["quote"][0]
        df = pd.DataFrame({
            "time": [datetime.fromtimestamp(t, tz=IST) for t in ts],
            "open": q.get("open", []),
            "high": q.get("high", []),
            "low": q.get("low", []),
            "close": q.get("close", []),
            "volume": q.get("volume", [])
        }).dropna()

        df = df[(df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)].reset_index(drop=True)
        if df.empty:
            return {"ok": False}

        df["volume"] = df["volume"].fillna(0)
        df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()

        df["date_str"] = df["time"].dt.strftime("%Y-%m-%d")
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        df["pv"] = tp * df["volume"]
        cum_pv = df.groupby("date_str")["pv"].cumsum()
        cum_vol = df.groupby("date_str")["volume"].cumsum()
        df["vwap"] = (cum_pv / cum_vol.where(cum_vol > 0)).fillna(tp)

        df["bb_mid"] = df["close"].rolling(20, min_periods=1).mean()
        bb_std = df["close"].rolling(20, min_periods=1).std().fillna(0)
        df["bb_upper"] = df["bb_mid"] + 2.0 * bb_std
        df["bb_lower"] = df["bb_mid"] - 2.0 * bb_std

        delta = df["close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, min_periods=1, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, min_periods=1, adjust=False).mean().replace(0, 0.0001)
        df["rsi"] = 100.0 - (100.0 / (1.0 + (gain / loss)))

        ema12 = df["close"].ewm(span=12, adjust=False).mean()
        ema26 = df["close"].ewm(span=26, adjust=False).mean()
        df["macd"] = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]

        df = df.tail(75).reset_index(drop=True)
        df["time_label"] = df["time"].dt.strftime("%d %b %H:%M") if interval != "1d" else df["time"].dt.strftime("%d %b %Y")

        ltp = float(meta.get("regularMarketPrice", df["close"].iloc[-1]))
        prev_close = float(meta.get("previousClose", meta.get("chartPreviousClose", ltp)))
        day_open = float(df["open"].iloc[-1])
        day_high = float(meta.get("regularMarketDayHigh", df["high"].max()))
        day_low = float(meta.get("regularMarketDayLow", df["low"].min()))
        day_vol = int(meta.get("regularMarketVolume", df["volume"].tail(20).sum()))

        return {
            "ok": True, "df": df, "ltp": ltp, "prev_close": prev_close,
            "day_open": day_open, "day_high": day_high, "day_low": day_low, "volume": day_vol
        }
    except Exception:
        return {"ok": False}

@st.cache_data(ttl=180)
def fetch_stock_news(symbol: str):
    clean_sym = str(symbol).replace(".NS", "").strip().upper()
    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={clean_sym}.NS&newsCount=4"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=3)
        items = r.json().get("news", [])
        return [{"title": it.get("title", ""), "publisher": it.get("publisher", "NSE Feed"), "link": it.get("link", "")} for it in items[:3] if it.get("title")]
    except Exception:
        return []

def seed_demo_trades():
    conn = get_conn()
    table_info = conn.execute("PRAGMA table_info(trades)").fetchall()
    today = datetime.now(IST).strftime("%Y-%m-%d")

    live_q = fetch_all_market_quotes(("RELIANCE", "TCS", "INFY"))
    rel_p = live_q.get("RELIANCE", {}).get("ltp") or 1363.00
    tcs_p = live_q.get("TCS", {}).get("ltp") or 2379.50
    infy_p = live_q.get("INFY", {}).get("ltp") or 1108.80

    samples = [
        {"symbol": "RELIANCE", "side": "LONG", "pattern": "orb_long", "qty": 35,
         "signal_entry_price": round(rel_p * 0.992, 2), "entry_price": round(rel_p * 0.993, 2),
         "exit_price": round(rel_p * 1.004, 2), "high_water": round(rel_p * 1.005, 2),
         "initial_stop": round(rel_p * 0.986, 2), "final_stop": round(rel_p * 0.998, 2),
         "target_price": round(rel_p * 1.004, 2), "exit_reason": "TARGET",
         "gross_pnl": 524.80, "charges": 48.20, "net_pnl": 476.60, "peak_capture_pct": 92.5, "status": "CLOSED",
         "entry_time": f"{today}T09:45:00+05:30", "exit_time": f"{today}T11:00:00+05:30", "timemachine_prob": 72.4,
         "entry_reason": "BUY->SELL Breakout above 15m ORB High with 2.1x volume surge and Nifty Energy leadership.",
         "tech_params": "Side: LONG (BUY->SELL) | RSI(14): 64.2 | Price > VWAP | RVOL: 2.1x",
         "news_catalyst": "Strong O2C refining margin outlook and institutional morning accumulation."},
        {"symbol": "TCS", "side": "SHORT", "pattern": "orb_short", "qty": 15,
         "signal_entry_price": round(tcs_p * 1.006, 2), "entry_price": round(tcs_p * 1.005, 2),
         "exit_price": round(tcs_p * 0.995, 2), "high_water": round(tcs_p * 0.994, 2),
         "initial_stop": round(tcs_p * 1.012, 2), "final_stop": round(tcs_p * 1.000, 2),
         "target_price": round(tcs_p * 0.995, 2), "exit_reason": "TARGET",
         "gross_pnl": 356.00, "charges": 36.10, "net_pnl": 319.90, "peak_capture_pct": 93.0, "status": "CLOSED",
         "entry_time": f"{today}T10:15:00+05:30", "exit_time": f"{today}T12:30:00+05:30", "timemachine_prob": 69.5,
         "entry_reason": "SELL->BUY Breakdown below 15m ORB Low & VWAP rejection on heavy intraday selling volume.",
         "tech_params": "Side: SHORT (SELL->BUY) | RSI(14): 36.4 | Price < VWAP | RVOL: 1.9x",
         "news_catalyst": "Intraday IT sector profit-booking and weak ADR cues."},
        {"symbol": "INFY", "side": "SHORT", "pattern": "vwap_short", "qty": 30,
         "signal_entry_price": round(infy_p * 1.005, 2), "entry_price": round(infy_p * 1.004, 2),
         "exit_price": 0.0, "high_water": round(infy_p * 0.998, 2),
         "initial_stop": round(infy_p * 1.011, 2), "final_stop": round(infy_p * 1.002, 2),
         "target_price": round(infy_p * 0.992, 2), "exit_reason": "",
         "gross_pnl": 132.00, "charges": 21.50, "net_pnl": 110.50, "peak_capture_pct": 100.0, "status": "OPEN",
         "entry_time": f"{today}T13:15:00+05:30", "exit_time": "", "timemachine_prob": 71.8,
         "entry_reason": "SELL->BUY Short entry after repeated VWAP rejection and 9/21 EMA bearish crossover.",
         "tech_params": "Side: SHORT (SELL->BUY) | RSI(14): 38.2 | Price < VWAP | RVOL: 1.85x",
         "news_catalyst": "Bearish intraday momentum across large-cap IT counters."}
    ]
    for s in samples:
        row_data = {}
        for cid, col_name, col_type, notnull, dflt_val, pk in table_info:
            if pk == 1:
                continue
            if col_name in s:
                row_data[col_name] = s[col_name]
            elif notnull == 1 and dflt_val is None:
                c_low = col_name.lower()
                if "price" in c_low:
                    row_data[col_name] = s["entry_price"]
                elif "stop" in c_low:
                    row_data[col_name] = s["final_stop"]
                elif "time" in c_low or "date" in c_low:
                    row_data[col_name] = s["entry_time"]
                elif any(t in col_type.upper() for t in ["INT", "REAL", "NUM"]):
                    row_data[col_name] = 0
                else:
                    row_data[col_name] = "DEMO"
        keys = ", ".join(row_data.keys())
        placeholders = ", ".join(["?"] * len(row_data))
        conn.execute(f"INSERT INTO trades ({keys}) VALUES ({placeholders})", list(row_data.values()))
    conn.commit()
    conn.close()

def reset_account():
    conn = get_conn()
    conn.execute("DELETE FROM trades")
    conn.commit()
    conn.close()

if "active_nse_symbol" not in st.session_state:
    st.session_state.active_nse_symbol = "RELIANCE"

@st.fragment(run_every="10s")
def render_top_index_bar():
    now_ist = datetime.now(IST)
    market_open = (now_ist.weekday() < 5) and (9, 15) <= (now_ist.hour, now_ist.minute) <= (15, 30)
    mkt_status = f"● NSE OPEN • {now_ist.strftime('%H:%M:%S IST')}" if market_open else f"○ NSE CLOSED • {now_ist.strftime('%d %b %H:%M IST')}"

    q_all = fetch_all_market_quotes(tuple(DEFAULT_NSE_WATCHLIST))
    n50 = q_all.get("NIFTY 50", {"ltp": 0, "chg": 0, "pct": 0})
    nbk = q_all.get("NIFTY BANK", {"ltp": 0, "chg": 0, "pct": 0})
    n50_cls = "idx-pos" if n50["chg"] >= 0 else "idx-neg"
    nbk_cls = "idx-pos" if nbk["chg"] >= 0 else "idx-neg"

    st.markdown(f"""
    <div class="pro-topbar">
        <div class="pro-logo">
            <span class="pro-wordmark">NSE Alpha Terminal</span>
            <span class="pro-badge">PAPER TRADING</span>
        </div>
        <div class="index-strip">
            <div class="idx-box">
                <span class="idx-name">NIFTY 50</span>
                <span class="idx-val">{n50['ltp']:,.2f}</span>
                <span class="{n50_cls}">{n50['chg']:+.2f} ({n50['pct']:+.2f}%)</span>
            </div>
            <div class="idx-box">
                <span class="idx-name">NIFTY BANK</span>
                <span class="idx-val">{nbk['ltp']:,.2f}</span>
                <span class="{nbk_cls}">{nbk['chg']:+.2f} ({nbk['pct']:+.2f}%)</span>
            </div>
            <div class="idx-box">
                <span class="idx-name">{mkt_status}</span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

@st.fragment(run_every="6s")
def render_live_chart_and_inspector(active_sym: str, tf: str, indicators: list):
    df = load_trades()
    sym_trades = df[df["symbol"].str.upper() == active_sym] if not df.empty else pd.DataFrame()
    active_trade = sym_trades.iloc[-1] if not sym_trades.empty else None

    chart_col, insp_col = st.columns([2.55, 1.15])

    with chart_col:
        mkt = fetch_tv_candles(active_sym, tf)
        if mkt.get("ok"):
            cdf = mkt["df"]
            ltp = mkt["ltp"]
            prev_close = mkt["prev_close"]
            day_open = mkt["day_open"]
            day_high = mkt["day_high"]
            day_low = mkt["day_low"]
            day_vol = mkt["volume"]
        else:
            cdf = pd.DataFrame()
            ltp = float(active_trade["entry_price"]) if active_trade is not None else 0.0
            prev_close = ltp
            day_open = ltp
            day_high = ltp
            day_low = ltp
            day_vol = 0

        day_chg = ltp - prev_close
        day_chg_pct = (day_chg / prev_close * 100.0) if prev_close > 0 else 0.0
        chg_cls = "ux-green" if day_chg >= 0 else "ux-red"

        pos_badge = ""
        if active_trade is not None:
            t_qty = int(active_trade["qty"])
            t_ent = float(active_trade["entry_price"])
            t_stat = active_trade["status"]
            t_side = str(active_trade.get("side", "LONG")).upper(); t_pnl = ((t_ent - ltp) * t_qty if t_side == "SHORT" else (ltp - t_ent) * t_qty) if t_stat == "OPEN" else float(active_trade["net_pnl"])
            p_cls = "ux-green" if t_pnl >= 0 else "ux-red"
            pos_badge = f"<div style='text-align:right;'><div style='font-size:0.74rem; color:#8B949E;'>{t_stat} POSITION ({t_qty} QTY @ ₹{t_ent:,.2f})</div><div class='{p_cls}' style='font-size:1.05rem;'>P&L: ₹{t_pnl:+,.2f}</div></div>"

        st.markdown(f"""
        <div class="scrip-bar">
            <div>
                <div><span class="scrip-sym">{active_sym}</span><span class="scrip-nse">NSE EQ</span></div>
                <div style="margin-top:0.2rem;">
                    <span class="scrip-ltp">₹{ltp:,.2f}</span>
                    <span class="{chg_cls}" style="margin-left:0.6rem; font-size:0.9rem;">{day_chg:+.2f} ({day_chg_pct:+.2f}%)</span>
                </div>
            </div>
            {pos_badge}
        </div>
        """, unsafe_allow_html=True)

        show_vol = "Volume" in indicators
        show_rsi = "RSI (14)" in indicators
        show_macd = "MACD" in indicators
        rows = 1 + int(show_vol) + int(show_rsi) + int(show_macd)
        heights = [0.60] + ([0.133] * (rows - 1))
        heights = [h / sum(heights) for h in heights]

        fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.025, row_heights=heights)

        if not cdf.empty:
            fig.add_trace(go.Candlestick(
                x=cdf["time_label"], open=cdf["open"], high=cdf["high"],
                low=cdf["low"], close=cdf["close"], name=active_sym,
                increasing=dict(line=dict(color="#00B386", width=1.2), fillcolor="#00B386"),
                decreasing=dict(line=dict(color="#F23645", width=1.2), fillcolor="#F23645")
            ), row=1, col=1)

            if "EMA 9/21" in indicators:
                fig.add_trace(go.Scatter(x=cdf["time_label"], y=cdf["ema9"], name="EMA 9", line=dict(color="#2962FF", width=1.5)), row=1, col=1)
                fig.add_trace(go.Scatter(x=cdf["time_label"], y=cdf["ema21"], name="EMA 21", line=dict(color="#F59E0B", width=1.5)), row=1, col=1)
            if "VWAP" in indicators:
                fig.add_trace(go.Scatter(x=cdf["time_label"], y=cdf["vwap"], name="VWAP", line=dict(color="#E040FB", width=1.5, dash="dot")), row=1, col=1)
            if "Bollinger Bands" in indicators:
                fig.add_trace(go.Scatter(x=cdf["time_label"], y=cdf["bb_upper"], name="BB Upper", line=dict(color="rgba(110,168,254,0.45)", width=1)), row=1, col=1)
                fig.add_trace(go.Scatter(x=cdf["time_label"], y=cdf["bb_lower"], name="BB Lower", line=dict(color="rgba(110,168,254,0.45)", width=1), fill="tonexty", fillcolor="rgba(41,98,255,0.06)"), row=1, col=1)

            c_min = float(cdf["low"].min())
            c_max = float(cdf["high"].max())

            if active_trade is not None:
                ent_p = float(active_trade["entry_price"])
                sl_p = float(active_trade["final_stop"]) if active_trade["final_stop"] > 0 else round(ent_p * 0.992, 2)
                tgt_p = float(active_trade["target_price"]) if active_trade["target_price"] > 0 else round(ent_p * 1.015, 2)
                if ltp > 0 and abs(ent_p - ltp) / ltp < 0.15:
                    fig.add_hline(y=ent_p, line_dash="dash", line_color="#6EA8FE", line_width=1.2, annotation_text=f"BUY ₹{ent_p:,.2f}", annotation_position="top left", row=1, col=1)
                    fig.add_hline(y=tgt_p, line_dash="dot", line_color="#00B386", line_width=1.2, annotation_text=f"TGT ₹{tgt_p:,.2f}", annotation_position="top left", row=1, col=1)
                    fig.add_hline(y=sl_p, line_dash="dot", line_color="#F23645", line_width=1.2, annotation_text=f"SL ₹{sl_p:,.2f}", annotation_position="bottom left", row=1, col=1)
                    c_min = min(c_min, sl_p)
                    c_max = max(c_max, tgt_p)

            pad = max((c_max - c_min) * 0.08, c_max * 0.002)
            fig.update_yaxes(range=[c_min - pad, c_max + pad], tickprefix="₹", row=1, col=1)

            curr_row = 2
            if show_vol:
                v_cols = ["#00B386" if c >= o else "#F23645" for c, o in zip(cdf["close"], cdf["open"])]
                fig.add_trace(go.Bar(x=cdf["time_label"], y=cdf["volume"], name="Volume", marker_color=v_cols, opacity=0.75), row=curr_row, col=1)
                fig.update_yaxes(title_text="Vol", row=curr_row, col=1, gridcolor="#21262D")
                curr_row += 1
            if show_rsi:
                fig.add_trace(go.Scatter(x=cdf["time_label"], y=cdf["rsi"], name="RSI(14)", line=dict(color="#6EA8FE", width=1.6)), row=curr_row, col=1)
                fig.add_hline(y=70, line_dash="dot", line_color="rgba(242,54,69,0.5)", row=curr_row, col=1)
                fig.add_hline(y=30, line_dash="dot", line_color="rgba(0,179,134,0.5)", row=curr_row, col=1)
                fig.update_yaxes(title_text="RSI", range=[10, 90], row=curr_row, col=1, gridcolor="#21262D")
                curr_row += 1
            if show_macd:
                h_cols = ["#00B386" if h >= 0 else "#F23645" for h in cdf["macd_hist"]]
                fig.add_trace(go.Bar(x=cdf["time_label"], y=cdf["macd_hist"], name="Hist", marker_color=h_cols, opacity=0.7), row=curr_row, col=1)
                fig.add_trace(go.Scatter(x=cdf["time_label"], y=cdf["macd"], name="MACD", line=dict(color="#2962FF", width=1.4)), row=curr_row, col=1)
                fig.add_trace(go.Scatter(x=cdf["time_label"], y=cdf["macd_signal"], name="Signal", line=dict(color="#F59E0B", width=1.4)), row=curr_row, col=1)
                fig.update_yaxes(title_text="MACD", row=curr_row, col=1, gridcolor="#21262D")

        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="#161B22",
            plot_bgcolor="#161B22",
            height=550,
            uirevision=f"{active_sym}_{tf}_{'_'.join(indicators)}",
            margin=dict(l=10, r=55, t=25, b=15),
            xaxis_rangeslider_visible=False,
            hovermode="x unified",
            dragmode="pan",
            font=dict(family="Inter, Segoe UI, sans-serif", size=12, color="#E6EDF3"),
            legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0, font=dict(size=11)),
            newshape=dict(line_color="#2962FF", line_width=2)
        )
        fig.update_yaxes(side="right", gridcolor="#21262D", showspikes=True, spikemode="across", spikethickness=1, spikedash="dot", spikecolor="#6E7681")
        fig.update_xaxes(type="category", nticks=9, gridcolor="#21262D", showspikes=True, spikemode="across", spikethickness=1, spikedash="dot", spikecolor="#6E7681")

        st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True, "displaylogo": False, "modeBarButtonsToAdd": ["drawline", "drawrect", "eraseshape"]})

    with insp_col:
        st.markdown(f"""
        <div class="ux-panel">
            <div class="ux-panel-title"><span>Market Overview</span><span>NSE</span></div>
            <div class="ux-kv"><span class="ux-k">Open</span><span class="ux-v">₹{day_open:,.2f}</span></div>
            <div class="ux-kv"><span class="ux-k">High</span><span class="ux-v" style="color:#00B386;">₹{day_high:,.2f}</span></div>
            <div class="ux-kv"><span class="ux-k">Low</span><span class="ux-v" style="color:#F23645;">₹{day_low:,.2f}</span></div>
            <div class="ux-kv"><span class="ux-k">Prev. Close</span><span class="ux-v">₹{prev_close:,.2f}</span></div>
            <div class="ux-kv" style="border:none;"><span class="ux-k">Volume</span><span class="ux-v">{day_vol:,}</span></div>
        </div>
        """, unsafe_allow_html=True)

        if active_trade is not None:
            pat_key = str(active_trade["pattern"]).lower().strip()
            tm_prob = float(active_trade["timemachine_prob"]) if active_trade["timemachine_prob"] > 0 else 68.5
            pat_desc = PATTERN_DESCRIPTIONS.get(pat_key, f"{pat_key.upper()} intraday breakout setup.")
            reason_txt = active_trade["entry_reason"].strip() if active_trade["entry_reason"].strip() else pat_desc
            tech_txt = active_trade["tech_params"].strip() if active_trade["tech_params"].strip() else f"Time-Machine: {tm_prob:.1f}% | Risk Gate: PASS"
            st.markdown(f"""
            <div class="ux-panel">
                <div class="ux-panel-title"><span>AI Trade Rationale</span><span>{active_trade['status']}</span></div>
                <div>
                    <span class="tag-blue">{pat_key.upper()}</span>
                    <span class="tag-mint">TM {tm_prob:.1f}% (>65%)</span>
                </div>
                <div style="font-size:0.83rem; color:#C9D1D9; line-height:1.45; margin-top:0.35rem;">{reason_txt}</div>
                <div style="font-size:0.76rem; color:#6EA8FE; margin-top:0.45rem;">{tech_txt}</div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown(f"""
            <div class="ux-panel">
                <div class="ux-panel-title"><span>AI Scanner Status</span><span>STANDBY</span></div>
                <div style="font-size:0.83rem; color:#8B949E; line-height:1.45;">
                    No active trade on <b>{active_sym}</b> yet. Bot triggers automatically when pattern + Time-Machine (>65%) + Risk Gate align.
                </div>
            </div>
            """, unsafe_allow_html=True)

        news_list = fetch_stock_news(active_sym)
        stored_cat = active_trade["news_catalyst"].strip() if (active_trade is not None and active_trade["news_catalyst"].strip()) else ""
        n_html = f"<div style='font-size:0.81rem; color:#E6EDF3; margin-bottom:0.4rem;'><b>Catalyst:</b> {stored_cat}</div>" if stored_cat else ""
        for item in news_list[:2]:
            n_html += f"<div style='font-size:0.8rem; margin-bottom:0.35rem;'>• <a href='{item['link']}' target='_blank' style='color:#6EA8FE; text-decoration:none;'>{item['title']}</a></div>"
        if not n_html:
            n_html = "<div style='font-size:0.8rem; color:#8B949E;'>No breaking headlines; pure price-volume technical setup.</div>"

        st.markdown(f"""
        <div class="ux-panel">
            <div class="ux-panel-title"><span>NSE News & Catalysts</span><span>LIVE</span></div>
            {n_html}
        </div>
        """, unsafe_allow_html=True)

@st.fragment(run_every="8s")
def render_bottom_dock(watchlist_tuple: tuple):
    df = load_trades()
    closed = df[df["status"] == "CLOSED"].copy() if not df.empty else pd.DataFrame()
    open_df = df[df["status"] == "OPEN"].copy() if not df.empty else pd.DataFrame()
    quotes = fetch_all_market_quotes(watchlist_tuple)

    realized_pnl = float(closed["net_pnl"].sum()) if not closed.empty else 0.0
    total_charges = float(closed["charges"].sum()) if not closed.empty else 0.0
    available_funds = INITIAL_CAPITAL + realized_pnl

    dock_positions, dock_insights = st.tabs(["💼 Positions (Running & Completed)", "📊 Insights & Analytics"])

    with dock_positions:
        running_pnl = 0.0
        pos_rows = []
        if not df.empty:
            for _, r in df.iloc[::-1].iterrows():
                s = str(r["symbol"]).upper()
                q = int(r["qty"])
                ep = float(r["entry_price"])
                st_status = str(r["status"]).upper()
                cur_q = quotes.get(s, {"ltp": ep})["ltp"]
                if cur_q <= 0:
                    cur_q = float(r["high_water"]) if st_status == "OPEN" and r["high_water"] > 0 else ep

                if st_status == "OPEN":
                    t_side = str(r.get("side", "LONG")).upper(); row_pnl = (ep - cur_q) * q if t_side == "SHORT" else (cur_q - ep) * q
                    running_pnl += row_pnl
                    ltp_or_exit = cur_q
                    badge = "🟢 RUNNING"
                else:
                    row_pnl = float(r["net_pnl"])
                    ltp_or_exit = float(r["exit_price"]) if r["exit_price"] > 0 else ep
                    badge = "⚪ COMPLETED"

                pos_rows.append({
                    "Status": badge,
                    "Symbol (NSE)": s,
                    "Side": "🔴 SELL→BUY" if str(r.get("side", "LONG")).upper() == "SHORT" else "🟢 BUY→SELL", "Strategy": str(r["pattern"]).upper(),
                    "Qty": q,
                    "Buy Avg (₹)": round(ep, 2),
                    "LTP / Exit (₹)": round(ltp_or_exit, 2),
                    "Trailing SL (₹)": round(float(r["final_stop"]), 2),
                    "Target (₹)": round(float(r["target_price"]), 2),
                    "Charges (₹)": round(float(r["charges"]), 2),
                    "Net P&L (₹)": round(row_pnl, 2),
                    "Exit Reason": str(r["exit_reason"]) if r["exit_reason"] else "Active Trailing",
                    "Time (IST)": str(r["exit_time"] if r["exit_time"] else r["entry_time"])[:19]
                })

        total_net_pnl = realized_pnl + running_pnl
        tot_cls = "ux-green" if total_net_pnl >= 0 else "ux-red"
        run_cls = "ux-green" if running_pnl >= 0 else "ux-red"
        cmp_cls = "ux-green" if realized_pnl >= 0 else "ux-red"

        st.markdown(f"""
        <div class="dock-summary">
            <div class="dock-metric">
                <div class="dock-lbl">Total Net P&L (Running + Completed)</div>
                <div class="dock-val {tot_cls}">₹{total_net_pnl:+,.2f}</div>
            </div>
            <div class="dock-metric">
                <div class="dock-lbl">Running Positions ({len(open_df)})</div>
                <div class="dock-val {run_cls}">₹{running_pnl:+,.2f}</div>
            </div>
            <div class="dock-metric">
                <div class="dock-lbl">Completed Positions ({len(closed)})</div>
                <div class="dock-val {cmp_cls}">₹{realized_pnl:+,.2f}</div>
            </div>
            <div class="dock-metric">
                <div class="dock-lbl">Intraday Charges Paid</div>
                <div class="dock-val" style="color:#F0F6FC;">₹{total_charges:,.2f}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        p_filter = st.radio("Filter Positions", ["All Positions", "🟢 Running (Open)", "⚪ Completed (Closed)"], horizontal=True, label_visibility="collapsed")
        if pos_rows:
            pos_df = pd.DataFrame(pos_rows)
            if p_filter == "🟢 Running (Open)":
                pos_df = pos_df[pos_df["Status"] == "🟢 RUNNING"]
            elif p_filter == "⚪ Completed (Closed)":
                pos_df = pos_df[pos_df["Status"] == "⚪ COMPLETED"]

            def color_pnl(val):
                color = "#00B386" if val > 0 else ("#F23645" if val < 0 else "#8B949E")
                return f"color: {color}; font-weight: 600;"

            st.dataframe(pos_df.style.map(color_pnl, subset=["Net P&L (₹)"]), use_container_width=True, hide_index=True)
        else:
            st.info("No running or completed NSE positions yet. Click **▶ Run Offline Demo** above to test or wait for live market signals.")

    with dock_insights:
        wins = int((closed["net_pnl"] > 0).sum()) if not closed.empty else 0
        win_rate = (wins / len(closed) * 100.0) if not closed.empty else 0.0
        gross_win = float(closed[closed["net_pnl"] > 0]["net_pnl"].sum()) if not closed.empty else 0.0
        gross_loss = float(abs(closed[closed["net_pnl"] < 0]["net_pnl"].sum())) if not closed.empty else 0.0
        pf = (gross_win / gross_loss) if gross_loss > 0 else (gross_win if gross_win > 0 else 0.0)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Portfolio Equity", f"₹{available_funds:,.2f}", f"{(realized_pnl/INITIAL_CAPITAL)*100:+.2f}%")
        m2.metric("Strategy Win Rate", f"{win_rate:.1f}%", f"{wins} Wins / {len(closed)-wins} Losses")
        m3.metric("Profit Factor", f"{pf:.2f}", "Gross Profit / Loss")
        m4.metric("Total Statutory Charges", f"₹{total_charges:,.2f}", "STT + Exchange + GST")

        ins_c1, ins_c2 = st.columns([1.5, 1])
        with ins_c1:
            st.subheader("📈 Intraday Portfolio Equity Curve")
            if not closed.empty:
                closed_eq = closed.reset_index(drop=True)
                closed_eq["trade_num"] = closed_eq.index + 1
                closed_eq["equity"] = INITIAL_CAPITAL + closed_eq["net_pnl"].cumsum()
                eq_df = pd.concat([pd.DataFrame({"trade_num": [0], "equity": [INITIAL_CAPITAL], "symbol": ["Start"]}), closed_eq[["trade_num", "equity", "symbol"]]], ignore_index=True)
                fig_eq = go.Figure(go.Scatter(
                    x=eq_df["trade_num"], y=eq_df["equity"], mode="lines+markers",
                    line=dict(color="#2962FF", width=3, shape="spline"),
                    marker=dict(size=8, color="#00B386"),
                    fill="tozeroy", fillcolor="rgba(41, 98, 255, 0.14)",
                    text=eq_df["symbol"]
                ))
                fig_eq.update_layout(template="plotly_dark", paper_bgcolor="#161B22", plot_bgcolor="#161B22", height=310, margin=dict(l=15, r=15, t=15, b=15), yaxis=dict(tickprefix="₹"))
                st.plotly_chart(fig_eq, use_container_width=True)
            else:
                st.caption("Completed trades will automatically generate your intraday equity curve.")
        with ins_c2:
            st.subheader("🧠 Strategy Pattern Insights")
            if not closed.empty:
                pat_df = closed.groupby("pattern", as_index=False).agg(
                    Trades=("net_pnl", "count"),
                    Net_PnL=("net_pnl", "sum"),
                    Charges=("charges", "sum")
                )
                st.dataframe(pat_df, use_container_width=True, hide_index=True)
            else:
                st.caption("Pattern performance metrics will populate after completed trades.")

def main():
    render_top_index_bar()

    df = load_trades()
    closed = df[df["status"] == "CLOSED"].copy() if not df.empty else pd.DataFrame()
    open_df = df[df["status"] == "OPEN"].copy() if not df.empty else pd.DataFrame()
    realized_pnl = float(closed["net_pnl"].sum()) if not closed.empty else 0.0
    available_funds = INITIAL_CAPITAL + realized_pnl

    act1, act2, act3, act4 = st.columns([1.2, 1.3, 2.5, 1.5])
    with act1:
        if st.button("▶ Run Offline Demo", use_container_width=True, type="primary"):
            seed_demo_trades()
            st.rerun()
    with act2:
        if st.button("↺ Reset ₹1,00,000", use_container_width=True):
            reset_account()
            st.rerun()
    with act3:
        st.markdown(f"<div style='padding-top:0.38rem; font-size:0.86rem; color:#8B949E;'>Available Margin: <span style='font-weight:700; color:#F0F6FC;'>₹{available_funds:,.2f}</span>  |  AI Gate: <span style='color:#6EA8FE; font-weight:600;'>Time-Machine >65%</span></div>", unsafe_allow_html=True)
    with act4:
        custom_sym = st.text_input("Search NSE Symbol", placeholder="Add NSE symbol (e.g. ITC)...", label_visibility="collapsed")

    traded_syms = df["symbol"].str.upper().unique().tolist() if not df.empty else []
    watchlist_syms = list(dict.fromkeys(traded_syms + DEFAULT_NSE_WATCHLIST))
    if custom_sym and custom_sym.strip():
        cs = custom_sym.strip().upper().replace(".NS", "")
        if cs not in watchlist_syms:
            watchlist_syms.insert(0, cs)
        st.session_state.active_nse_symbol = cs

    wl_tuple = tuple(watchlist_syms[:10])
    quotes = fetch_all_market_quotes(wl_tuple)

    wl_col, right_col = st.columns([1.05, 3.7])

    with wl_col:
        st.markdown('<div class="ux-panel"><div class="ux-panel-title"><span>NSE Watchlist</span><span>All NSE Scan Active</span></div>', unsafe_allow_html=True)
        open_syms = set(open_df["symbol"].str.upper().tolist()) if not open_df.empty else set()
        for sym in wl_tuple:
            q = quotes.get(sym, {"ltp": 0.0, "pct": 0.0})
            tag = " 🟢" if sym in open_syms else ""
            pct_str = f"{q['pct']:+.2f}%" if q["ltp"] > 0 else "NSE"
            btn_label = f"{sym}{tag}  •  ₹{q['ltp']:,.1f} ({pct_str})" if q["ltp"] > 0 else f"{sym}{tag} • NSE"
            is_sel = (st.session_state.active_nse_symbol == sym)
            if st.button(btn_label, key=f"wl_{sym}", use_container_width=True, type="primary" if is_sel else "secondary"):
                st.session_state.active_nse_symbol = sym
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    with right_col:
        c_bar1, c_bar2 = st.columns([1, 3])
        with c_bar1:
            tf = st.selectbox("Timeframe", ["5m", "15m", "1m", "1d"], index=0, label_visibility="collapsed")
        with c_bar2:
            indicators = st.multiselect(
                "Studies",
                ["EMA 9/21", "VWAP", "Bollinger Bands", "Volume", "RSI (14)", "MACD"],
                default=["EMA 9/21", "VWAP", "Volume", "RSI (14)"],
                label_visibility="collapsed"
            )
        render_live_chart_and_inspector(st.session_state.active_nse_symbol, tf, indicators)

    st.markdown("---")
    render_bottom_dock(wl_tuple)

main()