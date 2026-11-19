#!/usr/bin/env python3
"""Chart Scanner for trader-scanner (NSE India edition). Standard library only.

    py scan.py universe                              # NSE index CSV (nifty200) -> trader/universe.txt
    py scan.py daily                                 # before the open: daily setups -> trader/watchlist.json
    py scan.py daily --symbols RELIANCE,TCS,INFY     # or --watchlist FILE, --limit 50
    py scan.py orb                                   # after 09:30 IST: opening-range breakouts
    py scan.py flag                                  # intraday bull flags
    py scan.py triangle                              # intraday ascending/symmetrical triangles
    py scan.py vwap                                  # intraday VWAP reclaims
    py scan.py patterns --at 10:30                   # all 4 intraday patterns at once
    py scan.py patterns --offline wall5m.json --now "2026-11-18 11:00"

All times are IST (UTC+05:30). Session 09:15-15:30 IST; opening range 09:15-09:30 IST.
Liquidity filters: price >= --min-price (₹50 default) and avg/projected daily turnover
>= --min-turnover-cr (₹25 crore default).
Volume filter: intraday breakout bar volume >= --min-vol-ratio (1.5x default) times the
average volume of prior session bars.

Data: Yahoo Finance (<SYMBOL>.NS, free, unofficial, delayed), Upstox API, or Angel One
SmartAPI (via --feed or config.json data_feed).

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found at least one hit (or universe succeeded), 1 otherwise.
"""

import argparse
import datetime
import gzip
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as N

DEFAULT_UNIVERSE = "trader/universe.txt"
DEFAULT_WATCHLIST = "trader/watchlist.json"
DEFAULT_CONFIG = "trader/config.json"
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

DAILY_SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "daily bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
    ("triangle_daily", "daily triangle breakout"),
]
INTRADAY_SETUPS = [
    ("orb", "opening-range breakout"),
    ("bull_flag", "bull flag"),
    ("triangle", "triangle breakout"),
    ("vwap_reclaim", "VWAP reclaim"),
]
LABEL = dict(DAILY_SETUPS + INTRADAY_SETUPS)


class ScanError(N.NSEError):
    pass


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def load_universe(path=DEFAULT_UNIVERSE):
    """Read trader/universe.txt -> [(sym, name, isin)]. Falls back to N.LIQUID_UNIVERSE."""
    if path and os.path.exists(path):
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                sym = N.norm_symbol(parts[0])
                name = parts[1].strip() if len(parts) > 1 else ""
                isin = parts[2].strip() if len(parts) > 2 else ""
                rows.append((sym, name, isin))
        if rows:
            return rows, path
    return [(s, s, "") for s in N.LIQUID_UNIVERSE], "built-in liquid NSE universe"


def build_universe(index_name="nifty200", offline=None, out_path=DEFAULT_UNIVERSE):
    """Fetch NSE index CSV (or --offline CSV) -> [(sym, name, isin)], fallback N.LIQUID_UNIVERSE."""
    note = ""
    rows = []
    if offline:
        if os.path.isdir(offline):
            cands = [os.path.join(offline, f) for f in os.listdir(offline) if f.lower().endswith(".csv")]
            if not cands:
                raise ScanError("No CSV file found in offline folder %s." % offline)
            src_path = cands[0]
        else:
            src_path = offline
        txt = N.read_maybe_gz(src_path)
        rows = N.parse_index_csv(txt)
        note = "offline CSV (%s)" % os.path.basename(src_path)
    else:
        url = N.NSE_INDEX_CSV.get(index_name)
        if not url:
            raise ScanError("Unknown index %r (choose: %s)." % (index_name, ", ".join(N.NSE_INDEX_CSV)))
        try:
            raw = N.http_get(url, headers={"User-Agent": N.UA_BROWSER, "Referer": N.NSE_WWW + "/"})
            rows = N.parse_index_csv(raw.decode("utf-8", "replace"))
            note = "NSE %s index CSV" % index_name
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, N.NSEError) as e:
            rows = [(s, s, "") for s in N.LIQUID_UNIVERSE]
            note = "built-in liquid NSE fallback (%d names; NSE CSV fetch failed: %s)" % (len(rows), e)

    if not rows:
        rows = [(s, s, "") for s in N.LIQUID_UNIVERSE]
        note = "built-in liquid NSE fallback (%d names; CSV was empty)" % len(rows)

    seen = {}
    clean = []
    for sym, name, isin in rows:
        try:
            ns = N.norm_symbol(sym)
        except N.NSEError:
            continue
        if ns not in seen:
            seen[ns] = True
            clean.append((ns, name or ns, isin or ""))

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# NSE universe (%s)\n" % note)
        for sym, name, isin in clean:
            f.write("%s|%s|%s\n" % (sym, name, isin))
    return clean, note


# ---------------------------------------------------------------------------
# Offline / feed loading for bars
# ---------------------------------------------------------------------------

def _load_offline_bundle(offline_path):
    """Returns dict {SYM: raw_doc_or_bars} or None if directory."""
    if not offline_path:
        return None
    if os.path.isdir(offline_path):
        out = {}
        for fn in os.listdir(offline_path):
            if fn.endswith((".json", ".json.gz")):
                sym = fn.split(".")[0].upper()
                if sym.endswith("_NS"):
                    sym = sym[:-3]
                if sym.endswith(".NS"):
                    sym = sym[:-3]
                out[sym] = json.loads(N.read_maybe_gz(os.path.join(offline_path, fn)))
        return out
    doc = json.loads(N.read_maybe_gz(offline_path))
    if isinstance(doc, dict) and "chart" not in doc and "data" not in doc:
        # bundle {SYM: doc_or_bars}
        return {N.norm_symbol(k): v for k, v in doc.items()}
    return {"_SINGLE": doc}


def _bars_from_any(sym, raw, interval_min=5, now=None):
    """Convert Yahoo v8 chart JSON, Upstox JSON, Angel JSON, or plain list of bar dicts."""
    if isinstance(raw, list):
        out = []
        for b in raw:
            t = b["t"]
            dt = t if isinstance(t, datetime.datetime) else N.parse_now(str(t))
            dt = N.to_ist(dt)
            if now is not None and interval_min < 1440 and dt + datetime.timedelta(minutes=interval_min) > now:
                continue
            out.append({
                "t": dt, "o": float(b["o"]), "h": float(b["h"]),
                "l": float(b["l"]), "c": float(b["c"]), "v": int(b.get("v", 0)),
            })
        out.sort(key=lambda x: x["t"])
        return out
    if isinstance(raw, dict):
        if "chart" in raw:
            bars, _ = N.bars_from_yahoo(raw, interval_min, drop_partial_at=now)
            return bars
        if "data" in raw and isinstance(raw["data"], dict) and "candles" in raw["data"]:
            return N.bars_from_upstox(raw, interval_min, drop_partial_at=now)
        if "data" in raw and isinstance(raw["data"], list):
            return N.bars_from_angel(raw, interval_min, drop_partial_at=now)
        if "bars" in raw:
            return _bars_from_any(sym, raw["bars"], interval_min, now)
    raise ScanError("Unrecognized chart format for %s." % sym)


def fetch_bars_for_symbol(sym, day, interval_min=5, feed="yahoo", now=None, cfg=None, offline_bundle=None):
    """Return list of completed bars for `sym`."""
    if offline_bundle is not None:
        raw = offline_bundle.get(sym) or offline_bundle.get(N.yahoo_symbol(sym)) or offline_bundle.get("_SINGLE")
        if raw is None:
            raise ScanError("No offline bars for %s." % sym)
        bars = _bars_from_any(sym, raw, interval_min, now)
        if interval_min < 1440 and day is not None:
            day_bars = [b for b in bars if b["t"].date() == day]
            return day_bars if day_bars else bars
        return bars

    if interval_min >= 1440:
        if feed == "upstox":
            isin = N.load_upstox_instruments()
            to_d = day.isoformat()
            from_d = (day - datetime.timedelta(days=120)).isoformat()
            doc = N.fetch_upstox_history(sym, 1440, to_d, from_d, isin)
            bars = N.bars_from_upstox(doc, 1440, None)
        elif feed in ("angelone", "angel"):
            tokens = N.load_angel_tokens()
            end_dt = datetime.datetime.combine(day, N.CLOSE, N.IST)
            start_dt = end_dt - datetime.timedelta(days=120)
            doc = N.fetch_angel_candles(sym, 1440, start_dt, end_dt, tokens)
            bars = N.bars_from_angel(doc, 1440, None)
        else:
            doc = N.fetch_yahoo_chart(sym, interval="1d", rng="6mo")
            bars, _ = N.bars_from_yahoo(doc, 1440, drop_partial_at=None)
        # Drop today's partial daily bar if session isn't over yet
        if now is not None and N.market_state(now, cfg) in ("pre_open", "open"):
            bars = [b for b in bars if b["t"].date() < now.date()]
        return bars

    bars, _ = N.get_intraday_bars(sym, day, interval_min=interval_min, feed=feed, now=now, cfg=cfg)
    return bars


# ---------------------------------------------------------------------------
# Pure Liquidity & Pattern Detectors
# ---------------------------------------------------------------------------

def avg_turnover_cr(bars, lookback=20):
    """Average daily turnover in ₹ crore across daily bars (or projected full-day turnover for intraday)."""
    if not bars:
        return 0.0
    sample = bars[-lookback:] if len(bars) > lookback else bars
    is_daily = len(sample) >= 2 and (sample[-1]["t"].date() != sample[0]["t"].date())
    if is_daily:
        vals = [(b["c"] * b["v"]) / 1e7 for b in sample if b.get("v", 0) > 0]
        return round(sum(vals) / len(vals), 2) if vals else 0.0
    # Intraday single-session bars: project cumulative turnover to full 75-bar (375-min) session
    cum_cr = sum((b["c"] * b["v"]) / 1e7 for b in bars)
    elapsed_frac = max(1, len(bars)) / 75.0
    return round(cum_cr / min(1.0, elapsed_frac), 2)


def passes_liquidity(bars, min_price=50.0, min_turnover_cr=25.0):
    if not bars:
        return False, 0.0
    last_price = float(bars[-1]["c"])
    t_cr = avg_turnover_cr(bars)
    if last_price < float(min_price):
        return False, t_cr
    if t_cr < float(min_turnover_cr):
        return False, t_cr
    return True, t_cr


def _vol_ratio(bars, idx):
    """Volume of bar at `idx` divided by average volume of prior bars (at least 1 bar prior)."""
    prior = [b["v"] for b in bars[:idx] if b["v"] > 0]
    if not prior:
        return 0.0
    avg_v = sum(prior[-12:]) / len(prior[-12:])
    if avg_v <= 0:
        return 0.0
    return round(bars[idx]["v"] / avg_v, 2)


def detect_orb(bars, or_minutes=15, min_vol_ratio=1.5):
    """Opening-range breakout on 5m bars (09:15-09:30 IST by default).
    First completed bar after the opening range that CLOSES strictly above or_high
    with bar volume >= min_vol_ratio * avg prior bar volume."""
    if not bars:
        return None
    day = bars[0]["t"].date()
    start_dt = datetime.datetime.combine(day, N.OPEN, N.IST)
    or_end = start_dt + datetime.timedelta(minutes=or_minutes)
    or_bars = [b for b in bars if start_dt <= b["t"] < or_end]
    expected = max(1, or_minutes // 5)
    if len(or_bars) < expected:
        return None
    or_high = max(b["h"] for b in or_bars)
    or_low = min(b["l"] for b in or_bars)
    or_mid = N.round_tick((or_high + or_low) / 2.0, "down")

    for idx, b in enumerate(bars):
        if b["t"] < or_end:
            continue
        if b["c"] > or_high:
            vr = _vol_ratio(bars, idx)
            if vr < float(min_vol_ratio):
                continue
            close_dt = b["t"] + datetime.timedelta(minutes=5)
            over_pct = round((b["c"] - or_high) / or_high * 100.0, 2)
            return {
                "setup": "orb",
                "label": LABEL["orb"],
                "price": round(b["c"], 2),
                "time": N.fmt_time(close_dt),
                "bar_index": idx,
                "volume_ratio": vr,
                "over_pct": over_pct,
                "levels": {
                    "or_high": round(or_high, 2),
                    "or_low": round(or_low, 2),
                    "or_mid": or_mid,
                    "pattern_low": or_mid,
                    "trigger_level": round(or_high, 2),
                },
            }
    return None


def detect_bull_flag(bars, min_vol_ratio=1.5, min_pole_pct=1.2, max_retrace=0.55):
    """Intraday bull flag on 5m bars:
    1) Impulse pole: 3..8 consecutive/window bars gaining >= min_pole_pct % from pole_low to pole_high.
    2) Consolidation flag: 3..8 bars holding inside upper half (flag_low >= pole_high - max_retrace * pole_height)
       with lower average volume than the pole.
    3) Breakout bar closing strictly above flag_high with volume >= min_vol_ratio * avg prior volume."""
    n = len(bars)
    if n < 8:
        return None
    for brk_idx in range(7, n):
        brk = bars[brk_idx]
        for flag_len in range(3, 9):
            f_start = brk_idx - flag_len
            if f_start < 3:
                continue
            flag_bars = bars[f_start:brk_idx]
            flag_high = max(b["h"] for b in flag_bars)
            flag_low = min(b["l"] for b in flag_bars)
            if brk["c"] <= flag_high:
                continue
            for pole_len in range(3, 9):
                p_start = f_start - pole_len
                if p_start < 0:
                    continue
                pole_bars = bars[p_start:f_start]
                pole_low = min(b["l"] for b in pole_bars)
                pole_high = max(b["h"] for b in pole_bars)
                if pole_low <= 0:
                    continue
                pole_gain_pct = (pole_high - pole_low) / pole_low * 100.0
                if pole_gain_pct < min_pole_pct:
                    continue
                # Pole must generally rise (last pole bar close near upper part of pole)
                if pole_bars[-1]["c"] < pole_low + 0.6 * (pole_high - pole_low):
                    continue
                pole_height = pole_high - pole_low
                if flag_low < pole_high - max_retrace * pole_height:
                    continue
                # Flag tightness & lower volume during consolidation
                if flag_high > pole_high * 1.004:
                    continue
                pole_vol = sum(b["v"] for b in pole_bars) / len(pole_bars)
                flag_vol = sum(b["v"] for b in flag_bars) / len(flag_bars)
                if pole_vol > 0 and flag_vol > pole_vol * 0.95:
                    continue
                vr = _vol_ratio(bars, brk_idx)
                if vr < float(min_vol_ratio):
                    continue
                close_dt = brk["t"] + datetime.timedelta(minutes=5)
                return {
                    "setup": "bull_flag",
                    "label": LABEL["bull_flag"],
                    "price": round(brk["c"], 2),
                    "time": N.fmt_time(close_dt),
                    "bar_index": brk_idx,
                    "volume_ratio": vr,
                    "over_pct": round((brk["c"] - flag_high) / flag_high * 100.0, 2),
                    "levels": {
                        "pole_low": round(pole_low, 2),
                        "pole_high": round(pole_high, 2),
                        "flag_high": round(flag_high, 2),
                        "flag_low": round(flag_low, 2),
                        "pattern_low": round(flag_low, 2),
                        "trigger_level": round(flag_high, 2),
                    },
                }
    return None


def detect_triangle(bars, min_vol_ratio=1.5, min_bars=6, max_bars=15):
    """Ascending or symmetrical triangle breakout on 5m bars:
    Window of 6..15 bars prior to breakout where:
    - Higher lows across first half vs second half (min low of second half > min low of first half)
    - Resistance is flat or slightly descending (second half max high <= first half max high * 1.002)
    - Range narrows (second half range < first half range * 0.85)
    - Breakout bar closes strictly above upper resistance with volume >= min_vol_ratio."""
    n = len(bars)
    if n < min_bars + 1:
        return None
    for brk_idx in range(min_bars, n):
        brk = bars[brk_idx]
        for w_len in range(min(max_bars, brk_idx), min_bars - 1, -1):
            win = bars[brk_idx - w_len:brk_idx]
            half = w_len // 2
            h1, h2 = win[:half], win[half:]
            hi1, lo1 = max(b["h"] for b in h1), min(b["l"] for b in h1)
            hi2, lo2 = max(b["h"] for b in h2), min(b["l"] for b in h2)
            # Higher lows, flat/lower highs, narrowing range
            if lo2 <= lo1:
                continue
            if hi2 > hi1 * 1.002:
                continue
            r1 = hi1 - lo1
            r2 = hi2 - lo2
            if r1 <= 0 or r2 >= r1 * 0.85:
                continue
            upper = round(max(hi1, hi2) if abs(hi1 - hi2) / hi1 <= 0.002 else hi2, 2)
            lower = round(lo2, 2)
            if brk["c"] <= upper:
                continue
            vr = _vol_ratio(bars, brk_idx)
            if vr < float(min_vol_ratio):
                continue
            close_dt = brk["t"] + datetime.timedelta(minutes=5)
            return {
                "setup": "triangle",
                "label": LABEL["triangle"],
                "price": round(brk["c"], 2),
                "time": N.fmt_time(close_dt),
                "bar_index": brk_idx,
                "volume_ratio": vr,
                "over_pct": round((brk["c"] - upper) / upper * 100.0, 2),
                "levels": {
                    "triangle_upper": upper,
                    "triangle_lower": lower,
                    "pattern_low": lower,
                    "trigger_level": upper,
                },
            }
    return None


def compute_vwap_series(bars):
    """Cumulative session VWAP per bar."""
    cum_pv = 0.0
    cum_v = 0
    out = []
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        v = max(1, int(b["v"]))
        cum_pv += tp * v
        cum_v += v
        out.append(cum_pv / cum_v)
    return out


def detect_vwap_reclaim(bars, min_vol_ratio=1.5, min_below_bars=2):
    """VWAP reclaim on 5m bars:
    After at least 3 opening bars, price closes strictly below session VWAP for at least
    `min_below_bars` bars, then a bar closes strictly above VWAP with volume >= min_vol_ratio."""
    if len(bars) < 6:
        return None
    vwaps = compute_vwap_series(bars)
    below_streak = 0
    recent_low = None
    for idx in range(3, len(bars)):
        b = bars[idx]
        vw = vwaps[idx]
        if b["c"] < vw:
            below_streak += 1
            recent_low = b["l"] if recent_low is None else min(recent_low, b["l"])
        elif b["c"] > vw and below_streak >= min_below_bars:
            vr = _vol_ratio(bars, idx)
            if vr >= float(min_vol_ratio):
                close_dt = b["t"] + datetime.timedelta(minutes=5)
                reclaim_low = round(recent_low if recent_low is not None else b["l"], 2)
                return {
                    "setup": "vwap_reclaim",
                    "label": LABEL["vwap_reclaim"],
                    "price": round(b["c"], 2),
                    "time": N.fmt_time(close_dt),
                    "bar_index": idx,
                    "volume_ratio": vr,
                    "over_pct": round((b["c"] - vw) / vw * 100.0, 2),
                    "levels": {
                        "vwap": round(vw, 2),
                        "reclaim_low": reclaim_low,
                        "pattern_low": reclaim_low,
                        "trigger_level": round(vw, 2),
                    },
                }
            # volume failed: keep streak only if still below, else reset
            below_streak = 0
            recent_low = None
        else:
            below_streak = 0
            recent_low = None
    return None


def wilder_rsi(closes, period=14):
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    out = [None] * period

    def _rsi(g, l):
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    out.append(_rsi(avg_g, avg_l))
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        out.append(_rsi(avg_g, avg_l))
    return out


def detect_daily_setups(bars):
    """Run daily close setups on 1d bars (>= 30 bars). Returns list of setup ids matched."""
    if len(bars) < 25:
        return []
    closes = [b["c"] for b in bars]
    last_c = closes[-1]
    prev_c = closes[-2]
    hits = []

    # 1. breakout_20d
    if last_c > max(closes[-21:-1]):
        hits.append(("breakout_20d", {"level": round(max(closes[-21:-1]), 2)}))

    # 2. bull_flag daily (up > 10% in 10 sessions prior to last 5, then last 5 inside 5%)
    if len(closes) >= 16:
        pole_base = closes[-16]
        pole_top = closes[-6]
        win5 = closes[-5:]
        if pole_base > 0 and (pole_top - pole_base) / pole_base > 0.10:
            if min(win5) > 0 and (max(win5) - min(win5)) / min(win5) <= 0.05:
                hits.append(("bull_flag", {"flag_high": round(max(win5), 2), "flag_low": round(min(win5), 2)}))

    # 3. reclaim_ma20
    if len(closes) >= 21:
        ma20_prev = sum(closes[-21:-1]) / 20.0
        ma20_now = sum(closes[-20:]) / 20.0
        if prev_c < ma20_prev and last_c > ma20_now:
            hits.append(("reclaim_ma20", {"ma20": round(ma20_now, 2)}))

    # 4 & 5. oversold / oversold_bounce
    rsis = wilder_rsi(closes, 14)
    if rsis and rsis[-1] is not None:
        if rsis[-1] < 30.0:
            hits.append(("oversold", {"rsi14": round(rsis[-1], 2)}))
        if len(rsis) >= 2 and rsis[-2] is not None and rsis[-2] < 30.0 and last_c > prev_c:
            hits.append(("oversold_bounce", {"rsi14_prev": round(rsis[-2], 2), "rsi14": round(rsis[-1], 2)}))

    # 6. daily triangle breakout
    tri = detect_triangle(bars, min_vol_ratio=1.2)
    if tri:
        hits.append(("triangle_daily", tri["levels"]))

    return hits


# ---------------------------------------------------------------------------
# Scan runners
# ---------------------------------------------------------------------------

def run_intraday_scan(mode, symbols, day, now, cfg, feed="yahoo", offline_bundle=None,
                      min_price=50.0, min_turnover_cr=25.0, min_vol_ratio=1.5, or_minutes=15):
    detectors = {
        "orb": lambda b: detect_orb(b, or_minutes=or_minutes, min_vol_ratio=min_vol_ratio),
        "flag": lambda b: detect_bull_flag(b, min_vol_ratio=min_vol_ratio),
        "triangle": lambda b: detect_triangle(b, min_vol_ratio=min_vol_ratio),
        "vwap": lambda b: detect_vwap_reclaim(b, min_vol_ratio=min_vol_ratio),
    }
    active = list(detectors.keys()) if mode == "patterns" else [mode]

    hits = []
    usable = 0
    failed = 0
    filtered_illiquid = 0
    for idx, sym in enumerate(symbols):
        try:
            bars = fetch_bars_for_symbol(sym, day, interval_min=5, feed=feed, now=now, cfg=cfg,
                                         offline_bundle=offline_bundle)
        except N.NSEError:
            failed += 1
            continue
        if not bars:
            failed += 1
            continue
        usable += 1
        ok_liq, t_cr = passes_liquidity(bars, min_price=min_price, min_turnover_cr=min_turnover_cr)
        if not ok_liq:
            filtered_illiquid += 1
            continue
        for key in active:
            res = detectors[key](bars)
            if res:
                res["symbol"] = sym
                res["avg_turnover_cr"] = t_cr
                hits.append(res)
        if offline_bundle is None and idx + 1 < len(symbols):
            N.SLEEP(N.PAUSE)

    hits.sort(key=lambda h: (h["avg_turnover_cr"], h["volume_ratio"]), reverse=True)
    return {
        "mode": mode,
        "as_of": now.isoformat(),
        "market": "NSE",
        "feed": feed,
        "universe": len(symbols),
        "scanned": usable,
        "failed": failed,
        "filtered_illiquid": filtered_illiquid,
        "hits": hits,
    }


def run_daily_scan(symbols, day, now, cfg, feed="yahoo", offline_bundle=None,
                   min_price=50.0, min_turnover_cr=25.0):
    hits = []
    usable = 0
    failed = 0
    filtered_illiquid = 0
    for idx, sym in enumerate(symbols):
        try:
            bars = fetch_bars_for_symbol(sym, day, interval_min=1440, feed=feed, now=now, cfg=cfg,
                                         offline_bundle=offline_bundle)
        except N.NSEError:
            failed += 1
            continue
        if len(bars) < 25:
            failed += 1
            continue
        usable += 1
        ok_liq, t_cr = passes_liquidity(bars, min_price=min_price, min_turnover_cr=min_turnover_cr)
        if not ok_liq:
            filtered_illiquid += 1
            continue
        matches = detect_daily_setups(bars)
        last_b = bars[-1]
        for setup_id, lvls in matches:
            vr = _vol_ratio(bars, len(bars) - 1)
            hits.append({
                "symbol": sym,
                "setup": setup_id,
                "label": LABEL.get(setup_id, setup_id),
                "price": round(last_b["c"], 2),
                "time": last_b["t"].date().isoformat(),
                "volume_ratio": vr,
                "avg_turnover_cr": t_cr,
                "levels": lvls,
            })
        if offline_bundle is None and idx + 1 < len(symbols):
            N.SLEEP(N.PAUSE)

    hits.sort(key=lambda h: h["avg_turnover_cr"], reverse=True)
    return {
        "mode": "daily",
        "as_of": now.isoformat(),
        "market": "NSE",
        "feed": feed,
        "universe": len(symbols),
        "scanned": usable,
        "failed": failed,
        "filtered_illiquid": filtered_illiquid,
        "hits": hits,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_symbols(args, cfg):
    if args.symbols:
        return N.parse_symbols(args.symbols)
    if args.watchlist:
        wl = N.read_json(args.watchlist, {})
        syms = [h["symbol"] for h in (wl or {}).get("hits", []) if h.get("symbol")]
        if not syms:
            raise ScanError("No symbols in watchlist %s." % args.watchlist)
        return list(dict.fromkeys(N.norm_symbol(s) for s in syms))
    rows, _ = load_universe(args.universe)
    syms = [r[0] for r in rows]
    if args.limit and args.limit > 0:
        syms = syms[: args.limit]
    return syms


def _print_report(doc, show_limit=40):
    hits = doc["hits"]
    print("NSE %s scan (%s) | feed: %s | %d universe, %d scanned, %d failed, %d filtered by liquidity"
          % (doc["mode"], doc["as_of"][:19], doc["feed"], doc["universe"], doc["scanned"],
             doc["failed"], doc.get("filtered_illiquid", 0)))
    if not hits:
        print("No setups matched the rule.")
        return
    print("%d setup hit(s) (most liquid first):" % len(hits))
    for h in hits[:show_limit]:
        lvls = ", ".join("%s=%s" % (k, v) for k, v in (h.get("levels") or {}).items() if k != "pattern_low")
        print("  %-12s %-14s %10s  vol %.2fx  turnover %s  [%s] (%s)"
              % (h["symbol"], h["setup"], N.inr(h["price"]), h.get("volume_ratio", 0.0),
                 N.crore(h.get("avg_turnover_cr", 0.0) * 1e7), h["time"], lvls))
    if len(hits) > show_limit:
        print("  ... and %d more (see --show or --json)." % (len(hits) - show_limit))
    print(NOTE)


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Chart Scanner (trader-scanner)")
    p.add_argument("mode", choices=("universe", "daily", "orb", "flag", "triangle", "vwap", "patterns"),
                   help="What to run")
    p.add_argument("--index", default="nifty200", choices=tuple(N.NSE_INDEX_CSV.keys()),
                   help="NSE index CSV for universe (default nifty200)")
    p.add_argument("--symbols", help="Comma-separated NSE symbols (e.g. RELIANCE,TCS,INFY)")
    p.add_argument("--watchlist", help="Scan symbols from an existing watchlist JSON")
    p.add_argument("--universe", default=DEFAULT_UNIVERSE, help="Path to universe.txt")
    p.add_argument("--limit", type=int, default=0, help="Cap symbols scanned (0 = all)")
    p.add_argument("--feed", choices=("yahoo", "upstox", "angelone"), help="Market data feed")
    p.add_argument("--min-price", type=float, help="Min share price in INR (default from config)")
    p.add_argument("--min-turnover-cr", type=float, help="Min daily turnover in INR crore (default from config)")
    p.add_argument("--min-vol-ratio", type=float, default=1.5, help="Min breakout bar volume ratio (default 1.5)")
    p.add_argument("--or-minutes", type=int, default=15, help="Opening range minutes (default 15: 09:15-09:30 IST)")
    p.add_argument("--at", help="Cut intraday clock at HH:MM IST today")
    p.add_argument("--now", help="Pin IST clock: 'YYYY-MM-DD HH:MM'")
    p.add_argument("--offline", help="Read saved CSV or JSON/folder instead of network")
    p.add_argument("--no-write", action="store_true", help="Do not write trader/watchlist.json")
    p.add_argument("--show", type=int, default=40, help="Max rows to print")
    p.add_argument("--json", action="store_true", help="Print JSON output")
    args = p.parse_args(argv)

    try:
        cfg = N.load_config(DEFAULT_CONFIG)
        if args.mode == "universe":
            clean, note = build_universe(args.index, offline=args.offline, out_path=args.universe)
            if args.json:
                print(json.dumps({"count": len(clean), "source": note, "file": args.universe}, indent=2))
            else:
                print("Wrote %d NSE symbols to %s (%s)." % (len(clean), args.universe, note))
            return 0

        now = N.parse_now(args.now) if args.now else N.now_ist()
        if args.at:
            t = N.hhmm(args.at)
            now = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)

        feed = args.feed or cfg.get("data_feed") or "yahoo"
        min_price = args.min_price if args.min_price is not None else float(cfg.get("min_price_inr", 50))
        min_turn = args.min_turnover_cr if args.min_turnover_cr is not None else float(cfg.get("min_avg_turnover_cr", 25))
        offline_bundle = _load_offline_bundle(args.offline) if args.offline else None
        syms = _resolve_symbols(args, cfg)

        if args.mode != "daily":
            if not args.offline:
                reason = N.closed_reason(now.date(), cfg)
                if reason:
                    raise ScanError(reason)
                or_end_t = (datetime.datetime.combine(now.date(), N.OPEN, N.IST)
                            + datetime.timedelta(minutes=args.or_minutes))
                if args.mode == "orb" and now < or_end_t + datetime.timedelta(minutes=5):
                    raise ScanError("Too early for ORB at %s (first 5m bar after 09:%02d closes at %s)."
                                    % (N.fmt_time(now), 15 + args.or_minutes,
                                       N.fmt_time(or_end_t + datetime.timedelta(minutes=5))))
                cutoff_t = N.config_time(cfg, "no_entries_after", N.NO_ENTRIES_AFTER)
                if now.time() > N.hhmm(cfg.get("safety_cutoff", N.SAFETY_CUTOFF)):
                    raise ScanError("Past safety cutoff (%s IST): intraday scan expired."
                                    % cfg.get("safety_cutoff", N.SAFETY_CUTOFF))
            doc = run_intraday_scan(
                args.mode, syms, now.date(), now, cfg, feed=feed,
                offline_bundle=offline_bundle, min_price=min_price,
                min_turnover_cr=min_turn, min_vol_ratio=args.min_vol_ratio,
                or_minutes=args.or_minutes,
            )
        else:
            doc = run_daily_scan(
                syms, now.date(), now, cfg, feed=feed,
                offline_bundle=offline_bundle, min_price=min_price,
                min_turnover_cr=min_turn,
            )

        if not args.no_write:
            N.write_json(DEFAULT_WATCHLIST, doc)

        if args.json:
            print(json.dumps(doc, indent=2))
        else:
            _print_report(doc, show_limit=args.show)
        return 0 if doc["hits"] else 1
    except N.NSEError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
