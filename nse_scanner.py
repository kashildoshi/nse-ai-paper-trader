import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import pandas as pd
import requests

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = "paper_trades.db"

FALLBACK_NSE_UNIVERSE = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "BHARTIARTL", "SBIN", "INFY", "LICI", "ITC", "HINDUNILVR",
    "LT", "BAJFINANCE", "HCLTECH", "MARUTI", "SUNPHARMA", "ADANIENT", "KOTAKBANK", "TITAN", "ONGC", "TATAMOTORS",
    "NTPC", "AXISBANK", "ADANIPORTS", "DMART", "ULTRACEMCO", "ASIANPAINT", "COALINDIA", "BAJAJFINSV", "POWERGRID", "NESTLEIND",
    "WIPRO", "M&M", "IOC", "JSWSTEEL", "TATASTEEL", "DLF", "JIOFIN", "HAL", "BEL", "VBL",
    "IRFC", "TRENT", "ZOMATO", "SIEMENS", "PIDILITIND", "GRASIM", "SBILIFE", "HDFCLIFE", "TECHM", "HINDALCO",
    "INDIGO", "ADANIPOWER", "PFC", "RECLTD", "BANKBARODA", "GAIL", "CHOLAFIN", "BPCL", "TATAPOWER", "CIPLA",
    "DIVISLAB", "DRREDDY", "EICHERMOT", "HEROMOTOCO", "APOLLOHOSP", "BRITANNIA", "BAJAJ-AUTO", "SHRIRAMFIN", "INDUSINDBK", "MANKIND",
    "TVSMOTOR", "HAVELLS", "ABB", "BHEL", "CANBK", "PNB", "UNIONBANK", "IDFCFIRSTB", "YESBANK", "IDEA",
    "SUZLON", "NHPC", "SJVN", "IRCTC", "RVNL", "MAZDOCK", "COCHINSHIP", "BDL", "POLYCAB", "PERSISTENT",
    "COFORGE", "LTIM", "MPHASIS", "KPITTECH", "TATAELXSI", "DIXON", "TRENT", "AUROPHARMA", "LUPIN", "BIOCON"
]

def fetch_all_nse_symbols(max_symbols: int = 500) -> list[str]:
    """Fetches official NSE EQ series symbols from NSE archives with fallback."""
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "text/csv,*/*;q=0.8",
        "Referer": "https://www.nseindia.com/"
    }
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200 and "SYMBOL" in r.text:
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [c.strip().upper() for c in df.columns]
            if "SERIES" in df.columns:
                df = df[df["SERIES"].astype(str).str.strip() == "EQ"]
            syms = [str(s).strip().upper() for s in df["SYMBOL"].dropna().tolist() if str(s).strip().isalnum()]
            if len(syms) >= 50:
                # Prioritize liquid large/midcaps first, followed by full NSE EQ list
                merged = list(dict.fromkeys(FALLBACK_NSE_UNIVERSE + syms))
                return merged[:max_symbols]
    except Exception:
        pass
    return FALLBACK_NSE_UNIVERSE

def evaluate_nse_symbol(symbol: str, min_prob: float = 65.0) -> dict | None:
    """Evaluates a single NSE stock for BOTH Long (BUY->SELL) and Short (SELL->BUY) setups."""
    clean = symbol.replace(".NS", "").strip().upper()
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{clean}.NS?interval=5m&range=1d"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=4)
        res = r.json()["chart"]["result"][0]
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

        df = df[(df["open"] > 0) & (df["close"] > 0)].reset_index(drop=True)
        if len(df) < 6:
            return None

        ltp = float(df["close"].iloc[-1])
        if ltp < 50.0 or ltp > 15000.0:
            return None

        # Liquidity check (minimum intraday traded value)
        total_vol = float(df["volume"].sum())
        if (ltp * total_vol) < 25_000_000:
            return None

        # Technical Indicators
        df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        cum_vol = df["volume"].cumsum().replace(0, 1)
        vwap = float((tp * df["volume"]).cumsum().iloc[-1] / cum_vol.iloc[-1])

        delta = df["close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, min_periods=1, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, min_periods=1, adjust=False).mean().replace(0, 0.0001)
        rsi = float(100.0 - (100.0 / (1.0 + (gain.iloc[-1] / loss.iloc[-1]))))

        avg_vol = float(df["volume"].iloc[:-1].mean()) if len(df) > 1 else total_vol
        rvol = float(df["volume"].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0

        # First 3 candles (15m Opening Range)
        orb_high = float(df["high"].iloc[:3].max())
        orb_low = float(df["low"].iloc[:3].min())
        ema9 = float(df["ema9"].iloc[-1])
        ema21 = float(df["ema21"].iloc[-1])

        # --- LONG SETUP (BUY -> SELL) ---
        if ltp > orb_high and ltp > vwap and ema9 > ema21 and 56.0 <= rsi <= 74.0 and rvol >= 1.3:
            prob = min(88.0, 62.0 + (rvol * 3.5) + ((rsi - 55.0) * 0.25))
            if prob > min_prob:
                return {
                    "symbol": clean,
                    "side": "LONG",
                    "action": "BUY",
                    "pattern": "orb_long",
                    "entry_price": round(ltp, 2),
                    "stop_loss": round(max(orb_low, ltp * 0.992), 2),
                    "target_price": round(ltp * 1.015, 2),
                    "timemachine_prob": round(prob, 1),
                    "entry_reason": f"LONG Breakout above 15m ORB High (₹{orb_high:,.2f}) & VWAP (₹{vwap:,.2f}) with {rvol:.1f}x volume.",
                    "tech_params": f"Side: BUY->SELL | RSI(14): {rsi:.1f} | VWAP: ₹{vwap:,.2f} | RVOL: {rvol:.2f}x"
                }

        # --- SHORT SETUP (SELL -> BUY) ---
        if ltp < orb_low and ltp < vwap and ema9 < ema21 and 26.0 <= rsi <= 44.0 and rvol >= 1.3:
            prob = min(88.0, 62.0 + (rvol * 3.5) + ((45.0 - rsi) * 0.25))
            if prob > min_prob:
                return {
                    "symbol": clean,
                    "side": "SHORT",
                    "action": "SELL",
                    "pattern": "orb_short",
                    "entry_price": round(ltp, 2),
                    "stop_loss": round(min(orb_high, ltp * 1.008), 2),
                    "target_price": round(ltp * 0.985, 2),
                    "timemachine_prob": round(prob, 1),
                    "entry_reason": f"SHORT Breakdown below 15m ORB Low (₹{orb_low:,.2f}) & VWAP (₹{vwap:,.2f}) with {rvol:.1f}x selling volume.",
                    "tech_params": f"Side: SELL->BUY | RSI(14): {rsi:.1f} | VWAP: ₹{vwap:,.2f} | RVOL: {rvol:.2f}x"
                }
    except Exception:
        return None
    return None

def scan_full_nse_universe(max_symbols: int = 250, min_prob: float = 65.0) -> list[dict]:
    """Scans the NSE equity universe in parallel and returns top Long & Short setups (>65%)."""
    universe = fetch_all_nse_symbols(max_symbols=max_symbols)
    signals = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(evaluate_nse_symbol, sym, min_prob): sym for sym in universe}
        for fut in as_completed(futures):
            res = fut.result()
            if res is not None:
                signals.append(res)
    signals.sort(key=lambda x: x["timemachine_prob"], reverse=True)
    return signals

if __name__ == "__main__":
    syms = fetch_all_nse_symbols(500)
    print(f"Loaded {len(syms)} NSE EQ symbols. Running sample parallel scan...")
    hits = scan_full_nse_universe(max_symbols=100, min_prob=65.0)
    print(f"Found {len(hits)} setups (>65%): {hits[:5]}")