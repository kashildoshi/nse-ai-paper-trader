#!/usr/bin/env python3
"""nse_common: the shared NSE (India) layer for the trader-* skills. Standard library only.

ONE master copy lives in shared/nse_common.py. tools/sync_common.py copies it,
byte for byte, into every .agent/skills/trader-*/ folder so each skill still
works when copied on its own. Never edit a copy: edit the master and re-sync.

What is in here:
  * The NSE clock: IST (UTC+05:30, no daylight saving), the 09:15-15:30 session,
    the 2026 trading-holiday list (NSE circular NSE/CMTR/71775), and helpers.
  * Symbols: plain NSE symbols (RELIANCE, M&M, BAJAJ-AUTO) <-> Yahoo (.NS),
    a built-in liquid universe (NIFTY 50 + liquid NIFTY Next 50 / midcaps).
  * Price ticks: NSE cash-market tick sizes by price band (effective 2025-04-15).
  * Data feeds, one bar format for all of them:
      yahoo     free, unofficial, delayed; SYMBOL.NS on query1/query2
      upstox    Upstox API v2/v3, needs UPSTOX_ACCESS_TOKEN (env)
      angelone  Angel One SmartAPI, needs ANGEL_API_KEY + ANGEL_JWT_TOKEN (env)
  * An NSE website session for the public JSON/RSS disclosure endpoints.
  * Indian intraday (MIS) equity charges: brokerage, STT, exchange, SEBI,
    stamp duty, GST.
  * The ratcheting trailing stop used by trader-exit, -timemachine and -alerts.
  * INR formatting with Indian digit grouping (Rs 1,23,456.78 / ₹).

Nothing here places an order. Credentials are read from environment variables
only, never from the chat and never written to disk.
"""

import datetime
import gzip
import http.cookiejar
import io
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Clock: IST, session, holidays
# ---------------------------------------------------------------------------

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")
UTC = datetime.timezone.utc

PRE_OPEN = datetime.time(9, 0)        # pre-open call auction 09:00-09:08
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
OR_END_DEFAULT = datetime.time(9, 30)  # 15-minute opening range 09:15-09:30
SAFETY_CUTOFF = "15:20"                # square off everything by here (before broker MIS auto square-off)
NO_ENTRIES_BEFORE = "09:30"
NO_ENTRIES_AFTER = "15:00"

# NSE equity-segment trading holidays, 2026 (NSE/CMTR/71775, circular 172/2025).
NSE_HOLIDAYS = {
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
# Special sessions on a weekend (Muhurat trading). Times are notified by NSE
# shortly before; a skill treats the day as a trading day only if the user
# adds the session to config "special_sessions": {"2026-11-08": ["18:00", "19:00"]}.
NSE_SPECIAL_NOTE = {"2026-11-08": "Muhurat trading (Diwali Laxmi Pujan): timings notified separately"}
CALENDAR_YEARS = (2026,)


class NSEError(Exception):
    """A plain-English failure: the message is safe to print as one line."""


def now_ist():
    return datetime.datetime.now(UTC).astimezone(IST)


def to_ist(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def parse_now(text):
    """--now "2026-11-18 10:15" (IST) or "2026-11-18T04:45:00Z" (UTC). Returns an aware IST datetime."""
    if not text:
        return now_ist()
    t = text.strip()
    if t.endswith("Z"):
        dt = datetime.datetime.fromisoformat(t[:-1].replace(" ", "T")).replace(tzinfo=UTC)
        return dt.astimezone(IST)
    t = t.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(t, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    try:
        return to_ist(datetime.datetime.fromisoformat(t))
    except ValueError:
        raise NSEError("Could not read the time %r: use \"YYYY-MM-DD HH:MM\" (IST)." % text)


def hhmm(text):
    """'15:20' -> datetime.time(15, 20)."""
    if isinstance(text, datetime.time):
        return text
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text or ""))
    if not m:
        raise NSEError("Could not read the time of day %r: use HH:MM (24-hour, IST)." % text)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def fmt_time(t):
    """24-hour IST label, the Indian-market convention: '10:15 IST'."""
    if isinstance(t, datetime.datetime):
        t = to_ist(t).time()
    return "%02d:%02d IST" % (t.hour, t.minute)


def holidays(cfg=None):
    out = dict(NSE_HOLIDAYS)
    for d in (cfg or {}).get("extra_holidays", []) or []:
        out[str(d)] = "holiday (from config)"
    return out


def is_trading_day(day, cfg=None):
    """day: datetime.date. Weekends and NSE holidays are closed. Years beyond the
    built-in calendar are treated as open on weekdays and flagged by calendar_note()."""
    if isinstance(day, datetime.datetime):
        day = to_ist(day).date()
    ds = day.isoformat()
    if ds in ((cfg or {}).get("special_sessions") or {}):
        return True
    if day.weekday() >= 5:
        return False
    return ds not in holidays(cfg)


def calendar_note(day):
    if isinstance(day, datetime.datetime):
        day = day.date()
    if day.year not in CALENDAR_YEARS:
        return ("The built-in NSE holiday list covers %s only: add %d holidays to "
                "config \"extra_holidays\"." % (", ".join(map(str, CALENDAR_YEARS)), day.year))
    return ""


def closed_reason(day, cfg=None):
    if isinstance(day, datetime.datetime):
        day = to_ist(day).date()
    if day.weekday() >= 5 and day.isoformat() not in ((cfg or {}).get("special_sessions") or {}):
        return "NSE is closed on %s (weekend)." % day.strftime("%A %d %b %Y")
    h = holidays(cfg).get(day.isoformat())
    if h:
        return "NSE is closed on %s (%s)." % (day.strftime("%a %d %b %Y"), h)
    return ""


def session_bounds(day, cfg=None):
    """(open_dt, close_dt) as aware IST datetimes for a trading day."""
    sp = ((cfg or {}).get("special_sessions") or {}).get(day.isoformat())
    if sp:
        o, c = hhmm(sp[0]), hhmm(sp[1])
    else:
        o, c = OPEN, CLOSE
    return (datetime.datetime.combine(day, o, IST), datetime.datetime.combine(day, c, IST))


def previous_trading_day(day, cfg=None):
    d = day - datetime.timedelta(days=1)
    while not is_trading_day(d, cfg):
        d -= datetime.timedelta(days=1)
    return d


def market_state(now, cfg=None):
    """'closed' | 'pre_open' | 'open' | 'post_close' for an aware datetime."""
    now = to_ist(now)
    if not is_trading_day(now.date(), cfg):
        return "closed"
    o, c = session_bounds(now.date(), cfg)
    if now < o.replace(hour=PRE_OPEN.hour, minute=PRE_OPEN.minute) and o.time() == OPEN:
        return "closed"
    if now < o:
        return "pre_open"
    if now < c:
        return "open"
    return "post_close"


def config_time(cfg, key, default):
    return hhmm((cfg or {}).get(key) or default)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_DEFAULTS = {
    "market": "NSE",
    "currency": "INR",
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 200000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 1000,
    "max_daily_loss_inr": 3000,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": NO_ENTRIES_BEFORE,
    "no_entries_after": NO_ENTRIES_AFTER,
    "flat_by": SAFETY_CUTOFF,
    "safety_cutoff": SAFETY_CUTOFF,
    "trail_activate_pct": 0.5,
    "trail_pct": 0.6,
    "product": "MIS",
    "broker": "upstox",
    "data_feed": "yahoo",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "allow_short": False,
    "min_avg_turnover_cr": 25,
    "min_price_inr": 50,
    "max_price_band_pct": None,
    "charges": {},
    "tax_slab_pct": None,
    "extra_holidays": [],
    "special_sessions": {},
    "contact_email": "",
}


def load_config(path="trader/config.json"):
    """Defaults overlaid with trader/config.json (if any). "paper" stays True unless
    the file says false. Old US keys (*_usd) are read as INR with a note."""
    cfg = dict(CONFIG_DEFAULTS)
    notes = []
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
        except (OSError, ValueError) as e:
            raise NSEError("Could not read %s: %s" % (path, e))
        for k, v in user.items():
            if k.endswith("_usd"):
                nk = k[:-4] + "_inr"
                if nk not in user:
                    cfg[nk] = v
                    notes.append("%s is a US key; read as %s (rupees). Rename it." % (k, nk))
                continue
            if k == "pdt_guard":
                notes.append("pdt_guard is a US rule and is ignored on NSE.")
                continue
            cfg[k] = v
    cfg["paper"] = cfg.get("paper") is not False
    cfg["_notes"] = notes
    return cfg


# ---------------------------------------------------------------------------
# Money and prices
# ---------------------------------------------------------------------------

RUPEE = "\u20b9"


def indian_group(n_int):
    s = str(abs(int(n_int)))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def inr(x, decimals=2, symbol=None):
    """inr(123456.5) -> '₹1,23,456.50'. symbol=None picks ₹ unless the console
    cannot print it (then 'Rs ')."""
    if x is None:
        return "-"
    sym = symbol if symbol is not None else _rupee_symbol()
    neg = x < 0
    x = abs(float(x))
    whole = int(math.floor(x + 1e-9)) if decimals else int(round(x))
    frac = x - whole
    out = sym + indian_group(whole)
    if decimals:
        cents = int(round(frac * (10 ** decimals)))
        if cents >= 10 ** decimals:
            whole += 1
            cents = 0
            out = sym + indian_group(whole)
        out += "." + str(cents).zfill(decimals)
    return ("-" + out) if neg else out


def _rupee_symbol():
    import sys
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    try:
        RUPEE.encode(enc or "ascii")
        return RUPEE
    except (UnicodeEncodeError, LookupError):
        return "Rs "


def crore(x):
    """Turnover label: 1.23e9 -> '₹123.0 cr'."""
    return "%s%.1f cr" % (_rupee_symbol(), float(x) / 1e7)


def tick_size(price):
    """NSE cash-market tick size by price band (effective 2025-04-15, reviewed monthly by NSE)."""
    p = float(price)
    if p < 250:
        return 0.01
    if p < 1000:
        return 0.05
    if p < 5000:
        return 0.10
    if p < 10000:
        return 0.50
    if p < 20000:
        return 1.00
    return 5.00


def round_tick(price, mode="nearest", tick=None):
    """Round to the NSE tick. mode: 'down' | 'up' | 'nearest'. Integer arithmetic in paise."""
    t = tick or tick_size(price)
    tp = int(round(t * 100))
    p = float(price) * 100
    q = p / tp
    if mode == "down":
        n = math.floor(q + 1e-9)
    elif mode == "up":
        n = math.ceil(q - 1e-9)
    else:
        n = math.floor(q + 0.5)
    return round(n * tp / 100.0, 2)


# ---------------------------------------------------------------------------
# Symbols and the liquid universe
# ---------------------------------------------------------------------------

# NIFTY 50 (Sep 2025 composition) + liquid NIFTY Next 50 / midcap names.
# Refresh with:  scan.py universe   (downloads NSE's index constituent CSVs).
NIFTY50 = (
    "ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV BEL "
    "BHARTIARTL CIPLA COALINDIA DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK HDFCLIFE "
    "HEROMOTOCO HINDALCO HINDUNILVR ICICIBANK INDIGO INFY ITC JIOFIN JSWSTEEL KOTAKBANK LT M&M "
    "MARUTI MAXHEALTH NESTLEIND NTPC ONGC POWERGRID RELIANCE SBILIFE SBIN SHRIRAMFIN SUNPHARMA "
    "TATACONSUM TATAMOTORS TATASTEEL TCS TECHM TITAN TRENT ULTRACEMCO WIPRO"
).split()
LIQUID_EXTRA = (
    "ABB ADANIGREEN ADANIPOWER AMBUJACEM AUROPHARMA BANKBARODA BHEL BOSCHLTD BPCL CANBK "
    "CHOLAFIN DABUR DIVISLAB DLF DMART GAIL GODREJCP HAL HAVELLS HINDPETRO IDFCFIRSTB INDHOTEL "
    "INDUSINDBK INDUSTOWER IOC IRCTC IRFC JINDALSTEL LICI LODHA LUPIN MOTHERSON NAUKRI NHPC "
    "PFC PIDILITIND PNB POLYCAB RECLTD SAIL SIEMENS SRF TATAPOWER TORNTPHARM TVSMOTOR UNIONBANK "
    "VBL VEDL YESBANK ZYDUSLIFE BSE CDSL DIXON PERSISTENT COFORGE KALYANKJIL MAZDOCK SUZLON"
).split()
LIQUID_UNIVERSE = tuple(dict.fromkeys(NIFTY50 + LIQUID_EXTRA))

NSE_INDEX_CSV = {
    "nifty50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "nifty100": "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    "nifty200": "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "nifty500": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "midcap150": "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
}
NSE_EQUITY_MASTER = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

_SYM_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")


def norm_symbol(sym):
    """'reliance.ns' / 'NSE:RELIANCE' / 'RELIANCE-EQ' -> 'RELIANCE'. Raises on junk."""
    s = str(sym or "").strip().upper()
    for p in ("NSE:", "NSE_EQ:", "NSE_EQ|"):
        if s.startswith(p):
            s = s[len(p):]
    for suf in (".NS", "-EQ"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    if s.endswith(".BO"):
        raise NSEError("%s is a BSE symbol; these skills trade NSE (use the NSE symbol)." % sym)
    if not _SYM_RE.match(s):
        raise NSEError("%r is not an NSE symbol (like RELIANCE, M&M, BAJAJ-AUTO)." % sym)
    return s


def yahoo_symbol(sym):
    return norm_symbol(sym) + ".NS"


def parse_symbols(text):
    out = []
    for part in re.split(r"[,\s]+", text or ""):
        if part:
            out.append(norm_symbol(part))
    return list(dict.fromkeys(out))


def parse_index_csv(text):
    """NSE index constituent CSV -> [(SYMBOL, Company Name)]. Columns:
    Company Name,Industry,Symbol,Series,ISIN Code. Keeps series EQ / BE / BZ? -> EQ only."""
    import csv
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        sym = r.get("Symbol") or r.get("SYMBOL")
        series = (r.get("Series") or r.get(" SERIES") or r.get("SERIES") or "EQ").upper()
        if sym and series == "EQ":
            rows.append((sym.upper(), r.get("Company Name") or r.get("NAME OF COMPANY") or "",
                         r.get("ISIN Code") or r.get(" ISIN NUMBER") or r.get("ISIN NUMBER") or ""))
    return rows


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

UA_BOT = "day-trader-skill-nse/1.0 (+personal research; contact in config)"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PAUSE = 0.35
SLEEP = time.sleep  # tests swap this out


def _read(resp):
    data = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def http_get(url, headers=None, timeout=20, opener=None, data=None, method=None):
    h = {"User-Agent": UA_BROWSER, "Accept": "*/*", "Accept-Encoding": "gzip"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, data=data, method=method)
    op = opener.open if opener else urllib.request.urlopen
    with op(req, timeout=timeout) as r:
        return _read(r)


def http_json(url, headers=None, timeout=20, opener=None, data=None, method=None):
    raw = http_get(url, headers=headers, timeout=timeout, opener=opener, data=data, method=method)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise NSEError("%s did not return JSON (it may be blocking scripted requests)." % _host(url))


def _host(url):
    return urllib.parse.urlparse(url).netloc


def read_maybe_gz(path):
    with open(path, "rb") as f:
        data = f.read()
    if path.endswith(".gz") or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8")


# ---------------------------------------------------------------------------
# Bars: one format for every feed
#   bar = {"t": aware IST datetime (bar START), "o","h","l","c": float, "v": int}
# ---------------------------------------------------------------------------

YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")


def yahoo_chart_url(sym, interval="5m", rng="1d", host=YAHOO_HOSTS[0], period1=None, period2=None):
    q = {"interval": interval, "includePrePost": "false"}
    if period1 is not None:
        q["period1"], q["period2"] = int(period1), int(period2)
    else:
        q["range"] = rng
    return "%s/v8/finance/chart/%s?%s" % (host, urllib.parse.quote(yahoo_symbol(sym)), urllib.parse.urlencode(q))


def fetch_yahoo_chart(sym, interval="5m", rng="1d", period1=None, period2=None):
    """Raw Yahoo v8 chart JSON for SYMBOL.NS. query2 first, query1 on 429/5xx, one retry after a pause."""
    last = None
    for attempt, host in enumerate(YAHOO_HOSTS + (YAHOO_HOSTS[1],)):
        try:
            return http_json(yahoo_chart_url(sym, interval, rng, host, period1, period2),
                             headers={"User-Agent": UA_BROWSER})
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                raise NSEError("Yahoo has no chart for %s." % yahoo_symbol(sym))
            if e.code == 429 and attempt == 1:
                SLEEP(5.0)
            elif e.code not in (429,) and e.code < 500:
                break
        except (urllib.error.URLError, OSError) as e:
            last = e
    raise NSEError("Yahoo chart for %s failed: %s" % (yahoo_symbol(sym), last))


def bars_from_yahoo(doc, interval_min=5, drop_partial_at=None):
    """Yahoo v8 chart JSON -> (bars, meta). Null closes dropped; off-grid ticks dropped;
    with drop_partial_at (aware dt) a bar counts only once start + interval <= that time."""
    try:
        res = doc["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        err = (doc or {}).get("chart", {}).get("error") if isinstance(doc, dict) else None
        raise NSEError("Yahoo returned no chart%s." % (": %s" % err.get("description") if err else ""))
    meta = res.get("meta", {}) or {}
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    out = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c, v = (q["open"][i], q["high"][i], q["low"][i], q["close"][i], (q.get("volume") or [0] * len(ts))[i])
        except (KeyError, IndexError):
            continue
        if c is None or o is None or h is None or l is None:
            continue
        dt = datetime.datetime.fromtimestamp(int(t), UTC).astimezone(IST)
        if interval_min and interval_min < 1440 and (dt.minute * 60 + dt.second) % (interval_min * 60) != (OPEN.minute * 60) % (interval_min * 60):
            continue
        if drop_partial_at is not None and interval_min < 1440 and dt + datetime.timedelta(minutes=interval_min) > drop_partial_at:
            continue
        out.append({"t": dt, "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": int(v or 0)})
    return out, meta


# --- Upstox -----------------------------------------------------------------

UPSTOX_API = "https://api.upstox.com"
UPSTOX_INSTRUMENTS = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


def upstox_token():
    tok = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if not tok:
        raise NSEError("data_feed is upstox but UPSTOX_ACCESS_TOKEN is not set (set it in your shell, never in the chat).")
    return tok


def upstox_headers():
    return {"Authorization": "Bearer " + upstox_token(), "Accept": "application/json"}


def upstox_instrument_key(sym, isin_map=None):
    """NSE_EQ|<ISIN>. isin_map {SYMBOL: ISIN} comes from trader/instruments-upstox.json
    (built by load_upstox_instruments) or NSE's EQUITY_L.csv."""
    s = norm_symbol(sym)
    isin = (isin_map or {}).get(s)
    if not isin:
        raise NSEError("No ISIN for %s: run the universe/instruments refresh first." % s)
    return "NSE_EQ|" + isin


def load_upstox_instruments(path="trader/instruments-upstox.json", refresh=False):
    """{SYMBOL: ISIN} for NSE_EQ equities, cached in trader/."""
    if os.path.exists(path) and not refresh:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    rows = json.loads(http_get(UPSTOX_INSTRUMENTS).decode("utf-8"))
    out = {}
    for r in rows:
        if r.get("segment") == "NSE_EQ" and r.get("instrument_type") in ("EQ", None) and r.get("trading_symbol"):
            key = r.get("instrument_key", "")
            if key.startswith("NSE_EQ|"):
                out[r["trading_symbol"].upper()] = key.split("|", 1)[1]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


def bars_from_upstox(doc, interval_min=5, drop_partial_at=None):
    """Upstox candles: data.candles = [[iso_ts, o, h, l, c, v, oi], ...] newest first."""
    candles = ((doc or {}).get("data") or {}).get("candles") or []
    out = []
    for row in candles:
        dt = to_ist(datetime.datetime.fromisoformat(row[0]))
        if drop_partial_at is not None and dt + datetime.timedelta(minutes=interval_min) > drop_partial_at:
            continue
        out.append({"t": dt, "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                    "c": float(row[4]), "v": int(row[5] or 0)})
    out.sort(key=lambda b: b["t"])
    return out


def fetch_upstox_intraday(sym, interval_min=5, isin_map=None):
    key = urllib.parse.quote(upstox_instrument_key(sym, isin_map), safe="")
    url = "%s/v3/historical-candle/intraday/%s/minutes/%d" % (UPSTOX_API, key, interval_min)
    return http_json(url, headers=upstox_headers())


def fetch_upstox_history(sym, interval_min, to_date, from_date, isin_map=None):
    key = urllib.parse.quote(upstox_instrument_key(sym, isin_map), safe="")
    if interval_min >= 1440:
        url = "%s/v3/historical-candle/%s/days/1/%s/%s" % (UPSTOX_API, key, to_date, from_date)
    else:
        url = "%s/v3/historical-candle/%s/minutes/%d/%s/%s" % (UPSTOX_API, key, interval_min, to_date, from_date)
    return http_json(url, headers=upstox_headers())


def fetch_upstox_ltp(symbols, isin_map=None):
    keys = [upstox_instrument_key(s, isin_map) for s in symbols]
    url = "%s/v2/market-quote/ltp?instrument_key=%s" % (UPSTOX_API, urllib.parse.quote(",".join(keys), safe=","))
    doc = http_json(url, headers=upstox_headers())
    out = {}
    for k, v in ((doc or {}).get("data") or {}).items():
        sym = k.split(":", 1)[-1].upper()
        if v.get("last_price") is not None:
            out[sym] = float(v["last_price"])
    return out


# --- Angel One SmartAPI -----------------------------------------------------

ANGEL_API = "https://apiconnect.angelone.in"
ANGEL_SCRIP_MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
ANGEL_INTERVAL = {1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE", 10: "TEN_MINUTE",
                  15: "FIFTEEN_MINUTE", 30: "THIRTY_MINUTE", 60: "ONE_HOUR", 1440: "ONE_DAY"}


def angel_headers():
    key = os.environ.get("ANGEL_API_KEY", "").strip()
    jwt = os.environ.get("ANGEL_JWT_TOKEN", "").strip()
    if not key or not jwt:
        raise NSEError("data_feed is angelone but ANGEL_API_KEY / ANGEL_JWT_TOKEN are not set "
                       "(log in with SmartAPI yourself and export them; never paste them in the chat).")
    return {"Authorization": "Bearer " + jwt, "X-PrivateKey": key, "Content-Type": "application/json",
            "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00:00:00:00:00:00"}


def load_angel_tokens(path="trader/instruments-angelone.json", refresh=False):
    """{SYMBOL: symboltoken} for NSE -EQ scrips, cached in trader/."""
    if os.path.exists(path) and not refresh:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    rows = json.loads(http_get(ANGEL_SCRIP_MASTER).decode("utf-8"))
    out = {}
    for r in rows:
        if r.get("exch_seg") == "NSE" and str(r.get("symbol", "")).endswith("-EQ"):
            out[r["symbol"][:-3].upper()] = str(r["token"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


def bars_from_angel(doc, interval_min=5, drop_partial_at=None):
    """SmartAPI getCandleData: data = [[iso_ts, o, h, l, c, v], ...]."""
    out = []
    for row in (doc or {}).get("data") or []:
        dt = to_ist(datetime.datetime.fromisoformat(row[0]))
        if drop_partial_at is not None and interval_min < 1440 and dt + datetime.timedelta(minutes=interval_min) > drop_partial_at:
            continue
        out.append({"t": dt, "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                    "c": float(row[4]), "v": int(row[5] or 0)})
    out.sort(key=lambda b: b["t"])
    return out


def fetch_angel_candles(sym, interval_min, start, end, tokens=None):
    tok = (tokens or {}).get(norm_symbol(sym))
    if not tok:
        raise NSEError("No Angel One symboltoken for %s: refresh the instruments first." % sym)
    body = json.dumps({"exchange": "NSE", "symboltoken": tok, "interval": ANGEL_INTERVAL[interval_min],
                       "fromdate": start.strftime("%Y-%m-%d %H:%M"), "todate": end.strftime("%Y-%m-%d %H:%M")}).encode()
    return http_json(ANGEL_API + "/rest/secure/angelbroking/historical/v1/getCandleData",
                     headers=angel_headers(), data=body, method="POST")


def fetch_angel_ltp(symbols, tokens=None):
    toks = {}
    for s in symbols:
        t = (tokens or {}).get(norm_symbol(s))
        if t:
            toks[t] = norm_symbol(s)
    body = json.dumps({"mode": "LTP", "exchangeTokens": {"NSE": list(toks)}}).encode()
    doc = http_json(ANGEL_API + "/rest/secure/angelbroking/market/v1/quote/",
                    headers=angel_headers(), data=body, method="POST")
    out = {}
    for r in ((doc or {}).get("data") or {}).get("fetched") or []:
        sym = toks.get(str(r.get("symbolToken")))
        if sym and r.get("ltp") is not None:
            out[sym] = float(r["ltp"])
    return out


# --- One entry point ----------------------------------------------------------

def get_intraday_bars(sym, day, interval_min=5, feed="yahoo", now=None, cfg=None):
    """Today's (or `day`'s) completed intraday bars for one NSE symbol from the chosen feed.
    Returns (bars, source_label). Yahoo is free and delayed; Upstox / Angel One are your broker's feed."""
    now = to_ist(now) if now else now_ist()
    feed = (feed or (cfg or {}).get("data_feed") or "yahoo").lower()
    if feed == "upstox":
        isin = load_upstox_instruments()
        if day == now.date():
            doc = fetch_upstox_intraday(sym, interval_min, isin)
        else:
            doc = fetch_upstox_history(sym, interval_min, day.isoformat(), day.isoformat(), isin)
        bars = bars_from_upstox(doc, interval_min, now)
        src = "Upstox"
    elif feed in ("angelone", "angel"):
        o, c = session_bounds(day, cfg)
        doc = fetch_angel_candles(sym, interval_min, o, min(c, now), load_angel_tokens())
        bars = bars_from_angel(doc, interval_min, now)
        src = "Angel One SmartAPI"
    else:
        o, c = session_bounds(day, cfg)
        doc = fetch_yahoo_chart(sym, "%dm" % interval_min, period1=o.timestamp() - 60, period2=c.timestamp() + 60)
        bars, _ = bars_from_yahoo(doc, interval_min, now)
        src = "Yahoo Finance (free, delayed)"
    bars = [b for b in bars if b["t"].date() == day]
    return bars, src


def get_ltp(symbols, feed="yahoo"):
    """{SYMBOL: last price}. Yahoo reads each chart's meta.regularMarketPrice."""
    feed = (feed or "yahoo").lower()
    syms = [norm_symbol(s) for s in symbols]
    if feed == "upstox":
        return fetch_upstox_ltp(syms, load_upstox_instruments())
    if feed in ("angelone", "angel"):
        return fetch_angel_ltp(syms, load_angel_tokens())
    out = {}
    for s in syms:
        try:
            doc = fetch_yahoo_chart(s, "1m", "1d")
            p = doc["chart"]["result"][0]["meta"].get("regularMarketPrice")
            if p is not None:
                out[s] = float(p)
        except (NSEError, KeyError, IndexError, TypeError):
            pass
        SLEEP(PAUSE)
    return out


# ---------------------------------------------------------------------------
# NSE website session (public disclosure JSON + RSS)
# ---------------------------------------------------------------------------

NSE_WWW = "https://www.nseindia.com"
NSE_ARCHIVES = "https://nsearchives.nseindia.com"
NSE_RSS = {
    "announcements": NSE_ARCHIVES + "/content/RSS/Online_announcements.xml",
    "financial_results": NSE_ARCHIVES + "/content/RSS/Financial_Results.xml",
    "insider_trading": NSE_ARCHIVES + "/content/RSS/Insider_Trading.xml",
    "board_meetings": NSE_ARCHIVES + "/content/RSS/Board_Meetings.xml",
    "corporate_actions": NSE_ARCHIVES + "/content/RSS/Corporate_action.xml",
}
INDIA_RSS = {
    "moneycontrol_markets": "https://www.moneycontrol.com/rss/marketreports.xml",
    "moneycontrol_business": "https://www.moneycontrol.com/rss/business.xml",
    "moneycontrol_buzzing": "https://www.moneycontrol.com/rss/buzzingstocks.xml",
    "et_markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "et_stocks": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "livemint_markets": "https://www.livemint.com/rss/markets",
    "business_standard_markets": "https://www.business-standard.com/rss/markets-106.rss",
}


class NSESession:
    """A polite, cookie-carrying session for nseindia.com's public JSON endpoints.

    NSE's site sets cookies on its home page that its own JSON endpoints expect;
    this opens the home page once, like a browser tab would, then calls the API
    with a Referer. It does NOT try to defeat bot checks: a 401/403 or an HTML
    challenge page is reported as "NSE refused the request" and the caller
    should fall back to --offline files or the archive RSS feeds."""

    def __init__(self, pause=1.0):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.pause = pause
        self.warmed = False

    def _headers(self, referer=None):
        return {"User-Agent": UA_BROWSER, "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-IN,en;q=0.9", "Referer": referer or NSE_WWW + "/",
                "Accept-Encoding": "gzip"}

    def warm(self, page="/"):
        if self.warmed:
            return
        try:
            http_get(NSE_WWW + page, headers={"User-Agent": UA_BROWSER, "Accept": "text/html"}, opener=self.opener)
        except (urllib.error.URLError, OSError):
            pass
        self.warmed = True
        SLEEP(self.pause)

    def get_json(self, path, params=None, referer_page="/"):
        self.warm(referer_page)
        url = NSE_WWW + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        try:
            raw = http_get(url, headers=self._headers(NSE_WWW + referer_page), opener=self.opener)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise NSEError("NSE refused the request (HTTP %d) for %s. Try again later, or use --offline "
                               "with a file saved from your browser." % (e.code, path))
            raise NSEError("NSE request %s failed: HTTP %d" % (path, e.code))
        except (urllib.error.URLError, OSError) as e:
            raise NSEError("Could not reach nseindia.com: %s" % e)
        finally:
            SLEEP(self.pause)
        txt = raw.decode("utf-8", "replace").strip()
        if not txt or txt[0] not in "[{":
            raise NSEError("NSE answered %s with a web page, not data (it may be challenging scripted "
                           "requests). Use --offline with a file saved from your browser." % path)
        return json.loads(txt)


def nse_date(d):
    """date -> 'DD-MM-YYYY' (NSE API parameter format)."""
    return d.strftime("%d-%m-%Y")


def parse_nse_date(text):
    """NSE uses '18-Nov-2026', '18-NOV-2026 17:45:01', '18-11-2026', '2026-11-18 ...'."""
    t = str(text or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d %b %Y %H:%M:%S", "%d %b %Y"):
        try:
            return datetime.datetime.strptime(t.title() if "%b" in fmt else t, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def parse_rss(xml_text):
    """RSS 2.0 / Atom -> [{"title","link","published" (aware dt or None),"summary","source"}]."""
    items = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError as e:
        raise NSEError("Feed is not valid XML: %s" % e)
    chan_title = root.findtext("channel/title") or ""
    for it in root.iter("item"):
        items.append({"title": (it.findtext("title") or "").strip(), "link": (it.findtext("link") or "").strip(),
                      "published": _rss_date(it.findtext("pubDate")), "summary": _strip_html(it.findtext("description") or ""),
                      "source": chan_title.strip()})
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for it in root.findall(".//a:entry", ns):
        link = it.find("a:link", ns)
        items.append({"title": (it.findtext("a:title", "", ns) or "").strip(),
                      "link": link.get("href") if link is not None else "",
                      "published": _rss_date(it.findtext("a:updated", "", ns) or it.findtext("a:published", "", ns)),
                      "summary": _strip_html(it.findtext("a:summary", "", ns) or ""), "source": chan_title.strip()})
    return items


def _strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _rss_date(s):
    if not s:
        return None
    s = s.strip()
    import email.utils
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return to_ist(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        return parse_nse_date(s)


def fetch_rss(url):
    return parse_rss(http_get(url, headers={"User-Agent": UA_BROWSER, "Accept": "application/rss+xml, application/xml, text/xml"}).decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Indian intraday (MIS) equity charges
# ---------------------------------------------------------------------------

CHARGES_DEFAULT = {
    "brokerage_flat_inr": 20.0,     # per executed order, the discount-broker cap (Upstox / Angel One / Zerodha)
    "brokerage_pct": 0.03,          # or this % of the order value, whichever is LOWER
    "stt_sell_pct": 0.025,          # STT, intraday equity: sell side only
    "exchange_txn_pct": 0.00297,    # NSE transaction charge, both sides
    "sebi_fee_pct": 0.0001,         # SEBI turnover fee Rs 10 / crore, both sides
    "stamp_buy_pct": 0.003,         # stamp duty, intraday: buy side only
    "gst_pct": 18.0,                # on brokerage + exchange charge + SEBI fee
}


def charges_config(cfg=None):
    c = dict(CHARGES_DEFAULT)
    c.update(((cfg or {}).get("charges") or {}))
    return c


def intraday_charges(qty, buy_price, sell_price, cfg=None):
    """Round-trip intraday charges for a long: buy qty at buy_price, sell at sell_price.
    Returns a dict of each component (rupees, 2 dp) plus 'total'. STT is rounded to the
    rupee as NSE does on a contract note."""
    c = charges_config(cfg)
    bv = float(qty) * float(buy_price)
    sv = float(qty) * float(sell_price)

    def brok(v):
        return min(c["brokerage_flat_inr"], v * c["brokerage_pct"] / 100.0) if v > 0 else 0.0
    brokerage = brok(bv) + brok(sv)
    stt = float(round(sv * c["stt_sell_pct"] / 100.0))
    exch = (bv + sv) * c["exchange_txn_pct"] / 100.0
    sebi = (bv + sv) * c["sebi_fee_pct"] / 100.0
    stamp = bv * c["stamp_buy_pct"] / 100.0
    gst = (brokerage + exch + sebi) * c["gst_pct"] / 100.0
    parts = {"brokerage": brokerage, "stt": stt, "exchange_txn": exch, "sebi_fee": sebi, "stamp_duty": stamp, "gst": gst}
    out = {k: round(v, 2) for k, v in parts.items()}
    out["total"] = round(sum(parts.values()), 2)
    out["turnover"] = round(bv + sv, 2)
    return out


def breakeven_price(qty, buy_price, cfg=None):
    """Lowest sell price (to the tick) whose round-trip charges are covered."""
    p = float(buy_price)
    t = tick_size(p)
    sell = p
    for _ in range(100000):
        ch = intraday_charges(qty, p, sell, cfg)["total"]
        if (sell - p) * qty >= ch:
            return round_tick(sell, "up")
        sell += t
    return None


TAX_NOTE = ("Intraday equity profit is speculative business income in India, taxed at your income-tax "
            "slab rate (not as STCG). STT paid is not deductible from it. Nothing here is tax advice.")


# ---------------------------------------------------------------------------
# The ratcheting trailing stop
# ---------------------------------------------------------------------------

def trail_init(entry, initial_stop, activate_pct=0.5, trail_pct=0.6):
    """State for one long. high_water starts at the entry; the stop starts at the plan's stop."""
    entry = float(entry)
    return {"entry": entry, "initial_stop": float(initial_stop), "stop": float(initial_stop),
            "high_water": entry, "activate_pct": float(activate_pct), "trail_pct": float(trail_pct),
            "active": False, "activate_at": round_tick(entry * (1 + float(activate_pct) / 100.0), "up")}


def trail_stop_for(high_water, trail_pct):
    """trail_pct below the peak, rounded DOWN to the NSE tick (away from the price)."""
    return round_tick(float(high_water) * (1 - float(trail_pct) / 100.0), "down")


def trail_update(state, bar_high, bar_low=None, bar_close=None):
    """Advance the trailing stop by one bar. Order inside a bar is unknown, so this is
    conservative: first test the bar's low against the stop that was in force when the bar
    opened; only then raise high_water with the bar's high and ratchet the stop.
    Returns (state, event) where event is None | 'stopped' | 'activated' | 'raised'.
    The stop never moves down."""
    s = dict(state)
    if bar_low is not None and float(bar_low) <= s["stop"]:
        s["exit_price"] = s["stop"]
        return s, "stopped"
    event = None
    hi = float(bar_high)
    if hi > s["high_water"]:
        s["high_water"] = hi
    if not s["active"] and s["high_water"] >= s["entry"] * (1 + s["activate_pct"] / 100.0) - 1e-9:
        s["active"] = True
        event = "activated"
    if s["active"]:
        new = trail_stop_for(s["high_water"], s["trail_pct"])
        if new > s["stop"] + 1e-9:
            s["stop"] = new
            event = event or "raised"
    return s, event


def describe_trail(activate_pct=0.5, trail_pct=0.6):
    return ("trailing stop: once the price is up %s%% from the entry, the stop trails %s%% below the "
            "highest price since entry (high water) and only ever moves up" % (_num(activate_pct), _num(trail_pct)))


def _num(x):
    x = float(x)
    return ("%d" % x) if x == int(x) else ("%g" % x)


# ---------------------------------------------------------------------------
# Small shared text
# ---------------------------------------------------------------------------

SAFETY = ("Education and tooling, not financial advice. Paper trading by default (\"paper\": true). "
          "Nothing places an order except trader-broker, after trader-risk passes it and you say yes.")
DATA_NOTE = ("Yahoo Finance (.NS) is free, unofficial and delayed; Upstox / Angel One feeds need your own "
             "API token. Check anything that matters against your broker's terminal.")


def write_json(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False, default=_json_default)
        f.write("\n")
    os.replace(tmp, path)


def _json_default(o):
    if isinstance(o, (datetime.datetime, datetime.date, datetime.time)):
        return o.isoformat()
    raise TypeError(type(o).__name__)


def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
