#!/usr/bin/env python3
"""Price alerts and the NSE IST clock for trader-alerts (NSE India edition). Standard library only.

    py alerts.py clock                             # NSE open/pre-open/closed + next open
    py alerts.py quote RELIANCE --write            # -> trader/prices.json
    py alerts.py quote --holdings --write          # portfolio + plan tickers
    py alerts.py add --from-plan                   # trail stop, optional target & 15:20 cutoff from plan.json
    py alerts.py add --symbol RELIANCE --stop 2445.00
    py alerts.py add --symbol RELIANCE --target 2497.50
    py alerts.py add --at 15:20 --label "safety cutoff: square off MIS"
    py alerts.py add --opening-bell
    py alerts.py check                             # rings what hit once; ratchets trail rules
    py alerts.py list
    py alerts.py reset reliance-plan-trail
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as N

DEFAULT_ALERTS = "trader/alerts.json"
DEFAULT_STATE = "trader/state/alerts.json"
DEFAULT_PRICES = "trader/prices.json"
DEFAULT_PLAN = "trader/plan.json"
DEFAULT_PORTFOLIO = "trader/portfolio.json"
DEFAULT_CONFIG = "trader/config.json"


class AlertsError(N.NSEError):
    pass


def _load_offline_quotes(syms, offline_dir):
    out = {}
    for sym in syms:
        cands = [
            os.path.join(offline_dir, "yahoo-%s.json" % sym),
            os.path.join(offline_dir, "yahoo-%s.json.gz" % sym),
            os.path.join(offline_dir, "%s.json" % sym),
            os.path.join(offline_dir, "%s.json.gz" % sym),
        ]
        if os.path.isfile(offline_dir):
            cands = [offline_dir]
        src = next((c for c in cands if os.path.exists(c)), None)
        if not src:
            continue
        doc = json.loads(N.read_maybe_gz(src))
        if isinstance(doc, dict) and sym in doc and "chart" not in doc:
            val = doc[sym]
            if isinstance(val, (int, float)):
                out[sym] = {"price": float(val), "prev_close": float(val), "high": float(val), "low": float(val)}
                continue
            doc = val
        if isinstance(doc, dict) and "chart" in doc:
            res = doc["chart"]["result"][0]
            meta = res.get("meta") or {}
            bars, _ = N.bars_from_yahoo(doc, 5)
            p = float(meta.get("regularMarketPrice") or (bars[-1]["c"] if bars else 0))
            pc = float(meta.get("previousClose") or meta.get("chartPreviousClose") or p)
            hi = max((b["h"] for b in bars), default=p)
            lo = min((b["l"] for b in bars), default=p)
            out[sym] = {"price": p, "prev_close": pc, "high": hi, "low": lo}
        elif isinstance(doc, dict) and "price" in doc:
            p = float(doc["price"])
            out[sym] = {"price": p, "prev_close": float(doc.get("prev_close", p)),
                        "high": float(doc.get("high", p)), "low": float(doc.get("low", p))}
    return out


def _fetch_quotes(syms, feed="yahoo", offline=None):
    if not syms:
        return {}
    if offline:
        return _load_offline_quotes(syms, offline)
    prices = N.get_ltp(syms, feed=feed)
    return {s: {"price": p, "prev_close": p, "high": p, "low": p} for s, p in prices.items()}


def cmd_clock(args, cfg):
    now = N.parse_now(args.now) if args.now else N.now_ist()
    st = N.market_state(now, cfg)
    reason = N.closed_reason(now.date(), cfg)
    note = N.calendar_note(now.date())
    next_day = now.date()
    if st in ("closed", "post_close"):
        next_day += datetime.timedelta(days=1)
        while not N.is_trading_day(next_day, cfg):
            next_day += datetime.timedelta(days=1)
    doc = {
        "market": "NSE",
        "now_ist": now.isoformat(),
        "state": st,
        "session": "09:15-15:30 IST",
        "safety_cutoff": cfg.get("safety_cutoff", N.SAFETY_CUTOFF),
        "closed_reason": reason,
        "next_open": "%s 09:15 IST" % next_day.isoformat() if st in ("closed", "post_close") else "today",
        "calendar_note": note,
    }
    if args.json:
        print(json.dumps(doc, indent=2))
    else:
        print("NSE %s (%s) | session 09:15-15:30 IST | MIS cutoff %s IST"
              % (st.upper(), N.fmt_time(now), doc["safety_cutoff"]))
        if reason:
            print("  " + reason)
        if st in ("closed", "post_close"):
            print("  Next open: %s" % doc["next_open"])
        if note:
            print("  Note: " + note)
    return 0


def cmd_quote(args, cfg):
    syms = []
    if args.symbols:
        syms.extend(N.parse_symbols(args.symbols))
    if args.holdings:
        pf = N.read_json(DEFAULT_PORTFOLIO, {}) or {}
        for k in (pf.get("positions") or {}):
            syms.append(N.norm_symbol(k))
        plan = N.read_json(DEFAULT_PLAN)
        if plan and plan.get("symbol"):
            syms.append(N.norm_symbol(plan["symbol"]))
    syms = list(dict.fromkeys(syms))
    if not syms:
        raise AlertsError("Give symbols or --holdings.")
    now = N.parse_now(args.now) if args.now else N.now_ist()
    feed = args.feed or cfg.get("data_feed") or "yahoo"
    quotes = _fetch_quotes(syms, feed=feed, offline=args.offline)
    if args.write and quotes:
        cur = N.read_json(DEFAULT_PRICES, {}) or {}
        merged = dict(cur.get("prices") or {})
        for s, q in quotes.items():
            merged[s] = q["price"]
        N.write_json(DEFAULT_PRICES, {"as_of": now.isoformat(), "market": "NSE", "feed": feed, "prices": merged})
    if args.json:
        print(json.dumps(quotes, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                print("  %-12s %s" % (s, N.inr(q["price"])))
            else:
                print("  %-12s (no quote)" % s)
    return 0 if quotes else 1


def _upsert_rule(rules, rule):
    rid = rule["id"]
    out = [r for r in rules if r.get("id") != rid]
    out.append(rule)
    return out


def cmd_add(args, cfg):
    doc = N.read_json(DEFAULT_ALERTS, {"rules": []}) or {"rules": []}
    rules = list(doc.get("rules") or [])
    added = []

    if args.from_plan:
        plan = N.read_json(DEFAULT_PLAN)
        if not plan:
            raise AlertsError("No %s found." % DEFAULT_PLAN)
        sym = N.norm_symbol(plan["symbol"])
        slug = sym.lower()
        d = plan.get("date")
        tr = plan.get("trailing") or {}
        trail_rule = {
            "id": "%s-plan-trail" % slug,
            "symbol": sym,
            "type": "trail",
            "entry": float(plan["entry"]),
            "initial_stop": float(plan["stop"]),
            "price": float(tr.get("current_stop", plan["stop"])),
            "high_water": float(tr.get("high_water", plan["entry"])),
            "activate_pct": float(tr.get("activate_pct", 0.5)),
            "trail_pct": float(tr.get("trail_pct", 0.6)),
            "active": bool(tr.get("active", False)),
            "note": "ratcheting trailing stop (+%g%% arms, %g%% trail)" % (
                tr.get("activate_pct", 0.5), tr.get("trail_pct", 0.6)),
            "date": d,
        }
        rules = _upsert_rule(rules, trail_rule)
        added.append(trail_rule)

        if plan.get("target") is not None:
            tgt_rule = {
                "id": "%s-plan-target" % slug,
                "symbol": sym,
                "type": "target",
                "price": float(plan["target"]),
                "note": "plan target (%s)" % (plan.get("target_label") or ""),
                "date": d,
            }
            rules = _upsert_rule(rules, tgt_rule)
            added.append(tgt_rule)

        cutoff = plan.get("safety_cutoff") or plan.get("flat_by") or N.SAFETY_CUTOFF
        t_rule = {
            "id": "%s-safety-cutoff" % slug,
            "symbol": sym,
            "type": "time",
            "at": cutoff,
            "label": "safety cutoff: square off MIS",
            "date": d,
        }
        rules = _upsert_rule(rules, t_rule)
        added.append(t_rule)
    elif args.opening_bell:
        r = {"id": "nse-opening-bell", "type": "opening_bell"}
        rules = _upsert_rule(rules, r)
        added.append(r)
    elif args.at:
        t = N.hhmm(args.at).strftime("%H:%M")
        lbl = args.label or ("time alert %s" % t)
        r = {"id": "time-%s" % t.replace(":", ""), "type": "time", "at": t, "label": lbl}
        if args.symbol:
            r["symbol"] = N.norm_symbol(args.symbol)
        rules = _upsert_rule(rules, r)
        added.append(r)
    else:
        if not args.symbol:
            raise AlertsError("Specify --from-plan, --opening-bell, --at, or --symbol.")
        sym = N.norm_symbol(args.symbol)
        slug = sym.lower()
        if args.stop is not None:
            r = {"id": "%s-stop-%g" % (slug, args.stop), "symbol": sym, "type": "stop", "price": float(args.stop)}
        elif args.target is not None:
            r = {"id": "%s-target-%g" % (slug, args.target), "symbol": sym, "type": "target", "price": float(args.target)}
        elif args.above is not None:
            r = {"id": "%s-above-%g" % (slug, args.above), "symbol": sym, "type": "above", "price": float(args.above)}
        elif args.below is not None:
            r = {"id": "%s-below-%g" % (slug, args.below), "symbol": sym, "type": "below", "price": float(args.below)}
        elif args.pct is not None:
            r = {"id": "%s-pct-%g" % (slug, args.pct), "symbol": sym, "type": "pct_move", "pct": float(args.pct)}
        else:
            raise AlertsError("Give --stop, --target, --above, --below, or --pct.")
        rules = _upsert_rule(rules, r)
        added.append(r)

    N.write_json(DEFAULT_ALERTS, {"rules": rules})
    if args.json:
        print(json.dumps(added, indent=2))
    else:
        for r in added:
            print("Added alert %-22s  type=%s" % (r["id"], r["type"]))
    return 0


def cmd_check(args, cfg):
    doc = N.read_json(DEFAULT_ALERTS, {"rules": []}) or {"rules": []}
    rules = list(doc.get("rules") or [])
    state = N.read_json(DEFAULT_STATE, {"fired": {}}) or {"fired": {}}
    fired_map = dict(state.get("fired") or {})

    now = N.parse_now(args.now) if args.now else N.now_ist()
    today_str = now.date().isoformat()
    trading_today = N.is_trading_day(now.date(), cfg)
    m_state = N.market_state(now, cfg)

    # Collect tickers needed for today's active rules
    needed_syms = []
    for r in rules:
        if r.get("date") and r["date"] != today_str:
            continue
        if r.get("symbol") and r.get("type") in ("above", "below", "stop", "target", "pct_move", "trail"):
            if r["id"] not in fired_map or r.get("rearm"):
                needed_syms.append(N.norm_symbol(r["symbol"]))
    needed_syms = list(dict.fromkeys(needed_syms))

    feed = args.feed or cfg.get("data_feed") or "yahoo"
    quotes = _fetch_quotes(needed_syms, feed=feed, offline=args.offline) if needed_syms else {}
    if quotes:
        cur_p = N.read_json(DEFAULT_PRICES, {}) or {}
        merged = dict(cur_p.get("prices") or {})
        for s, q in quotes.items():
            merged[s] = q["price"]
        N.write_json(DEFAULT_PRICES, {"as_of": now.isoformat(), "market": "NSE", "feed": feed, "prices": merged})

    rang = []
    rules_dirty = False
    for r in rules:
        rid = r["id"]
        if r.get("date") and r["date"] != today_str:
            continue
        rtype = r["type"]

        if rtype == "opening_bell":
            day_key = "%s@%s" % (rid, today_str)
            if trading_today and m_state == "open" and day_key not in fired_map:
                fired_map[day_key] = now.isoformat()
                rang.append((r, "NSE OPENING BELL (%s): market is open." % N.fmt_time(now)))
            continue

        if rtype == "time":
            day_key = "%s@%s" % (rid, today_str)
            at_t = N.hhmm(r["at"])
            if trading_today and now.time() >= at_t and day_key not in fired_map:
                fired_map[day_key] = now.isoformat()
                rang.append((r, "ALERT %s: %s (set for %s IST)." % (r.get("label", "time"), N.fmt_time(now), r["at"])))
            continue

        sym = N.norm_symbol(r["symbol"])
        q = quotes.get(sym)
        if not q:
            continue
        px, hi, lo, pc = q["price"], q["high"], q["low"], q["prev_close"]

        if rtype == "trail":
            if rid in fired_map:
                continue
            t_state = {
                "entry": float(r["entry"]),
                "initial_stop": float(r.get("initial_stop", r["price"])),
                "stop": float(r["price"]),
                "high_water": float(r.get("high_water", r["entry"])),
                "activate_pct": float(r.get("activate_pct", 0.5)),
                "trail_pct": float(r.get("trail_pct", 0.6)),
                "active": bool(r.get("active", False)),
            }
            prev_stop = t_state["stop"]
            new_state, ev = N.trail_update(t_state, hi, lo, px)
            r["high_water"] = round(new_state["high_water"], 2)
            r["price"] = max(prev_stop, float(new_state["stop"]))
            r["active"] = bool(new_state["active"])
            rules_dirty = True
            if ev == "stopped":
                fired_map[rid] = now.isoformat()
                rang.append((r, "ALERT TRAILING STOP HIT %s: low/price %s hit stop %s (high water %s)."
                             % (sym, N.inr(lo), N.inr(r["price"]), N.inr(r["high_water"]))))
            elif r["price"] > prev_stop + 1e-9:
                # Notify ratchet move without permanently silencing future stop hit
                rang.append((r, "TRAIL RATCHET %s: move stop-loss to %s (high water %s)."
                             % (sym, N.inr(r["price"]), N.inr(r["high_water"]))))
            continue

        if rtype in ("above", "target"):
            if px >= float(r["price"]):
                if rid not in fired_map:
                    fired_map[rid] = now.isoformat()
                    rang.append((r, "ALERT %s %s: %s >= %s." % (sym, rtype.upper(), N.inr(px), N.inr(r["price"]))))
            elif r.get("rearm") and rid in fired_map:
                del fired_map[rid]

        elif rtype in ("below", "stop"):
            if px <= float(r["price"]):
                if rid not in fired_map:
                    fired_map[rid] = now.isoformat()
                    rang.append((r, "ALERT %s %s: %s <= %s." % (sym, rtype.upper(), N.inr(px), N.inr(r["price"]))))
            elif r.get("rearm") and rid in fired_map:
                del fired_map[rid]

        elif rtype == "pct_move":
            day_key = "%s@%s" % (rid, today_str)
            ref = float(r.get("ref") or pc or 0)
            if ref > 0 and day_key not in fired_map:
                chg = (px - ref) / ref * 100.0
                if abs(chg) >= float(r["pct"]):
                    fired_map[day_key] = now.isoformat()
                    rang.append((r, "ALERT %s MOVE %+.2f%% (%s vs %s)." % (sym, chg, N.inr(px), N.inr(ref))))

    if rules_dirty:
        N.write_json(DEFAULT_ALERTS, {"rules": rules})
    N.write_json(DEFAULT_STATE, {"fired": fired_map, "last_checked": now.isoformat()})

    if args.json:
        print(json.dumps({"now": now.isoformat(), "market_state": m_state,
                          "alerts": [{"id": r["id"], "message": m} for r, m in rang]}, indent=2))
    else:
        if rang:
            for _, msg in rang:
                print(msg)
        else:
            print("NSE %s (%s). Nothing hit across %d rule(s)." % (m_state.upper(), N.fmt_time(now), len(rules)))
    return 0 if rang else 1


def cmd_list(args, cfg):
    doc = N.read_json(DEFAULT_ALERTS, {"rules": []}) or {"rules": []}
    rules = doc.get("rules") or []
    if args.json:
        print(json.dumps(doc, indent=2))
        return 0
    if not rules:
        print("No alert rules.")
        return 0
    for r in rules:
        print("  %-22s %-12s %-10s %s" % (r["id"], r["type"], r.get("symbol", "-"),
                                          N.inr(r["price"]) if "price" in r else r.get("at", "")))
    return 0


def cmd_reset(args, cfg):
    state = N.read_json(DEFAULT_STATE, {"fired": {}}) or {"fired": {}}
    fired = dict(state.get("fired") or {})
    removed = 0
    for k in list(fired):
        if args.rule_id == "all" or k == args.rule_id or k.startswith(args.rule_id + "@"):
            del fired[k]
            removed += 1
    N.write_json(DEFAULT_STATE, {"fired": fired})
    print("Reset %d alert state entry/entries for %s." % (removed, args.rule_id))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Price Alerts & IST Clock (trader-alerts)")
    sub = p.add_subparsers(dest="cmd")

    sc = sub.add_parser("clock", help="NSE IST market state and next open")
    sc.add_argument("--now")
    sc.add_argument("--json", action="store_true")

    sq = sub.add_parser("quote", help="Fetch LTP for symbols")
    sq.add_argument("symbols", nargs="?")
    sq.add_argument("--holdings", action="store_true")
    sq.add_argument("--feed", choices=("yahoo", "upstox", "angelone"))
    sq.add_argument("--offline")
    sq.add_argument("--now")
    sq.add_argument("--write", action="store_true")
    sq.add_argument("--json", action="store_true")

    sa = sub.add_parser("add", help="Add an alert rule")
    sa.add_argument("--from-plan", action="store_true")
    sa.add_argument("--opening-bell", action="store_true")
    sa.add_argument("--symbol")
    sa.add_argument("--stop", type=float)
    sa.add_argument("--target", type=float)
    sa.add_argument("--above", type=float)
    sa.add_argument("--below", type=float)
    sa.add_argument("--pct", type=float)
    sa.add_argument("--at")
    sa.add_argument("--label")
    sa.add_argument("--json", action="store_true")

    sk = sub.add_parser("check", help="Evaluate all rules once")
    sk.add_argument("--feed", choices=("yahoo", "upstox", "angelone"))
    sk.add_argument("--offline")
    sk.add_argument("--now")
    sk.add_argument("--json", action="store_true")

    sl = sub.add_parser("list", help="List rules")
    sl.add_argument("--json", action="store_true")

    sr = sub.add_parser("reset", help="Reset a fired alert id (or 'all')")
    sr.add_argument("rule_id")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 1
    try:
        cfg = N.load_config(DEFAULT_CONFIG)
        if args.cmd == "clock":
            return cmd_clock(args, cfg)
        if args.cmd == "quote":
            return cmd_quote(args, cfg)
        if args.cmd == "add":
            return cmd_add(args, cfg)
        if args.cmd == "check":
            return cmd_check(args, cfg)
        if args.cmd == "list":
            return cmd_list(args, cfg)
        return cmd_reset(args, cfg)
    except N.NSEError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
