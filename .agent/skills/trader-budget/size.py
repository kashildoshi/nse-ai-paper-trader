#!/usr/bin/env python3
"""Position sizing for trader-budget (NSE India edition). Standard library only.

Sizes an intraday NSE MIS equity trade in ₹ (INR) from trader/plan.json and
trader/config.json, factoring in full Indian intraday charges (brokerage,
STT 0.025% sell, NSE txn 0.00297%, SEBI fee, stamp duty 0.003% buy, 18% GST).

    py size.py --write                      # write trader/orders.json
    py size.py --limit --write              # limit order at plan entry
    py size.py --limit 2462.00 --write      # limit order at explicit tick price
    py size.py --targets targets.example.json --budget 100000 --prices trader/prices.json

Nothing is placed: trader-risk checks the order and trader-broker requires your yes.
"""

import argparse
import datetime
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as N

DEFAULT_CONFIG = "trader/config.json"
DEFAULT_PLAN = "trader/plan.json"
DEFAULT_PRICES = "trader/prices.json"
DEFAULT_PORTFOLIO = "trader/portfolio.json"
DEFAULT_ORDERS = "trader/orders.json"
FRESH_MINUTES = 15


class SizeError(N.NSEError):
    pass


def _fresh_price(sym, prices_doc, now):
    if not prices_doc or sym not in (prices_doc.get("prices") or {}):
        return None, None
    as_of = prices_doc.get("as_of")
    if as_of:
        try:
            dt = N.to_ist(N.parse_now(as_of))
            age_min = (now - dt).total_seconds() / 60.0
            if age_min > FRESH_MINUTES or age_min < -5:
                return None, "stale price (%d min old)" % int(age_min)
        except N.NSEError:
            pass
    return float(prices_doc["prices"][sym]), "fresh quote"


def size_plan_order(plan, cfg, portfolio=None, prices=None, now=None, limit=None):
    """Size one intraday NSE order from `plan`. Returns (order_dict, summary_dict)."""
    now = N.to_ist(now) if now else N.now_ist()
    if not plan:
        raise SizeError("No plan provided (run trader-exit first).")
    sym = N.norm_symbol(plan["symbol"])
    if sym in [N.norm_symbol(x) for x in (cfg.get("blocked") or [])]:
        raise SizeError("Refused: %s is on your blocked list." % sym)
    if plan.get("date") and plan["date"] != now.date().isoformat():
        raise SizeError("Refused: plan date %s is not today (%s)." % (plan["date"], now.date().isoformat()))

    budget = cfg.get("budget")
    if budget is None or float(budget) <= 0:
        raise SizeError("Set a positive 'budget' (in INR) in trader/config.json or pass --budget.")
    budget = float(budget)

    entry_plan = float(plan["entry"])
    stop = float(plan["stop"])
    target = plan.get("target")
    if stop >= entry_plan:
        raise SizeError("Refused: stop %s must be below entry %s." % (N.inr(stop), N.inr(entry_plan)))

    # Reference entry price
    if limit is not None:
        entry_ref = N.round_tick(float(limit), "nearest")
        entry_from = "limit"
        order_type = "limit"
    else:
        fresh_p, _ = _fresh_price(sym, prices, now)
        if fresh_p is not None and fresh_p > entry_plan:
            entry_ref = N.round_tick(fresh_p, "nearest")
            entry_from = "fresh_price"
        else:
            entry_ref = entry_plan
            entry_from = "plan"
        order_type = "market"

    if stop >= entry_ref:
        raise SizeError("Refused: stop %s is at or above reference entry %s." % (N.inr(stop), N.inr(entry_ref)))

    risk_per_share = round(entry_ref - stop, 2)
    ref_target = float(target) if target is not None else N.round_tick(entry_ref + 2.0 * float(plan["risk_per_share"]), "nearest")
    if target is not None and ref_target <= entry_ref:
        raise SizeError("Refused: price %s is already at or past target %s." % (N.inr(entry_ref), N.inr(ref_target)))

    buf_pct = float(cfg.get("cash_buffer_pct", 10))
    cap_pct = float(cfg.get("max_position_pct", 75))
    max_order_inr = float(cfg.get("max_order_inr", 200000))
    min_order_inr = float(cfg.get("min_order_inr", 500))
    max_loss_inr_lim = float(cfg.get("max_loss_per_trade_inr", 1000))
    min_rr = float(cfg.get("min_reward_risk", 1.0))

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = max(0.0, budget - cash_buffer)
    if portfolio and portfolio.get("cash") is not None:
        available = min(available, max(0.0, float(portfolio["cash"]) - cash_buffer if float(portfolio["cash"]) >= cash_buffer else float(portfolio["cash"])))

    mis_lev = float(cfg.get("mis_leverage", 1.0)) if cfg.get("allow_margin") else 1.0
    buying_power = available * max(1.0, mis_lev)

    cap_inr = budget * cap_pct / 100.0
    q_cap = int(math.floor((cap_inr + 1e-9) / entry_ref))
    q_max_ord = int(math.floor((max_order_inr + 1e-9) / entry_ref))
    q_cash = int(math.floor((buying_power + 1e-9) / entry_ref))

    # Iterate down from raw price-risk share count until total loss INCLUDING round-trip charges <= limit
    q_loss = int(math.floor((max_loss_inr_lim + 1e-9) / risk_per_share))
    while q_loss > 0:
        ch = N.intraday_charges(q_loss, entry_ref, stop, cfg)
        total_loss = q_loss * risk_per_share + ch["total"]
        if total_loss <= max_loss_inr_lim + 1e-9:
            break
        q_loss -= 1

    limits = [
        ("max_loss", q_loss, "%s / (%s + charges)" % (N.inr(max_loss_inr_lim), N.inr(risk_per_share))),
        ("position_cap", q_cap, "%g%% of %s" % (cap_pct, N.inr(budget))),
        ("max_order", q_max_ord, "%s / %s" % (N.inr(max_order_inr), N.inr(entry_ref))),
        ("cash", q_cash, "available %s / %s" % (N.inr(buying_power), N.inr(entry_ref))),
    ]
    bound = min(limits, key=lambda x: x[1])
    qty = bound[1]

    if qty <= 0:
        raise SizeError("Refused: 0 shares fit within %s limit (%s)." % (bound[0], bound[2]))

    cost_inr = round(qty * entry_ref, 2)
    if cost_inr < min_order_inr:
        raise SizeError("Refused: order value %s is under min_order_inr %s." % (N.inr(cost_inr), N.inr(min_order_inr)))

    ch_stop = N.intraday_charges(qty, entry_ref, stop, cfg)
    ch_tgt = N.intraday_charges(qty, entry_ref, ref_target, cfg)
    max_loss_total = round(qty * risk_per_share + ch_stop["total"], 2)
    net_reward = qty * (ref_target - entry_ref) - ch_tgt["total"]
    rr_net = round(net_reward / max_loss_total, 2) if max_loss_total > 0 else 0.0
    if rr_net < min_rr:
        raise SizeError("Refused: net reward:risk %.2f (after charges) is under min_reward_risk %.2f." % (rr_net, min_rr))

    be_px = N.breakeven_price(qty, entry_ref, cfg)
    tr = plan.get("trailing") or {}
    trailing = {
        "activate_pct": float(tr.get("activate_pct", cfg.get("trail_activate_pct", 0.5))),
        "trail_pct": float(tr.get("trail_pct", cfg.get("trail_pct", 0.6))),
    }
    cutoff = plan.get("safety_cutoff") or plan.get("flat_by") or cfg.get("safety_cutoff", N.SAFETY_CUTOFF)

    why = ("%s on %s: %d whole shares at %s, bound by %s; max loss incl. charges %s at stop %s"
           % (plan.get("setup", "setup"), sym, qty, N.inr(entry_ref), bound[0], N.inr(max_loss_total), N.inr(stop)))

    order = {
        "symbol": sym,
        "exchange": "NSE",
        "product": cfg.get("product", "MIS"),
        "side": "buy",
        "qty": qty,
        "order_type": order_type,
        "entry": entry_ref,
        "entry_from": entry_from,
        "stop": stop,
        "target": target,
        "trailing": trailing,
        "cost_inr": cost_inr,
        "max_loss_inr": max_loss_total,
        "charges_at_stop": ch_stop,
        "charges_at_target": ch_tgt,
        "breakeven_price": be_px,
        "reward_risk_net": rr_net,
        "flat_by": cutoff,
        "max_loss_per_trade_inr": max_loss_inr_lim,
        "why": why,
        "plan": {
            "date": plan.get("date") or now.date().isoformat(),
            "setup": plan.get("setup", ""),
            "entry": entry_ref,
            "stop": stop,
            "target": target,
            "ref_target": ref_target,
            "risk_per_share": risk_per_share,
            "max_loss_inr": max_loss_total,
            "entry_time": plan.get("entry_time"),
            "flat_by": cutoff,
        },
    }
    summary = {
        "budget": budget,
        "cash_buffer": cash_buffer,
        "available": available,
        "limits": [{"name": l[0], "shares": l[1], "how": l[2]} for l in limits],
        "bound_by": bound[0],
    }
    return order, summary


def size_weights(weights_map, cfg, prices_doc=None, whole_shares=True, top_n=None):
    """Split INR budget across NSE symbols by target weight (whole shares)."""
    budget = float(cfg.get("budget") or 0)
    if budget <= 0:
        raise SizeError("Set positive 'budget' (INR) in config or pass --budget.")
    blocked = {N.norm_symbol(x) for x in (cfg.get("blocked") or [])}
    clean = [(N.norm_symbol(k), float(v)) for k, v in weights_map.items() if N.norm_symbol(k) not in blocked and float(v) > 0]
    if top_n:
        clean.sort(key=lambda x: x[1], reverse=True)
        clean = clean[:top_n]
    if not clean:
        raise SizeError("No valid weights left after filtering.")

    buf_pct = float(cfg.get("cash_buffer_pct", 10))
    cap_pct = float(cfg.get("max_position_pct", 75))
    min_ord = float(cfg.get("min_order_inr", 500))
    investable = budget * (1.0 - buf_pct / 100.0)
    cap_amt = budget * cap_pct / 100.0

    tot_w = sum(w for _, w in clean)
    norm_w = {s: w / tot_w for s, w in clean}

    # Iterative cap redistribution
    alloc = {}
    pinned = set()
    rem_budget = investable
    for _ in range(len(norm_w) + 1):
        free_w = sum(norm_w[s] for s in norm_w if s not in pinned)
        if free_w <= 0:
            break
        over = False
        for s in norm_w:
            if s in pinned:
                continue
            amt = rem_budget * (norm_w[s] / free_w)
            if amt > cap_amt + 1e-6:
                alloc[s] = cap_amt
                pinned.add(s)
                rem_budget -= cap_amt
                over = True
        if not over:
            for s in norm_w:
                if s not in pinned:
                    alloc[s] = rem_budget * (norm_w[s] / free_w)
            break

    # Drop slices under min_order_inr
    kept = {s: round(v, 2) for s, v in alloc.items() if v >= min_ord}
    prices = (prices_doc or {}).get("prices") or {}
    positions = []
    invested = 0.0
    for s, amt in kept.items():
        px = prices.get(s)
        if whole_shares:
            if not px or float(px) <= 0:
                raise SizeError("Missing price for %s in prices.json (required for whole shares)." % s)
            qty = int(math.floor(amt / float(px)))
            spent = round(qty * float(px), 2)
        else:
            qty = round(amt / float(px), 4) if px else None
            spent = amt
        invested += spent
        positions.append({"symbol": s, "target_inr": amt, "qty": qty, "price": px, "spent_inr": spent})
    return {
        "budget": budget,
        "cash_buffer": round(budget - investable, 2),
        "investable": round(investable, 2),
        "invested": round(invested, 2),
        "idle": round(investable - invested, 2),
        "positions": positions,
    }


def _print_order(order, summary):
    print("NSE Position Size (%s): %s  %d shares @ %s = %s"
          % (order["product"], order["symbol"], order["qty"], N.inr(order["entry"]), N.inr(order["cost_inr"])))
    for lim in summary["limits"]:
        mark = " <- BOUND" if lim["name"] == summary["bound_by"] else ""
        print("  %-13s %5d shares  (%s)%s" % (lim["name"], lim["shares"], lim["how"], mark))
    ch = order["charges_at_stop"]
    print("  Stop %s | Max loss (incl. %s charges): %s (limit %s)"
          % (N.inr(order["stop"]), N.inr(ch["total"]), N.inr(order["max_loss_inr"]), N.inr(order["max_loss_per_trade_inr"])))
    print("  Charges breakdown at stop: brok %s, STT %s, exch %s, SEBI %s, stamp %s, GST %s"
          % (N.inr(ch["brokerage"]), N.inr(ch["stt"]), N.inr(ch["exchange_txn"]),
             N.inr(ch["sebi_fee"]), N.inr(ch["stamp_duty"]), N.inr(ch["gst"])))
    print("  Breakeven price: %s | Net R:R %s | Cutoff %s IST"
          % (N.inr(order["breakeven_price"]), order["reward_risk_net"], order["flat_by"]))
    print("  Tax note: " + N.TAX_NOTE)


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Position Sizing in INR (trader-budget)")
    p.add_argument("--plan", default=DEFAULT_PLAN)
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--prices", default=DEFAULT_PRICES)
    p.add_argument("--portfolio", default=DEFAULT_PORTFOLIO)
    p.add_argument("--budget", type=float, help="Override budget in INR")
    p.add_argument("--limit", nargs="?", const="PLAN", help="Limit order (at plan entry or given price)")
    p.add_argument("--weights", help="Portfolio weights: RELIANCE=30,TCS=30,...")
    p.add_argument("--targets", help="JSON targets file")
    p.add_argument("--top", type=int)
    p.add_argument("--now", help="Pin IST clock")
    p.add_argument("--write", action="store_true", help="Write trader/orders.json")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    try:
        cfg = N.load_config(args.config)
        if args.budget is not None:
            cfg["budget"] = args.budget
        now = N.parse_now(args.now) if args.now else N.now_ist()
        prices = N.read_json(args.prices)

        if args.weights or args.targets:
            if args.weights:
                wmap = {}
                for part in args.weights.split(","):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        wmap[k.strip()] = float(v)
            else:
                tdoc = N.read_json(args.targets)
                wmap = {r["ticker"]: float(r["weight"]) for r in tdoc.get("weights", [])}
            res = size_weights(wmap, cfg, prices_doc=prices, whole_shares=True, top_n=args.top)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print("NSE Portfolio Allocation | Budget %s | Invested %s | Idle %s"
                      % (N.inr(res["budget"]), N.inr(res["invested"]), N.inr(res["idle"])))
                for pos in res["positions"]:
                    print("  %-12s %5d shares @ %s = %s" % (pos["symbol"], pos["qty"], N.inr(pos["price"]), N.inr(pos["spent_inr"])))
            return 0

        plan = N.read_json(args.plan)
        portfolio = N.read_json(args.portfolio)
        lim_px = None
        if args.limit == "PLAN":
            lim_px = plan["entry"] if plan else None
        elif args.limit is not None:
            lim_px = float(args.limit)

        order, summary = size_plan_order(plan, cfg, portfolio=portfolio, prices=prices, now=now, limit=lim_px)
        out_doc = {
            "created_by": "trader-budget",
            "created_at": now.isoformat(),
            "market": "NSE",
            "currency": "INR",
            "status": "proposed",
            "paper": bool(cfg.get("paper", True)),
            "note": "Proposals only. Nothing is placed until trader-risk passes and you explicitly approve.",
            "orders": [order],
        }
        if args.write:
            N.write_json(DEFAULT_ORDERS, out_doc)
        if args.json:
            print(json.dumps({"order": order, "summary": summary, "paper": out_doc["paper"]}, indent=2))
        else:
            _print_order(order, summary)
        return 0
    except N.NSEError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
