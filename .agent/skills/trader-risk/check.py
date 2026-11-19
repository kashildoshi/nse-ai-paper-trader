#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk (NSE India edition). Standard library only.

Validates proposed NSE orders in ₹ (INR) against trader/config.json,
trader/portfolio.json, trader/prices.json and today's broker log.
All US PDT (Pattern Day Trader) rules have been removed; NSE intraday rules
(MIS product, NSE tick sizes, Indian intraday charges, 15:20 IST safety cutoff,
2026 NSE holidays, optional circuit-band checks) are enforced.

    py check.py                               # checks trader/orders.json -> trader/state/risk.json
    py check.py --now "2026-11-18 10:15"      # pin IST clock
    py check.py --no-write --json

Exit code 0 when every order gets PASS, 1 when any order is BLOCKED.
"""

import argparse
import datetime
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as N

DEFAULT_ORDERS = "trader/orders.json"
DEFAULT_CONFIG = "trader/config.json"
DEFAULT_PORTFOLIO = "trader/portfolio.json"
DEFAULT_PRICES = "trader/prices.json"
DEFAULT_BROKER_STATE = "trader/state/broker.json"
DEFAULT_RISK_STATE = "trader/state/risk.json"
FRESH_MINUTES = 15


class RiskError(N.NSEError):
    pass


def order_fingerprint(order):
    core = {
        "symbol": str(order.get("symbol") or "").upper(),
        "exchange": str(order.get("exchange") or "NSE").upper(),
        "product": str(order.get("product") or "MIS").upper(),
        "side": str(order.get("side") or "").lower(),
        "qty": int(order.get("qty") or 0),
        "order_type": str(order.get("order_type") or order.get("type") or "market").lower(),
        "entry": order.get("entry"),
        "stop": order.get("stop"),
        "target": order.get("target"),
    }
    blob = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _is_valid_tick(price):
    if price is None:
        return True
    p = float(price)
    t = N.tick_size(p)
    rounded = N.round_tick(p, "nearest", tick=t)
    return abs(p - rounded) <= 1e-4


def _fresh_quote(sym, prices_doc, now):
    if not prices_doc:
        return None, None
    px_map = prices_doc.get("prices") or {}
    val = px_map.get(sym)
    if val is None:
        return None, None
    px = float(val["price"]) if isinstance(val, dict) else float(val)
    as_of = prices_doc.get("as_of")
    if as_of:
        try:
            dt = N.to_ist(N.parse_now(as_of))
            age = (now - dt).total_seconds() / 60.0
            if age > FRESH_MINUTES or age < -5:
                return None, "stale price (%d min old)" % int(age)
        except N.NSEError:
            pass
    return px, None


def _circuit_bounds(sym, prices_doc):
    if not prices_doc:
        return None, None
    c = (prices_doc.get("circuits") or {}).get(sym)
    if isinstance(c, dict):
        return c.get("lower"), c.get("upper")
    val = (prices_doc.get("prices") or {}).get(sym)
    if isinstance(val, dict):
        return val.get("lower_circuit"), val.get("upper_circuit")
    return None, None


def check_order_dict(order, cfg, portfolio=None, prices=None, broker_log=None, now=None, state_acc=None):
    """Validate one order dict. Returns verdict dict: {verdict:'PASS'|'BLOCK', blocks:[], warnings:[], ...}."""
    now = N.to_ist(now) if now else N.now_ist()
    blocks = []
    warnings = list(cfg.get("_notes") or [])

    if not cfg.get("paper", True):
        warnings.append("LIVE mode enabled (paper is false): real money at risk.")

    sym_raw = order.get("symbol")
    try:
        sym = N.norm_symbol(sym_raw)
    except N.NSEError as e:
        sym = str(sym_raw or "")
        blocks.append("format: %s" % e)

    exch = str(order.get("exchange") or "NSE").upper()
    if exch != "NSE":
        blocks.append("format: exchange %r must be NSE." % exch)

    side = str(order.get("side") or "").lower()
    if side not in ("buy", "sell"):
        blocks.append("format: side must be 'buy' or 'sell'.")

    product = str(order.get("product") or cfg.get("product") or "MIS").upper()
    qty_raw = order.get("qty")
    try:
        if isinstance(qty_raw, bool) or float(qty_raw) != int(float(qty_raw)) or int(float(qty_raw)) <= 0:
            blocks.append("format: qty must be a positive whole integer (NSE equity).")
            qty = 0
        else:
            qty = int(float(qty_raw))
    except (TypeError, ValueError):
        blocks.append("format: invalid qty %r." % qty_raw)
        qty = 0

    otype = str(order.get("order_type") or order.get("type") or "market").lower()
    if otype not in ("market", "limit"):
        blocks.append("format: unsupported order_type %r." % otype)

    # Reject F&O / options if allow_options is false
    if any(tag in str(sym_raw or "").upper() for tag in ("CE", "PE", "FUT")) and len(str(sym_raw or "")) > 8:
        if not cfg.get("allow_options", False):
            blocks.append("asset: options/futures are blocked (allow_options is false).")

    held_qty = int((portfolio or {}).get("positions", {}).get(sym, {}).get("qty", 0)) if portfolio else 0
    is_exit = (side == "sell" and held_qty >= qty > 0)

    if side == "sell" and not is_exit and not cfg.get("allow_short", False):
        blocks.append("short: selling %d %s when holding %d (allow_short is false)." % (qty, sym, held_qty))

    entry = order.get("entry")
    stop = order.get("stop")
    target = order.get("target")
    for fname, fval in (("entry", entry), ("stop", stop), ("target", target)):
        if fval is not None and not _is_valid_tick(fval):
            blocks.append("tick: %s price %s is not a valid NSE tick (tick %.2f)."
                          % (fname, fval, N.tick_size(float(fval))))

    if is_exit:
        verdict = "BLOCK" if blocks else "PASS"
        return {
            "symbol": sym, "side": side, "qty": qty, "is_exit": True,
            "verdict": verdict, "blocks": blocks, "warnings": warnings,
            "fingerprint": order_fingerprint(order),
        }

    # --- Entry checks ---
    if product != "MIS":
        blocks.append("format: intraday NSE trades must use product 'MIS' (got %r)." % product)

    blocked_list = {N.norm_symbol(x) for x in (cfg.get("blocked") or [])}
    if sym in blocked_list:
        blocks.append("blocked: %s is on your blocked list." % sym)

    # Accumulator state across batch + today's broker log
    acc = state_acc if state_acc is not None else {
        "trades_today": 0, "orders_today": 0, "daily_loss_inr": 0.0,
        "cash": float((portfolio or {}).get("cash") if portfolio and portfolio.get("cash") is not None else (cfg.get("budget") or 0)),
        "seen_pairs": set(),
    }
    budget = float(cfg.get("budget") or acc["cash"] or 0)
    if budget <= 0:
        blocks.append("cash: positive budget required in trader/config.json.")

    fresh_px, price_note = _fresh_quote(sym, prices, now)
    if price_note:
        warnings.append("%s: %s." % (sym, price_note))

    if entry is None:
        blocks.append("format: entry price is required.")
        ref_entry = 0.0
    else:
        e_val = float(entry)
        if otype == "market" and fresh_px is not None and fresh_px > e_val:
            ref_entry = N.round_tick(fresh_px, "nearest")
        else:
            ref_entry = e_val

    cost_inr = qty * ref_entry
    min_ord = float(cfg.get("min_order_inr", 500))
    max_ord = float(cfg.get("max_order_inr", 200000))
    if cost_inr > 0:
        if cost_inr < min_ord:
            blocks.append("size: order value %s is under min_order_inr %s." % (N.inr(cost_inr), N.inr(min_ord)))
        if cost_inr > max_ord:
            blocks.append("size: order value %s exceeds max_order_inr %s." % (N.inr(cost_inr), N.inr(max_ord)))

    max_pos_pct = float(cfg.get("max_position_pct", 75))
    if budget > 0 and cost_inr > budget * max_pos_pct / 100.0 + 1e-6:
        blocks.append("position: %s exceeds %g%% of budget (%s)."
                      % (N.inr(cost_inr), max_pos_pct, N.inr(budget * max_pos_pct / 100.0)))

    buf_pct = float(cfg.get("cash_buffer_pct", 10))
    cash_buf = budget * buf_pct / 100.0
    if not cfg.get("allow_margin", False) and acc["cash"] - cost_inr < cash_buf - 1e-6:
        blocks.append("cash: remaining cash %s would dip below %g%% buffer (%s)."
                      % (N.inr(acc["cash"] - cost_inr), buf_pct, N.inr(cash_buf)))

    # Stop & trailing checks
    if cfg.get("require_stop", True):
        if stop is None:
            blocks.append("stop: initial stop is required (require_stop is true).")
        elif ref_entry > 0 and float(stop) >= ref_entry:
            blocks.append("stop: stop %s must be below entry %s." % (N.inr(stop), N.inr(ref_entry)))

    tr = order.get("trailing") or {}
    act_pct = float(tr.get("activate_pct", cfg.get("trail_activate_pct", 0.5)))
    tr_pct = float(tr.get("trail_pct", cfg.get("trail_pct", 0.6)))
    if act_pct <= 0 or tr_pct <= 0:
        blocks.append("trailing: activate_pct (%g) and trail_pct (%g) must be > 0." % (act_pct, tr_pct))

    # Loss & Reward:Risk (net of charges)
    max_loss_trade_lim = float(cfg.get("max_loss_per_trade_inr", 1000))
    max_daily_lim = float(cfg.get("max_daily_loss_inr", 3000))
    min_rr = float(cfg.get("min_reward_risk", 1.0))

    max_loss_inr = 0.0
    if qty > 0 and stop is not None and ref_entry > float(stop):
        ch_stop = N.intraday_charges(qty, ref_entry, float(stop), cfg)
        max_loss_inr = round(qty * (ref_entry - float(stop)) + ch_stop["total"], 2)
        if max_loss_inr > max_loss_trade_lim + 1e-6:
            blocks.append("loss: max loss incl. charges %s exceeds max_loss_per_trade_inr %s."
                          % (N.inr(max_loss_inr), N.inr(max_loss_trade_lim)))
        if acc["daily_loss_inr"] + max_loss_inr > max_daily_lim + 1e-6:
            blocks.append("daily_loss: today's risk %s + %s = %s exceeds max_daily_loss_inr %s."
                          % (N.inr(acc["daily_loss_inr"]), N.inr(max_loss_inr),
                             N.inr(acc["daily_loss_inr"] + max_loss_inr), N.inr(max_daily_lim)))

        plan_meta = order.get("plan") or {}
        ref_target = float(target) if target is not None else float(
            plan_meta.get("ref_target") or N.round_tick(ref_entry + 2.0 * (ref_entry - float(stop)), "nearest")
        )
        if ref_target <= ref_entry:
            blocks.append("reward_risk: entry %s is at or above target %s." % (N.inr(ref_entry), N.inr(ref_target)))
        else:
            ch_tgt = N.intraday_charges(qty, ref_entry, ref_target, cfg)
            net_reward = qty * (ref_target - ref_entry) - ch_tgt["total"]
            rr_net = net_reward / max_loss_inr if max_loss_inr > 0 else 0.0
            if rr_net < min_rr - 1e-6:
                blocks.append("reward_risk: net R:R %.2f is under min_reward_risk %.2f%s."
                              % (rr_net, min_rr, " (using 2R ref)" if target is None else ""))

    # Window & Holiday checks (IST)
    closed_msg = N.closed_reason(now.date(), cfg)
    if closed_msg:
        blocks.append("window: %s" % closed_msg)
    cutoff_t = N.config_time(cfg, "safety_cutoff", N.SAFETY_CUTOFF)
    if now.time() >= cutoff_t:
        blocks.append("window: %s is at or past safety cutoff %s IST." % (N.fmt_time(now), cutoff_t.strftime("%H:%M")))
    else:
        nb = cfg.get("no_entries_before")
        na = cfg.get("no_entries_after")
        if nb and now.time() < N.hhmm(nb):
            blocks.append("window: %s is before no_entries_before %s IST." % (N.fmt_time(now), nb))
        if na and now.time() > N.hhmm(na):
            blocks.append("window: %s is after no_entries_after %s IST." % (N.fmt_time(now), na))

    # Daily limits & duplicate checks
    pair = (sym, side)
    if pair in acc["seen_pairs"]:
        blocks.append("duplicate: %s %s already in this batch or placed today." % (side, sym))
    if acc["trades_today"] + 1 > int(cfg.get("max_trades_per_day", 3)):
        blocks.append("trades: already at max_trades_per_day (%d)." % int(cfg.get("max_trades_per_day", 3)))
    if acc["orders_today"] + 1 > int(cfg.get("max_orders_per_day", 10)):
        blocks.append("daily_limit: already at max_orders_per_day (%d)." % int(cfg.get("max_orders_per_day", 10)))

    # Optional NSE circuit band check
    lo_c, hi_c = _circuit_bounds(sym, prices)
    if hi_c is not None and ref_entry >= float(hi_c) * 0.995:
        blocks.append("circuit: %s is within 0.5%% of upper circuit %s." % (N.inr(ref_entry), N.inr(hi_c)))
    if lo_c is not None and ref_entry <= float(lo_c) * 1.005:
        blocks.append("circuit: %s is within 0.5%% of lower circuit %s." % (N.inr(ref_entry), N.inr(lo_c)))

    passed = len(blocks) == 0
    if passed:
        acc["trades_today"] += 1
        acc["orders_today"] += 1
        acc["daily_loss_inr"] += max_loss_inr
        acc["cash"] -= cost_inr
        acc["seen_pairs"].add(pair)

    return {
        "symbol": sym,
        "side": side,
        "qty": qty,
        "entry": ref_entry,
        "stop": stop,
        "target": target,
        "max_loss_inr": max_loss_inr,
        "is_exit": False,
        "verdict": "PASS" if passed else "BLOCK",
        "blocks": blocks,
        "warnings": warnings,
        "fingerprint": order_fingerprint(order),
    }


def _seed_acc_from_broker_log(broker_log, portfolio, cfg, today_str):
    trades_today = 0
    orders_today = 0
    daily_loss = 0.0
    seen = set()
    for ev in (broker_log or {}).get("events", []):
        if ev.get("date") != today_str:
            continue
        if ev.get("type") in ("placed", "paper_fill"):
            orders_today += 1
            if ev.get("side") == "buy":
                trades_today += 1
                daily_loss += float(ev.get("max_loss_inr") or 0.0)
                seen.add((str(ev.get("symbol", "")).upper(), "buy"))
    budget = float(cfg.get("budget") or 0)
    cash = float(portfolio["cash"]) if (portfolio and portfolio.get("cash") is not None) else budget
    return {
        "trades_today": trades_today,
        "orders_today": orders_today,
        "daily_loss_inr": daily_loss,
        "cash": cash,
        "seen_pairs": seen,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Pre-Trade Risk Check (trader-risk)")
    p.add_argument("--orders", default=DEFAULT_ORDERS)
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--portfolio", default=DEFAULT_PORTFOLIO)
    p.add_argument("--prices", default=DEFAULT_PRICES)
    p.add_argument("--broker-state", default=DEFAULT_BROKER_STATE)
    p.add_argument("--now", help="Pin IST clock 'YYYY-MM-DD HH:MM'")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    try:
        cfg = N.load_config(args.config)
        orders_doc = N.read_json(args.orders)
        if not orders_doc or not orders_doc.get("orders"):
            raise RiskError("No orders in %s." % args.orders)
        now = N.parse_now(args.now) if args.now else N.now_ist()
        portfolio = N.read_json(args.portfolio)
        prices = N.read_json(args.prices)
        broker_log = N.read_json(args.broker_state)

        acc = _seed_acc_from_broker_log(broker_log, portfolio, cfg, now.date().isoformat())
        verdicts = []
        for ord_d in orders_doc["orders"]:
            v = check_order_dict(ord_d, cfg, portfolio=portfolio, prices=prices,
                                 broker_log=broker_log, now=now, state_acc=acc)
            verdicts.append(v)

        out = {
            "checked_at": now.isoformat(),
            "date": now.date().isoformat(),
            "market": "NSE",
            "paper": bool(cfg.get("paper", True)),
            "verdicts": verdicts,
        }
        if not args.no_write:
            N.write_json(DEFAULT_RISK_STATE, out)

        all_pass = all(v["verdict"] == "PASS" for v in verdicts)
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            for idx, v in enumerate(verdicts, 1):
                print("%s  #%d %s %s %d shares (max loss %s)"
                      % (v["verdict"], idx, v["side"], v["symbol"], v["qty"], N.inr(v.get("max_loss_inr", 0))))
                for b in v["blocks"]:
                    print("      - %s" % b)
                for w in v["warnings"]:
                    print("      ! %s" % w)
        return 0 if all_pass else 1
    except N.NSEError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
