#!/usr/bin/env python3
"""NSE Quarterly Results Analyst for trader-analyst (NSE India edition). Standard library only.

Replaces SEC XBRL companyfacts with NSE quarterly financial results:
  - /api/corporates-financial-results?index=equities&symbol=SYM&period=Quarterly
  - Optional Ind-AS XBRL XML parsing (nsearchives XBRL attachments or --offline)
  - Documented manual CSV fallback (--csv FILE) when NSE blocks automated requests

    py facts.py RELIANCE
    py facts.py RELIANCE TCS HDFCBANK --quarters 8 --metrics trader/metrics.json
    py facts.py RELIANCE --offline tests/fixtures/trader-analyst --json

Numbers are reported in ₹ crore (unless EPS in ₹). Never says a stock is a buy.
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as N

DEFAULT_METRICS = "trader/metrics.json"
DEFAULT_CONFIG = "trader/config.json"


class AnalystError(N.NSEError):
    pass


def _num(val):
    if val is None or val in ("", "-", "NA", "null"):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = re.sub(r"[,₹\s]", "", str(val))
    try:
        return float(s)
    except ValueError:
        return None


def _to_cr(val, unit_hint="lakhs"):
    """Convert raw financial value to INR crore (1 crore = 1e7 INR = 100 lakhs)."""
    if val is None:
        return None
    v = float(val)
    u = str(unit_hint or "").lower()
    if "crore" in u or u == "cr":
        return round(v, 2)
    if "lakh" in u:
        return round(v / 100.0, 2)
    if abs(v) >= 1e7:
        return round(v / 1e7, 2)
    return round(v, 2)


def parse_indas_xbrl(xml_text):
    """Extract quarterly revenue_cr, operating_profit_cr, net_profit_cr, eps_basic, eps_diluted
    from an NSE/BSE Ind-AS XBRL instance document."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)

    # Identify monetary scale from decimals/unit or default raw INR -> crore
    facts = {}
    for el in root.iter():
        tag = el.tag.split("}", 1)[-1]
        if el.text is None or not el.text.strip():
            continue
        v = _num(el.text)
        if v is None:
            continue
        facts.setdefault(tag, []).append((v, el.attrib))

    def _first(names, is_eps=False):
        for nm in names:
            if nm in facts and facts[nm]:
                val, attrs = facts[nm][0]
                if is_eps:
                    return round(val, 2)
                decimals = attrs.get("decimals", "")
                unit_ref = str(attrs.get("unitRef", "")).lower()
                if "inr" in unit_ref or abs(val) >= 1e6 or decimals in ("-5", "-6", "-7", "0", "2"):
                    return round(val / 1e7, 2) if abs(val) >= 1e5 else round(val, 2)
                return round(val / 100.0, 2)
        return None

    rev = _first(["RevenueFromOperations", "IncomeFromOperations", "TotalRevenueFromOperations", "TotalIncome"])
    pbt = _first(["ProfitBeforeTax", "ProfitLossBeforeTax", "OperatingProfit"])
    pat = _first(["ProfitLossForPeriod", "ProfitLossForThePeriod", "ProfitLossFromContinuingOperations", "NetProfit"])
    eps_b = _first(["BasicEarningsLossPerShareFromContinuingOperations", "BasicEarningsPerShare"], is_eps=True)
    eps_d = _first(["DilutedEarningsLossPerShareFromContinuingOperations", "DilutedEarningsPerShare",
                    "BasicEarningsLossPerShareFromContinuingOperations"], is_eps=True)

    # Quarter end date from contexts if available
    end_date = None
    for el in root.iter():
        if el.tag.split("}", 1)[-1] == "endDate" and el.text:
            end_date = el.text.strip()
            break

    return {
        "to_date": end_date or "",
        "revenue_cr": rev,
        "operating_profit_cr": pbt,
        "net_profit_cr": pat,
        "eps_basic": eps_b,
        "eps_diluted": eps_d,
    }


def parse_nse_results_json(rows):
    """Normalize rows from /api/corporates-financial-results (tolerant field names)."""
    out = []
    for r in rows or []:
        to_raw = r.get("toDate") or r.get("reDate") or r.get("periodEnd") or ""
        dt = N.parse_nse_date(to_raw)
        to_iso = dt.date().isoformat() if dt else str(to_raw)
        rev = _num(r.get("totalIncome") or r.get("income") or r.get("revenue_cr") or r.get("reTotalIncome"))
        pbt = _num(r.get("reProLossBefTax") or r.get("pbt") or r.get("operating_profit_cr"))
        pat = _num(r.get("netProfit") or r.get("reNetProLoss") or r.get("net_profit_cr") or r.get("profitAfterTax"))
        eps = _num(r.get("eps") or r.get("reDilutedEps") or r.get("eps_diluted") or r.get("reBasicEps"))
        unit = r.get("unit") or ("cr" if "revenue_cr" in r else "lakhs")
        rev_cr = _to_cr(rev, unit) if rev is not None else None
        pbt_cr = _to_cr(pbt, unit) if pbt is not None else None
        pat_cr = _to_cr(pat, unit) if pat is not None else None
        cons = str(r.get("consolidated") or "").strip().lower()
        is_cons = cons in ("consolidated", "true", "yes", "c") or "cons" in cons
        audited = str(r.get("audited") or "").strip()
        out.append({
            "to_date": to_iso,
            "from_date": str(r.get("fromDate") or ""),
            "consolidated": is_cons,
            "audited": audited,
            "revenue_cr": rev_cr,
            "operating_profit_cr": pbt_cr,
            "net_profit_cr": pat_cr,
            "eps_basic": round(eps, 2) if eps is not None else None,
            "eps_diluted": round(eps, 2) if eps is not None else None,
            "xbrl_url": str(r.get("xbrl") or ""),
        })
    # Prefer consolidated if mixed for the same date, deduplicate by to_date
    by_date = {}
    for row in out:
        d = row["to_date"]
        if not d:
            continue
        if d not in by_date or (row["consolidated"] and not by_date[d]["consolidated"]):
            by_date[d] = row
    ordered = sorted(by_date.values(), key=lambda x: x["to_date"], reverse=True)
    return ordered


def parse_results_csv(csv_text):
    """Manual CSV fallback: columns date,revenue_cr,operating_profit_cr,net_profit_cr,eps_diluted,consolidated"""
    out = []
    for r in csv.DictReader(io.StringIO(csv_text)):
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        out.append({
            "to_date": r.get("date") or r.get("to_date") or "",
            "consolidated": r.get("consolidated", "true").lower() in ("true", "yes", "1"),
            "audited": r.get("audited", ""),
            "revenue_cr": _num(r.get("revenue_cr")),
            "operating_profit_cr": _num(r.get("operating_profit_cr")),
            "net_profit_cr": _num(r.get("net_profit_cr")),
            "eps_basic": _num(r.get("eps_basic") or r.get("eps_diluted")),
            "eps_diluted": _num(r.get("eps_diluted") or r.get("eps_basic")),
            "xbrl_url": "",
        })
    return sorted(out, key=lambda x: x["to_date"], reverse=True)


def enrich_quarters(quarters):
    """Compute margins, QoQ% and YoY% (same quarter a year earlier, index + 4)."""
    enriched = []
    for i, q in enumerate(quarters):
        row = dict(q)
        rev = row.get("revenue_cr")
        pat = row.get("net_profit_cr")
        pbt = row.get("operating_profit_cr")
        row["net_margin_pct"] = round(pat / rev * 100.0, 2) if (rev and pat is not None and rev > 0) else None
        row["op_margin_pct"] = round(pbt / rev * 100.0, 2) if (rev and pbt is not None and rev > 0) else None

        qoq_rev = None
        if i + 1 < len(quarters) and quarters[i + 1].get("revenue_cr") and rev is not None:
            prev = quarters[i + 1]["revenue_cr"]
            if prev > 0:
                qoq_rev = round((rev - prev) / prev * 100.0, 2)
        yoy_rev = None
        yoy_pat = None
        if i + 4 < len(quarters):
            yr_q = quarters[i + 4]
            if yr_q.get("revenue_cr") and yr_q["revenue_cr"] > 0 and rev is not None:
                yoy_rev = round((rev - yr_q["revenue_cr"]) / yr_q["revenue_cr"] * 100.0, 2)
            if yr_q.get("net_profit_cr") and yr_q["net_profit_cr"] > 0 and pat is not None:
                yoy_pat = round((pat - yr_q["net_profit_cr"]) / abs(yr_q["net_profit_cr"]) * 100.0, 2)
        row["qoq_rev_pct"] = qoq_rev
        row["yoy_rev_pct"] = yoy_rev
        row["yoy_pat_pct"] = yoy_pat
        enriched.append(row)
    return enriched


def analyze_symbol(sym, quarters_n=4, offline=None, csv_path=None):
    sym = N.norm_symbol(sym)
    notes = []
    quarters = []

    if csv_path and os.path.exists(csv_path):
        quarters = parse_results_csv(N.read_maybe_gz(csv_path))
        notes.append("Read manual CSV %s." % csv_path)
    elif offline:
        j_path = os.path.join(offline, "results-%s.json" % sym)
        x_path = os.path.join(offline, "xbrl-%s.xml" % sym)
        c_path = os.path.join(offline, "results-%s.csv" % sym)
        if os.path.exists(j_path):
            raw = json.loads(N.read_maybe_gz(j_path))
            rows = raw.get("data") if isinstance(raw, dict) else raw
            quarters = parse_nse_results_json(rows)
        if not quarters and os.path.exists(x_path):
            q = parse_indas_xbrl(N.read_maybe_gz(x_path))
            if q:
                quarters = [q]
        if not quarters and os.path.exists(c_path):
            quarters = parse_results_csv(N.read_maybe_gz(c_path))
    else:
        sess = N.NSESession()
        try:
            raw = sess.get_json("/api/corporates-financial-results",
                                params={"index": "equities", "symbol": sym, "period": "Quarterly"},
                                referer_page="/companies-listing/corporate-filings-financial-results")
            rows = raw.get("data") if isinstance(raw, dict) else raw
            quarters = parse_nse_results_json(rows)
        except N.NSEError as e:
            notes.append(str(e) + " Use --offline DIR or --csv FILE with manually saved results.")

    enriched = enrich_quarters(quarters)
    shown = enriched[: int(quarters_n)]
    ttm = None
    if len(enriched) >= 4:
        q4 = enriched[:4]
        revs = [q["revenue_cr"] for q in q4 if q.get("revenue_cr") is not None]
        pats = [q["net_profit_cr"] for q in q4 if q.get("net_profit_cr") is not None]
        epss = [q["eps_diluted"] for q in q4 if q.get("eps_diluted") is not None]
        ttm = {
            "revenue_cr": round(sum(revs), 2) if len(revs) == 4 else None,
            "net_profit_cr": round(sum(pats), 2) if len(pats) == 4 else None,
            "eps_diluted": round(sum(epss), 2) if len(epss) == 4 else None,
        }
    return {"symbol": sym, "currency": "INR", "unit": "crore", "quarters": shown, "ttm": ttm, "notes": notes}


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Quarterly Financial Results Analyst (trader-analyst)")
    p.add_argument("tickers", nargs="+", help="NSE symbol(s)")
    p.add_argument("--quarters", type=int, default=4)
    p.add_argument("--metrics", help="Write/merge summary rows to JSON file (e.g. trader/metrics.json)")
    p.add_argument("--offline", help="Folder with results-<SYM>.json, xbrl-<SYM>.xml, or results-<SYM>.csv")
    p.add_argument("--csv", help="Manual CSV results file for a single ticker")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    try:
        reports = [analyze_symbol(t, quarters_n=args.quarters, offline=args.offline, csv_path=args.csv)
                   for t in args.tickers]
        if args.metrics:
            existing = N.read_json(args.metrics, {"as_of": None, "currency": "INR", "unit": "crore", "companies": {}})
            comps = dict(existing.get("companies") or {})
            for rep in reports:
                if rep["quarters"]:
                    latest = rep["quarters"][0]
                    comps[rep["symbol"]] = {
                        "latest_quarter": latest["to_date"],
                        "consolidated": latest.get("consolidated", True),
                        "revenue_cr": latest.get("revenue_cr"),
                        "net_profit_cr": latest.get("net_profit_cr"),
                        "net_margin_pct": latest.get("net_margin_pct"),
                        "yoy_rev_pct": latest.get("yoy_rev_pct"),
                        "yoy_pat_pct": latest.get("yoy_pat_pct"),
                        "eps_diluted": latest.get("eps_diluted"),
                        "ttm": rep["ttm"],
                    }
            N.write_json(args.metrics, {"as_of": N.now_ist().isoformat(), "currency": "INR", "unit": "crore", "companies": comps})

        if args.json:
            print(json.dumps({"reports": reports}, indent=2))
        else:
            for rep in reports:
                print("=== %s (INR crore) ===" % rep["symbol"])
                for n in rep["notes"]:
                    print("  Note: " + n)
                if not rep["quarters"]:
                    print("  No quarterly results available.")
                    continue
                for q in rep["quarters"]:
                    scope = "Cons" if q.get("consolidated") else "SA"
                    print("  %s [%s]  Rev %s (YoY %s%%, QoQ %s%%) | PAT %s (Margin %s%%) | EPS %s"
                          % (q["to_date"], scope, q["revenue_cr"], q["yoy_rev_pct"], q["qoq_rev_pct"],
                             q["net_profit_cr"], q["net_margin_pct"], q["eps_diluted"]))
                if rep["ttm"]:
                    print("  TTM: Rev %s cr | PAT %s cr | EPS %s"
                          % (rep["ttm"]["revenue_cr"], rep["ttm"]["net_profit_cr"], rep["ttm"]["eps_diluted"]))
        return 0
    except N.NSEError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
