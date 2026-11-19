#!/usr/bin/env python3
"""NSE Bulk/Block Deals & Promoter Insider Buying Tracker (trader-pelosi). Standard library only.

Replaces US House PTR filings with NSE public disclosure endpoints:
  - Large deals snapshot:  /api/snapshot-capital-market-largedeal
  - Historical bulk/block: /api/historical/bulk-deals, /api/historical/block-deals
  - SEBI PIT Reg 7:        /api/corporates-pit?index=equities&from_date=...&to_date=...
  - Archive RSS fallback:  https://nsearchives.nseindia.com/content/RSS/Insider_Trading.xml

    py deals.py deals --days 7
    py deals.py deals --bulk --symbol RELIANCE --write
    py deals.py insider --promoters-only --buys-only --min-value-cr 0.5
    py deals.py watch --offline tests/fixtures/trader-pelosi/snapshot.json

Public disclosures only: no inside information, not a buy signal.
"""

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as N

DEFAULT_DEALS_OUT = "trader/deals.json"
DEFAULT_WATCHLIST = "trader/watchlist.json"
DEFAULT_PORTFOLIO = "trader/portfolio.json"


class DealsError(N.NSEError):
    pass


def _num(val):
    if val is None or val == "" or val == "-":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = re.sub(r"[,₹\s]", "", str(val))
    try:
        return float(s)
    except ValueError:
        return 0.0


def normalize_deal_row(r, deal_type="bulk"):
    """Tolerant normalizer for NSE bulk/block JSON rows."""
    sym_raw = r.get("BD_SYMBOL") or r.get("symbol") or r.get("SYMBOL") or ""
    try:
        sym = N.norm_symbol(sym_raw) if sym_raw else ""
    except N.NSEError:
        sym = str(sym_raw).upper()
    dt_raw = r.get("BD_DT_DATE") or r.get("date") or r.get("mTIMESTAMP") or ""
    dt_obj = N.parse_nse_date(dt_raw)
    date_iso = dt_obj.date().isoformat() if dt_obj else str(dt_raw)
    side_raw = str(r.get("BD_BUY_SELL") or r.get("buySell") or r.get("dealType") or "").strip().upper()
    side = "BUY" if side_raw.startswith("B") else ("SELL" if side_raw.startswith("S") else side_raw)
    qty = int(_num(r.get("BD_QTY_TRD") or r.get("qty") or r.get("quantity") or 0))
    watp = _num(r.get("BD_TP_WATP") or r.get("watp") or r.get("price") or r.get("tradePrice") or 0)
    val_cr = round((qty * watp) / 1e7, 2) if (qty and watp) else round(_num(r.get("value_cr")), 2)
    return {
        "deal_type": deal_type,
        "date": date_iso,
        "symbol": sym,
        "company": str(r.get("BD_SCRIP_NAME") or r.get("scripName") or r.get("company") or sym).strip(),
        "client": str(r.get("BD_CLIENT_NAME") or r.get("clientName") or "").strip(),
        "side": side,
        "qty": qty,
        "price": round(watp, 2),
        "value_cr": val_cr,
    }


def normalize_pit_row(r):
    """Tolerant normalizer for SEBI PIT Reg 7 (/api/corporates-pit) rows."""
    sym_raw = r.get("symbol") or r.get("SYMBOL") or ""
    try:
        sym = N.norm_symbol(sym_raw) if sym_raw else ""
    except N.NSEError:
        sym = str(sym_raw).upper()
    cat = str(r.get("personCategory") or r.get("category") or "").strip()
    txn = str(r.get("tdpTransactionType") or r.get("transactionType") or "").strip()
    mode = str(r.get("acqMode") or r.get("modeOfAcq") or "").strip()
    sec_acq = int(_num(r.get("secAcq") or r.get("qty") or 0))
    sec_val = _num(r.get("secVal") or r.get("value") or 0)
    val_cr = round(sec_val / 1e7, 2)
    dt_raw = r.get("date") or r.get("intimDt") or r.get("acqfromDt") or r.get("acqtoDt") or ""
    dt_obj = N.parse_nse_date(dt_raw)
    date_iso = dt_obj.date().isoformat() if dt_obj else str(dt_raw)

    is_promoter = "promoter" in cat.lower()
    is_buy = txn.lower() in ("buy", "purchase", "acquisition") or "buy" in txn.lower()
    is_mkt = "market purchase" in mode.lower() or "open market" in mode.lower()
    return {
        "deal_type": "insider_pit",
        "date": date_iso,
        "symbol": sym,
        "company": str(r.get("company") or sym).strip(),
        "acquirer": str(r.get("acqName") or "").strip(),
        "category": cat,
        "txn_type": txn,
        "acq_mode": mode,
        "sec_type": str(r.get("secType") or "Equity Shares").strip(),
        "qty": sec_acq,
        "value_inr": round(sec_val, 2),
        "value_cr": val_cr,
        "is_promoter": is_promoter,
        "is_buy": is_buy,
        "is_promoter_market_buy": bool(is_promoter and is_buy and (is_mkt or mode == "")),
        "disclosed_at": str(r.get("date") or r.get("intimDt") or "").strip(),
    }


def _pit_from_rss(rss_items):
    """Fallback normalizer from N.NSE_RSS['insider_trading'] XML items."""
    out = []
    for it in rss_items:
        title = it.get("title") or ""
        sym_m = re.match(r"^\s*([A-Z0-9&\-]+)\s*[-:]", title.upper())
        sym = sym_m.group(1) if sym_m else ""
        txt = title + " " + (it.get("summary") or "")
        is_prom = "promoter" in txt.lower()
        is_buy = any(w in txt.lower() for w in ("buy", "purchase", "acquir"))
        pub = it.get("published")
        out.append({
            "deal_type": "insider_rss",
            "date": pub.date().isoformat() if pub else "",
            "symbol": sym,
            "company": title,
            "acquirer": "",
            "category": "Promoter" if is_prom else "",
            "txn_type": "Buy" if is_buy else "",
            "acq_mode": "Market Purchase" if "market purchase" in txt.lower() else "",
            "qty": 0,
            "value_inr": 0.0,
            "value_cr": 0.0,
            "is_promoter": is_prom,
            "is_buy": is_buy,
            "is_promoter_market_buy": bool(is_prom and is_buy),
            "summary": it.get("summary") or title,
            "link": it.get("link") or "",
        })
    return out


def fetch_deals(days=7, bulk=True, block=True, symbol=None, offline=None, now=None):
    now = N.to_ist(now) if now else N.now_ist()
    to_d = now.date()
    from_d = to_d - datetime.timedelta(days=max(0, int(days)))
    rows = []
    notes = []

    if offline:
        doc = json.loads(N.read_maybe_gz(offline))
        if isinstance(doc, dict):
            if bulk:
                for r in doc.get("BULK_DEALS_DATA") or doc.get("bulk") or doc.get("data") or []:
                    rows.append(normalize_deal_row(r, "bulk"))
            if block:
                for r in doc.get("BLOCK_DEALS_DATA") or doc.get("block") or []:
                    rows.append(normalize_deal_row(r, "block"))
        elif isinstance(doc, list):
            for r in doc:
                rows.append(normalize_deal_row(r, r.get("deal_type", "bulk")))
    else:
        sess = N.NSESession()
        if days <= 1:
            try:
                snap = sess.get_json("/api/snapshot-capital-market-largedeal", referer_page="/market-data/large-deals")
                if bulk:
                    for r in snap.get("BULK_DEALS_DATA") or []:
                        rows.append(normalize_deal_row(r, "bulk"))
                if block:
                    for r in snap.get("BLOCK_DEALS_DATA") or []:
                        rows.append(normalize_deal_row(r, "block"))
            except N.NSEError as e:
                notes.append(str(e))
        else:
            params = {"from": N.nse_date(from_d), "to": N.nse_date(to_d)}
            if bulk:
                try:
                    bdoc = sess.get_json("/api/historical/bulk-deals", params=params, referer_page="/report-detail/display-bulk-and-block-deals")
                    for r in (bdoc.get("data") if isinstance(bdoc, dict) else bdoc) or []:
                        rows.append(normalize_deal_row(r, "bulk"))
                except N.NSEError as e:
                    notes.append(str(e))
            if block:
                try:
                    kdoc = sess.get_json("/api/historical/block-deals", params=params, referer_page="/report-detail/display-bulk-and-block-deals")
                    for r in (kdoc.get("data") if isinstance(kdoc, dict) else kdoc) or []:
                        rows.append(normalize_deal_row(r, "block"))
                except N.NSEError as e:
                    notes.append(str(e))

    if symbol:
        target_sym = N.norm_symbol(symbol)
        rows = [r for r in rows if r["symbol"] == target_sym]
    return rows, notes


def fetch_insider(days=7, symbol=None, promoters_only=False, buys_only=False, min_value_cr=0.0, offline=None, now=None):
    now = N.to_ist(now) if now else N.now_ist()
    to_d = now.date()
    from_d = to_d - datetime.timedelta(days=max(0, int(days)))
    rows = []
    notes = []

    if offline:
        raw = N.read_maybe_gz(offline)
        if raw.lstrip().startswith("<"):
            rows = _pit_from_rss(N.parse_rss(raw))
        else:
            doc = json.loads(raw)
            items = doc.get("data") if isinstance(doc, dict) else doc
            for r in items or []:
                rows.append(normalize_pit_row(r))
    else:
        sess = N.NSESession()
        params = {"index": "equities", "from_date": N.nse_date(from_d), "to_date": N.nse_date(to_d)}
        if symbol:
            params["symbol"] = N.norm_symbol(symbol)
        try:
            doc = sess.get_json("/api/corporates-pit", params=params, referer_page="/companies-listing/corporate-filings-insider-trading")
            items = doc.get("data") if isinstance(doc, dict) else doc
            for r in items or []:
                rows.append(normalize_pit_row(r))
        except N.NSEError as e:
            notes.append(str(e))
            try:
                rss_items = N.fetch_rss(N.NSE_RSS["insider_trading"])
                rows = _pit_from_rss(rss_items)
                notes.append("Fell back to NSE archive RSS Insider_Trading.xml (%d item(s))." % len(rows))
            except Exception as re_err:
                notes.append("RSS fallback also failed: %s" % re_err)

    target_sym = N.norm_symbol(symbol) if symbol else None
    out = []
    for r in rows:
        if target_sym and r["symbol"] != target_sym:
            continue
        if promoters_only and not r.get("is_promoter"):
            continue
        if buys_only and not r.get("is_buy"):
            continue
        if float(min_value_cr or 0) > 0 and r.get("value_cr", 0.0) < float(min_value_cr):
            continue
        out.append(r)
    return out, notes


def aggregate_by_symbol(rows):
    agg = {}
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        a = agg.setdefault(sym, {"symbol": sym, "buy_cr": 0.0, "sell_cr": 0.0, "net_value_cr": 0.0, "count": 0, "promoter_buys": 0})
        a["count"] += 1
        v = float(r.get("value_cr") or 0.0)
        is_buy = r.get("side") == "BUY" or r.get("is_buy")
        is_sell = r.get("side") == "SELL" or (r.get("deal_type") == "insider_pit" and not r.get("is_buy"))
        if is_buy:
            a["buy_cr"] = round(a["buy_cr"] + v, 2)
        elif is_sell:
            a["sell_cr"] = round(a["sell_cr"] + v, 2)
        a["net_value_cr"] = round(a["buy_cr"] - a["sell_cr"], 2)
        if r.get("is_promoter_market_buy"):
            a["promoter_buys"] += 1
    return sorted(agg.values(), key=lambda x: abs(x["net_value_cr"]), reverse=True)


def cross_check_watchlist(rows, watchlist_path=DEFAULT_WATCHLIST, portfolio_path=DEFAULT_PORTFOLIO):
    watch_syms = set()
    wl = N.read_json(watchlist_path)
    if wl:
        for h in wl.get("hits", []):
            if h.get("symbol"):
                watch_syms.add(h["symbol"].upper())
    pf = N.read_json(portfolio_path)
    if pf:
        for s in (pf.get("positions") or {}):
            watch_syms.add(s.upper())
    return [r for r in rows if r.get("symbol") in watch_syms], sorted(watch_syms)


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Bulk/Block Deals & Promoter Insider Tracker (trader-pelosi)")
    sub = p.add_subparsers(dest="cmd")

    def add_common(sp):
        sp.add_argument("--days", type=int, default=7)
        sp.add_argument("--symbol")
        sp.add_argument("--offline", help="Saved NSE JSON or RSS XML file")
        sp.add_argument("--now")
        sp.add_argument("--write", action="store_true", help="Write trader/deals.json")
        sp.add_argument("--json", action="store_true")

    sd = sub.add_parser("deals", aliases=["new"], help="Bulk and block deals")
    sd.add_argument("--bulk", action="store_true")
    sd.add_argument("--block", action="store_true")
    add_common(sd)

    si = sub.add_parser("insider", aliases=["index"], help="SEBI PIT promoter/insider disclosures")
    si.add_argument("--promoters-only", action="store_true")
    si.add_argument("--buys-only", action="store_true")
    si.add_argument("--min-value-cr", type=float, default=0.0)
    add_common(si)

    sw = sub.add_parser("watch", help="Cross-check deals/insider against watchlist & portfolio")
    sw.add_argument("--watchlist", default=DEFAULT_WATCHLIST)
    sw.add_argument("--portfolio", default=DEFAULT_PORTFOLIO)
    add_common(sw)

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 1
    try:
        now = N.parse_now(args.now) if args.now else N.now_ist()
        if args.cmd in ("deals", "new"):
            want_bulk = args.bulk or not args.block
            want_block = args.block or not args.bulk
            rows, notes = fetch_deals(args.days, bulk=want_bulk, block=want_block,
                                      symbol=args.symbol, offline=args.offline, now=now)
        elif args.cmd in ("insider", "index"):
            rows, notes = fetch_insider(args.days, symbol=args.symbol, promoters_only=args.promoters_only,
                                        buys_only=args.buys_only, min_value_cr=args.min_value_cr,
                                        offline=args.offline, now=now)
        else:
            d_rows, d_notes = fetch_deals(args.days, offline=args.offline, now=now)
            i_rows, i_notes = fetch_insider(args.days, offline=args.offline, now=now)
            notes = d_notes + i_notes
            hits, watch_syms = cross_check_watchlist(d_rows + i_rows, args.watchlist, args.portfolio)
            out = {"as_of": now.isoformat(), "watched": watch_syms, "hits": hits, "summary": aggregate_by_symbol(hits), "notes": notes}
            if args.write:
                N.write_json(DEFAULT_DEALS_OUT, out)
            if args.json:
                print(json.dumps(out, indent=2))
            else:
                print("Cross-checked %d deal/insider row(s) against %d watched ticker(s): %d match(es)."
                      % (len(d_rows) + len(i_rows), len(watch_syms), len(hits)))
                for r in hits[:30]:
                    print("  %-12s %-12s %s  %s cr" % (r["symbol"], r["deal_type"], r["date"], r.get("value_cr", 0)))
                for n in notes:
                    print("Note: " + n)
            return 0 if hits or not notes else 1

        agg = aggregate_by_symbol(rows)
        out = {
            "as_of": now.isoformat(), "cmd": args.cmd, "count": len(rows),
            "rows": rows, "by_symbol": agg, "notes": notes,
            "disclaimer": "Public exchange disclosures only; not inside information and not a buy signal.",
        }
        if args.write:
            N.write_json(DEFAULT_DEALS_OUT, out)
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            for n in notes:
                print("Note: " + n)
            print("%d disclosure row(s) across %d symbol(s)." % (len(rows), len(agg)))
            for r in rows[:30]:
                if r["deal_type"] in ("bulk", "block"):
                    print("  %s  %-10s %-5s %-4s %10d @ %-8s (%6.2f cr)  %s"
                          % (r["date"], r["symbol"], r["deal_type"].upper(), r["side"], r["qty"],
                             N.inr(r["price"]), r["value_cr"], r["client"][:32]))
                else:
                    flag = " [PROMOTER MKT BUY]" if r.get("is_promoter_market_buy") else ""
                    print("  %s  %-10s %-18s %-6s %10d (%6.2f cr)  %s%s"
                          % (r["date"], r["symbol"], r.get("category", "")[:18], r.get("txn_type", ""),
                             r.get("qty", 0), r.get("value_cr", 0), r.get("acquirer", "")[:24], flag))
        return 0 if rows or not notes else 1
    except N.NSEError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
