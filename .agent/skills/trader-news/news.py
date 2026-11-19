#!/usr/bin/env python3
"""NSE News & Corporate Announcements Monitor (trader-news). Standard library only.

Replaces SEC EDGAR 8-Ks with:
  1. NSE Corporate Announcements: /api/corporate-announcements?index=equities&symbol=SYM
     (with fallback to NSE archive RSS Online_announcements.xml)
  2. Indian financial RSS feeds: Moneycontrol, Economic Times, LiveMint, Business Standard
  3. Yahoo Finance (.NS) headline RSS + today's intraday price move

    py news.py                                   # watchlist + plan + portfolio tickers
    py news.py --tickers RELIANCE TCS INFY --hours 24
    py news.py --no-nse --json
    py news.py --offline tests/fixtures/trader-news --now "2026-11-18 10:30"

Times print in IST. Headlines are leads, not facts; nothing here is advice.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as N

DEFAULT_WATCHLIST = "trader/watchlist.json"
DEFAULT_PLAN = "trader/plan.json"
DEFAULT_PORTFOLIO = "trader/portfolio.json"
DEFAULT_UNIVERSE = "trader/universe.txt"
DEFAULT_STATE = "trader/state/news.json"
DEFAULT_CONFIG = "trader/config.json"

YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"
KEEP_DAYS = 30
MOVE_PCT = 3.0

BUILTIN_ALIASES = {
    "RELIANCE": ("reliance industries", "ril"),
    "TCS": ("tata consultancy",),
    "HDFCBANK": ("hdfc bank",),
    "ICICIBANK": ("icici bank",),
    "INFY": ("infosys",),
    "SBIN": ("state bank of india", "sbi"),
    "BHARTIARTL": ("bharti airtel", "airtel"),
    "LT": ("larsen & toubro", "larsen and toubro", "l&t"),
    "TATAMOTORS": ("tata motors",),
    "M&M": ("mahindra & mahindra", "mahindra and mahindra"),
    "BAJFINANCE": ("bajaj finance",),
    "HINDUNILVR": ("hindustan unilever", "hul"),
    "KOTAKBANK": ("kotak mahindra bank", "kotak bank"),
    "AXISBANK": ("axis bank",),
    "ASIANPAINT": ("asian paints",),
    "MARUTI": ("maruti suzuki",),
    "SUNPHARMA": ("sun pharma", "sun pharmaceutical"),
    "ADANIENT": ("adani enterprises",),
    "WIPRO": ("wipro",),
    "TATASTEEL": ("tata steel",),
}

KEYWORDS = [
    ("results", r"\b(?:financial\s+)?results?\b|\bquarterly\b|\bQ[1-4]\b|\bPAT\b|\bEBITDA\b|\bearnings\b"),
    ("board_meeting", r"\bboard\s+meeting\b|\bintimation\b"),
    ("dividend", r"\bdividend\b"),
    ("bonus_split", r"\bbonus\b|\bstock\s+split\b|\bsub-division\b"),
    ("order_win", r"\border\s+win\b|\baward(?:ed)?\b|\bcontract\b|\bLOA\b|\bLoI\b"),
    ("bulk_block_deal", r"\bbulk\s+deal\b|\bblock\s+deal\b|\bstake\s+(?:sale|buy|purchase)\b"),
    ("rating", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|\bbuy\s+rating\b|\btarget\s+price\b|\bCRISIL\b|\bICRA\b"),
    ("sebi_regulatory", r"\bSEBI\b|\bRBI\b|\bshow\s+cause\b|\bpenalty\b|\bcompliance\b|\bSAT\b"),
    ("pledge", r"\bpledge\b|\bencumbrance\b|\binvocation\b"),
    ("fund_raise", r"\bQIP\b|\bfund\s+rais|\bpreferential\b|\brights\s+issue\b|\bNCD\b|\bdebenture\b"),
    ("management", r"\bCEO\b|\bCFO\b|\bMD\b|\bresign|\bappoint(?:ed|ment)\b|\bKMP\b"),
    ("m_and_a", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bdemerger\b|\bamalgamation\b"),
]


class NewsError(N.NSEError):
    pass


def tag_text(text):
    t = text or ""
    tags = []
    for name, pat in KEYWORDS:
        if re.search(pat, t, re.IGNORECASE):
            tags.append(name)
    return tags


def load_aliases(universe_path=DEFAULT_UNIVERSE):
    aliases = {k: list(v) for k, v in BUILTIN_ALIASES.items()}
    if universe_path and os.path.exists(universe_path):
        with open(universe_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) >= 2:
                    sym = parts[0].strip().upper()
                    name = parts[1].strip().lower()
                    short = re.sub(r"\b(limited|ltd\.?|industries|india)\b", "", name).strip()
                    vals = aliases.setdefault(sym, [])
                    for cand in (name, short):
                        if len(cand) >= 4 and cand not in vals:
                            vals.append(cand)
    return aliases


def match_tickers(text, tickers, aliases):
    low = (text or "").lower()
    up = (text or "").upper()
    hits = []
    for sym in tickers:
        if re.search(r"(?<![A-Z0-9])" + re.escape(sym) + r"(?![A-Z0-9])", up):
            hits.append(sym)
            continue
        for al in aliases.get(sym, ()):
            if al in low:
                hits.append(sym)
                break
    return hits


def _item_id(item):
    raw = "%s|%s|%s" % (item.get("source", ""), item.get("title", ""), item.get("link", ""))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def fetch_nse_announcements(tickers, aliases, offline=None):
    items = []
    notes = []
    if offline:
        for sym in tickers:
            p = os.path.join(offline, "nse-ann-%s.json" % sym)
            if os.path.exists(p):
                rows = json.loads(N.read_maybe_gz(p))
                if isinstance(rows, dict):
                    rows = rows.get("data") or []
                for r in rows:
                    dt = N.parse_nse_date(r.get("an_dt") or r.get("sort_date") or "")
                    desc = str(r.get("desc") or "").strip()
                    txt = str(r.get("attchmntText") or "").strip()
                    title = "%s: %s" % (sym, desc) if desc else ("%s announcement" % sym)
                    items.append({
                        "source": "NSE Corporate Announcement",
                        "tickers": [sym],
                        "title": title,
                        "summary": txt,
                        "link": str(r.get("attchmntFile") or ""),
                        "published": dt,
                        "tags": tag_text(desc + " " + txt),
                        "is_filing": True,
                    })
        rss_p = os.path.join(offline, "nse-announcements.xml")
        if os.path.exists(rss_p):
            for it in N.parse_rss(N.read_maybe_gz(rss_p)):
                matched = match_tickers(it["title"] + " " + it["summary"], tickers, aliases)
                if matched:
                    items.append({
                        "source": "NSE Announcements RSS",
                        "tickers": matched,
                        "title": it["title"],
                        "summary": it["summary"],
                        "link": it["link"],
                        "published": it["published"],
                        "tags": tag_text(it["title"] + " " + it["summary"]),
                        "is_filing": True,
                    })
        return items, notes

    sess = N.NSESession()
    nse_failed = False
    for sym in tickers:
        try:
            rows = sess.get_json("/api/corporate-announcements", params={"index": "equities", "symbol": sym},
                                 referer_page="/companies-listing/corporate-filings-announcements")
            for r in (rows if isinstance(rows, list) else rows.get("data", [])):
                dt = N.parse_nse_date(r.get("an_dt") or r.get("sort_date") or "")
                desc = str(r.get("desc") or "").strip()
                txt = str(r.get("attchmntText") or "").strip()
                items.append({
                    "source": "NSE Corporate Announcement",
                    "tickers": [sym],
                    "title": "%s: %s" % (sym, desc or "Filing"),
                    "summary": txt,
                    "link": str(r.get("attchmntFile") or ""),
                    "published": dt,
                    "tags": tag_text(desc + " " + txt),
                    "is_filing": True,
                })
        except N.NSEError as e:
            notes.append(str(e))
            nse_failed = True
            break

    if nse_failed:
        try:
            for it in N.fetch_rss(N.NSE_RSS["announcements"]):
                matched = match_tickers(it["title"] + " " + it["summary"], tickers, aliases)
                if matched:
                    items.append({
                        "source": "NSE Announcements RSS",
                        "tickers": matched,
                        "title": it["title"],
                        "summary": it["summary"],
                        "link": it["link"],
                        "published": it["published"],
                        "tags": tag_text(it["title"] + " " + it["summary"]),
                        "is_filing": True,
                    })
        except Exception as re_err:
            notes.append("NSE archive RSS fallback failed: %s" % re_err)
    return items, notes


def fetch_india_news(tickers, aliases, offline=None):
    items = []
    notes = []
    if offline:
        for name in os.listdir(offline):
            if name.startswith("rss-") and name.endswith(".xml"):
                for it in N.parse_rss(N.read_maybe_gz(os.path.join(offline, name))):
                    matched = match_tickers(it["title"] + " " + it["summary"], tickers, aliases)
                    if matched:
                        items.append({
                            "source": it.get("source") or name,
                            "tickers": matched,
                            "title": it["title"],
                            "summary": it["summary"],
                            "link": it["link"],
                            "published": it["published"],
                            "tags": tag_text(it["title"] + " " + it["summary"]),
                            "is_filing": False,
                        })
            elif name.startswith("yahoo-") and name.endswith(".xml"):
                sym = name[6:-4].upper()
                if sym in tickers:
                    for it in N.parse_rss(N.read_maybe_gz(os.path.join(offline, name))):
                        items.append({
                            "source": "Yahoo Finance India",
                            "tickers": [sym],
                            "title": it["title"],
                            "summary": it["summary"],
                            "link": it["link"],
                            "published": it["published"],
                            "tags": tag_text(it["title"] + " " + it["summary"]),
                            "is_filing": False,
                        })
        return items, notes

    for feed_name, url in N.INDIA_RSS.items():
        try:
            for it in N.fetch_rss(url):
                matched = match_tickers(it["title"] + " " + it["summary"], tickers, aliases)
                if matched:
                    items.append({
                        "source": it.get("source") or feed_name,
                        "tickers": matched,
                        "title": it["title"],
                        "summary": it["summary"],
                        "link": it["link"],
                        "published": it["published"],
                        "tags": tag_text(it["title"] + " " + it["summary"]),
                        "is_filing": False,
                    })
        except Exception as e:
            notes.append("%s: %s" % (feed_name, e))

    for sym in tickers:
        try:
            url = YAHOO_RSS % urllib.parse.quote(N.yahoo_symbol(sym))
            for it in N.fetch_rss(url):
                items.append({
                    "source": "Yahoo Finance (.NS)",
                    "tickers": [sym],
                    "title": it["title"],
                    "summary": it["summary"],
                    "link": it["link"],
                    "published": it["published"],
                    "tags": tag_text(it["title"] + " " + it["summary"]),
                    "is_filing": False,
                })
        except Exception:
            pass
    return items, notes


def fetch_moves(tickers, now, cfg, offline=None):
    moves = {}
    for sym in tickers:
        try:
            if offline:
                p = os.path.join(offline, "chart-%s.json" % sym)
                if not os.path.exists(p):
                    continue
                doc = json.loads(N.read_maybe_gz(p))
            else:
                doc = N.fetch_yahoo_chart(sym, "5m", "1d")
            res = doc["chart"]["result"][0]
            meta = res.get("meta") or {}
            px = meta.get("regularMarketPrice")
            pc = meta.get("previousClose") or meta.get("chartPreviousClose")
            if px and pc:
                chg_pct = round((float(px) - float(pc)) / float(pc) * 100.0, 2)
                moves[sym] = {"price": float(px), "prev_close": float(pc), "chg_pct": chg_pct,
                              "moving": abs(chg_pct) >= MOVE_PCT}
        except Exception:
            pass
    return moves


def _default_tickers():
    syms = []
    wl = N.read_json(DEFAULT_WATCHLIST)
    if wl:
        syms.extend(h["symbol"] for h in wl.get("hits", []) if h.get("symbol"))
    pl = N.read_json(DEFAULT_PLAN)
    if pl and pl.get("symbol"):
        syms.append(pl["symbol"])
    pf = N.read_json(DEFAULT_PORTFOLIO)
    if pf:
        syms.extend((pf.get("positions") or {}).keys())
    return list(dict.fromkeys(N.norm_symbol(s) for s in syms))


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE News & Corporate Announcements Monitor (trader-news)")
    p.add_argument("--tickers", nargs="+")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--all", action="store_true", help="Show seen items as well")
    p.add_argument("--dry-run", action="store_true", help="Do not update seen state")
    p.add_argument("--no-nse", action="store_true", help="Skip NSE announcements, RSS/headlines only")
    p.add_argument("--no-moves", action="store_true", help="Skip intraday price moves")
    p.add_argument("--offline", help="Folder of saved fixtures")
    p.add_argument("--now", help="Pin IST clock")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    try:
        cfg = N.load_config(DEFAULT_CONFIG)
        now = N.parse_now(args.now) if args.now else N.now_ist()
        tickers = [N.norm_symbol(t) for t in args.tickers] if args.tickers else _default_tickers()
        if not tickers:
            raise NewsError("No tickers given and watchlist/plan/portfolio are empty.")

        aliases = load_aliases(DEFAULT_UNIVERSE)
        items = []
        notes = []
        if not args.no_nse:
            a_items, a_notes = fetch_nse_announcements(tickers, aliases, offline=args.offline)
            items.extend(a_items)
            notes.extend(a_notes)
        n_items, n_notes = fetch_india_news(tickers, aliases, offline=args.offline)
        items.extend(n_items)
        notes.extend(n_notes)

        cutoff_dt = now - datetime.timedelta(hours=float(args.hours))
        state = N.read_json(DEFAULT_STATE, {"seen": {}}) or {"seen": {}}
        seen = dict(state.get("seen") or {})

        filtered = []
        for it in items:
            pub = it.get("published")
            if pub is not None and pub < cutoff_dt:
                continue
            iid = _item_id(it)
            if not args.all and iid in seen:
                continue
            it["id"] = iid
            filtered.append(it)

        filtered.sort(key=lambda x: x["published"] or datetime.datetime.min.replace(tzinfo=N.IST), reverse=True)
        moves = {} if args.no_moves else fetch_moves(tickers, now, cfg, offline=args.offline)

        # Results / Board meeting warning
        event_alerts = [it for it in filtered if "results" in it.get("tags", []) or "board_meeting" in it.get("tags", [])]

        if not args.dry_run:
            keep_cutoff = (now - datetime.timedelta(days=KEEP_DAYS)).isoformat()
            for k in list(seen):
                if seen[k] < keep_cutoff:
                    del seen[k]
            for it in filtered:
                seen[it["id"]] = now.isoformat()
            N.write_json(DEFAULT_STATE, {"seen": seen, "updated_at": now.isoformat()})

        out = {
            "as_of": now.isoformat(),
            "tickers": tickers,
            "count": len(filtered),
            "moves": moves,
            "event_warnings": [{"tickers": x["tickers"], "title": x["title"], "tags": x["tags"]} for x in event_alerts],
            "items": [{**it, "published": it["published"].isoformat() if it["published"] else None} for it in filtered],
            "notes": notes,
        }
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            for sym in tickers:
                m = moves.get(sym)
                if m:
                    flag = "  MOVING" if m["moving"] else ""
                    print("%s  %s  (%+.2f%% vs prev close %s)%s"
                          % (sym, N.inr(m["price"]), m["chg_pct"], N.inr(m["prev_close"]), flag))
            if event_alerts:
                print("WARNING: %d results / board-meeting disclosure(s) in window (gap risk on overnight holds)." % len(event_alerts))
            for it in filtered[:30]:
                t_str = N.fmt_time(it["published"]) if it["published"] else "time-unknown"
                tags = (" [%s]" % ",".join(it["tags"])) if it["tags"] else ""
                mark = "! " if it.get("is_filing") else "  "
                print("%s%s  %-10s %-26s %s%s" % (mark, t_str, ",".join(it["tickers"]), it["source"][:26], it["title"], tags))
            for n in notes:
                print("Note: " + n)
        return 0 if filtered else 1
    except N.NSEError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
