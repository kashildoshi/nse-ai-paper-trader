#!/usr/bin/env python3
"""Order tickets and execution ledger for trader-broker (NSE India edition). Standard library only.

Replaces Alpaca with Upstox (default) and Angel One SmartAPI.
Paper trading ("paper": true) is enabled by default. Nothing ever contacts a
broker directly; live orders require all three gates:
  1. trader-risk passed this exact order today (matched by SHA-256 fingerprint)
  2. You typed an explicit yes for that one order (`approve`)
  3. Your MCP order tool is set to "Needs approval" and you clicked Allow once

    py ticket.py show
    py ticket.py approve --index 1 --reply "yes"
    py ticket.py paper-fill --index 1
    py ticket.py flatten --symbol RELIANCE --qty 20
    py ticket.py log
"""

import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as N

DEFAULT_ORDERS = "trader/orders.json"
DEFAULT_CONFIG = "trader/config.json"
DEFAULT_RISK_STATE = "trader/state/risk.json"
DEFAULT_BROKER_STATE = "trader/state/broker.json"
DEFAULT_PORTFOLIO = "trader/portfolio.json"
DEFAULT_PAPER_FILLS = "trader/paper_fills.json"
DEFAULT_PLAN = "trader/plan.json"

YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later", "hold", "unless", "if"}


class TicketError(N.NSEError):
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


def build_broker_payload(order, broker="upstox", isin_map=None, angel_tokens=None):
    sym = N.norm_symbol(order["symbol"])
    qty = int(order["qty"])
    side = str(order["side"]).upper()
    otype = str(order.get("order_type", "market")).upper()
    price = float(order.get("entry") or 0.0) if otype == "LIMIT" else 0.0
    stop = order.get("stop")
    b = (broker or "upstox").lower()

    if b in ("angelone", "angel"):
        tok = (angel_tokens or {}).get(sym, "LOOKUP_SYMBOLTOKEN")
        entry_payload = {
            "variety": "NORMAL",
            "tradingsymbol": "%s-EQ" % sym,
            "symboltoken": tok,
            "transactiontype": side,
            "exchange": "NSE",
            "ordertype": "LIMIT" if otype == "LIMIT" else "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(price),
            "quantity": str(qty),
        }
        sl_payload = None
        if stop is not None and side == "BUY":
            sl_payload = {
                "variety": "STOPLOSS",
                "tradingsymbol": "%s-EQ" % sym,
                "symboltoken": tok,
                "transactiontype": "SELL",
                "exchange": "NSE",
                "ordertype": "STOPLOSS_MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "price": "0",
                "triggerprice": str(stop),
                "quantity": str(qty),
            }
        return {"broker": "angelone", "entry_order": entry_payload, "stop_loss_order": sl_payload,
                "trailing_note": "Modify stop_loss_order triggerprice upward as `exit.py trail` updates current_stop."}

    isin = (isin_map or {}).get(sym)
    inst_tok = ("NSE_EQ|" + isin) if isin else ("NSE_EQ|LOOKUP_ISIN_%s" % sym)
    entry_payload = {
        "instrument_token": inst_tok,
        "quantity": qty,
        "product": "I",
        "validity": "DAY",
        "price": price,
        "order_type": "LIMIT" if otype == "LIMIT" else "MARKET",
        "transaction_type": side,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
    }
    sl_payload = None
    if stop is not None and side == "BUY":
        sl_payload = {
            "instrument_token": inst_tok,
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": 0,
            "order_type": "SL-M",
            "transaction_type": "SELL",
            "disclosed_quantity": 0,
            "trigger_price": float(stop),
            "is_amo": False,
        }
    return {"broker": "upstox", "entry_order": entry_payload, "stop_loss_order": sl_payload,
            "trailing_note": "Upstox regular equity orders do not trail natively: modify the SL-M trigger_price upward via modify_order as `exit.py trail` raises current_stop."}


def _is_ready(order, risk_doc, broker_log, today_str):
    fp = order_fingerprint(order)
    if not risk_doc or risk_doc.get("date") != today_str:
        return False, "NOT READY: run trader-risk today first."
    verdict = None
    for v in risk_doc.get("verdicts", []):
        if v.get("fingerprint") == fp:
            verdict = v
            break
    if not verdict:
        return False, "NOT READY: order changed since last risk check (fingerprint mismatch)."
    if verdict.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(verdict.get("blocks", []))

    # Already decided today?
    for ev in (broker_log or {}).get("events", []):
        if ev.get("date") == today_str and ev.get("fingerprint") == fp and ev.get("type") in ("placed", "paper_fill", "declined"):
            return False, "ALREADY %s today." % ev["type"].upper()
    return True, "READY: trader-risk PASS today, waiting for your yes."


def _append_broker_event(event):
    doc = N.read_json(DEFAULT_BROKER_STATE, {"events": []}) or {"events": []}
    events = list(doc.get("events") or [])
    events.append(event)
    N.write_json(DEFAULT_BROKER_STATE, {"events": events})


def _update_portfolio(sym, side, qty, price, cfg, now):
    pf = N.read_json(DEFAULT_PORTFOLIO, None)
    if pf is None:
        pf = {"cash": float(cfg.get("budget") or 100000.0), "positions": {}}
    cash = float(pf.get("cash") or 0.0)
    positions = dict(pf.get("positions") or {})
    cur = dict(positions.get(sym) or {"qty": 0, "avg_price": 0.0})
    if side == "buy":
        total_cost = cur["qty"] * cur["avg_price"] + qty * price
        new_qty = cur["qty"] + qty
        positions[sym] = {"qty": new_qty, "avg_price": round(total_cost / new_qty, 2) if new_qty else 0.0}
        cash -= qty * price
    else:
        new_qty = max(0, cur["qty"] - qty)
        if new_qty == 0:
            positions.pop(sym, None)
        else:
            positions[sym] = {"qty": new_qty, "avg_price": cur["avg_price"]}
        cash += qty * price
    pf = {"cash": round(cash, 2), "positions": positions, "updated_at": now.isoformat()}
    N.write_json(DEFAULT_PORTFOLIO, pf)
    return pf


def _validate_yes(reply, order):
    txt = str(reply or "").strip().lower()
    words = set(re.findall(r"[a-z']+", txt))
    if not txt:
        return False, "Empty reply."
    if words & BATCH_WORDS:
        return False, "Refused batch reply (%s): approve one order at a time." % ", ".join(sorted(words & BATCH_WORDS))
    if words & DOUBT_WORDS:
        return False, "Refused ambiguous/negative reply."
    if not any(txt == y or txt.startswith(y + " ") for y in YES_WORDS):
        return False, "Reply must start with 'yes' (not %r)." % reply
    # If reply mentions a ticker or qty, ensure it matches
    sym = N.norm_symbol(order["symbol"]).lower()
    other_syms = [w for w in re.findall(r"\b[A-Z]{2,12}\b", str(reply)) if w.lower() not in ("YES", "BUY", "SELL", "NSE", "MIS")]
    if other_syms and sym.upper() not in [s.upper() for s in other_syms]:
        return False, "Reply named %s instead of %s." % (other_syms, sym.upper())
    return True, "ok"


def cmd_show(args, cfg):
    orders_doc = N.read_json(args.orders or DEFAULT_ORDERS)
    if not orders_doc or not orders_doc.get("orders"):
        raise TicketError("No orders in %s." % (args.orders or DEFAULT_ORDERS))
    now = N.parse_now(args.now) if args.now else N.now_ist()
    risk_doc = N.read_json(DEFAULT_RISK_STATE)
    broker_log = N.read_json(DEFAULT_BROKER_STATE)
    broker_name = args.broker or cfg.get("broker") or "upstox"
    paper = bool(cfg.get("paper", True))

    isin_map = N.read_json("trader/instruments-upstox.json", {})
    angel_toks = N.read_json("trader/instruments-angelone.json", {})

    tickets = []
    for idx, ord_d in enumerate(orders_doc["orders"], 1):
        if args.index and idx != args.index:
            continue
        ready, reason = _is_ready(ord_d, risk_doc, broker_log, now.date().isoformat())
        payload = build_broker_payload(ord_d, broker_name, isin_map=isin_map, angel_tokens=angel_toks)
        tickets.append({
            "index": idx, "ready": ready, "status_msg": reason,
            "paper": paper, "broker": broker_name, "order": ord_d,
            "payload": payload, "fingerprint": order_fingerprint(ord_d),
        })

    if args.json:
        print(json.dumps(tickets, indent=2))
        return 0

    for t in tickets:
        o = t["order"]
        mode = "PAPER" if t["paper"] else "LIVE"
        print("ORDER %d (%s, %s | NSE %s)" % (t["index"], mode, t["broker"].upper(), o.get("product", "MIS")))
        print("  %s %d %s (%s) @ %s | cost %s"
              % (o["side"].upper(), o["qty"], o["symbol"], o.get("order_type", "market"),
                 N.inr(o.get("entry")), N.inr(o.get("cost_inr"))))
        if o.get("stop") is not None:
            tr = o.get("trailing") or {}
            print("  Initial SL %s | Trail: +%g%% arms, %g%% below peak | Max loss %s (incl. charges)"
                  % (N.inr(o["stop"]), tr.get("activate_pct", 0.5), tr.get("trail_pct", 0.6), N.inr(o.get("max_loss_inr"))))
        if o.get("target") is not None:
            print("  Target %s | Cutoff %s IST" % (N.inr(o["target"]), o.get("flat_by", "15:20")))
        print("  " + t["status_msg"])
        print("  Payload: " + json.dumps(t["payload"]["entry_order"]))
    return 0


def cmd_approve(args, cfg):
    orders_doc = N.read_json(args.orders or DEFAULT_ORDERS)
    if not orders_doc or len(orders_doc.get("orders", [])) < args.index:
        raise TicketError("Order #%d not found." % args.index)
    ord_d = orders_doc["orders"][args.index - 1]
    now = N.parse_now(args.now) if args.now else N.now_ist()
    ready, reason = _is_ready(ord_d, N.read_json(DEFAULT_RISK_STATE), N.read_json(DEFAULT_BROKER_STATE), now.date().isoformat())
    if not ready:
        raise TicketError("Not recorded: " + reason)
    ok, msg = _validate_yes(args.reply, ord_d)
    if not ok:
        raise TicketError("Not recorded: " + msg)
    _append_broker_event({
        "type": "approved", "date": now.date().isoformat(), "at": now.isoformat(),
        "symbol": ord_d["symbol"], "side": ord_d["side"], "qty": ord_d["qty"],
        "fingerprint": order_fingerprint(ord_d), "reply": args.reply,
    })
    print("Recorded your yes for #%d: %s %d %s." % (args.index, ord_d["side"].upper(), ord_d["qty"], ord_d["symbol"]))
    return 0


def cmd_decline(args, cfg):
    orders_doc = N.read_json(args.orders or DEFAULT_ORDERS)
    ord_d = orders_doc["orders"][args.index - 1]
    now = N.parse_now(args.now) if args.now else N.now_ist()
    _append_broker_event({
        "type": "declined", "date": now.date().isoformat(), "at": now.isoformat(),
        "symbol": ord_d["symbol"], "fingerprint": order_fingerprint(ord_d), "note": args.note or "",
    })
    print("Declined #%d (%s)." % (args.index, ord_d["symbol"]))
    return 0


def cmd_result(args, cfg):
    orders_doc = N.read_json(args.orders or DEFAULT_ORDERS)
    ord_d = orders_doc["orders"][args.index - 1]
    now = N.parse_now(args.now) if args.now else N.now_ist()
    _append_broker_event({
        "type": args.status, "date": now.date().isoformat(), "at": now.isoformat(),
        "symbol": ord_d["symbol"], "side": ord_d["side"], "qty": args.filled_qty or ord_d["qty"],
        "price": args.filled_price or ord_d.get("entry"), "broker_id": args.broker_id,
        "max_loss_inr": ord_d.get("max_loss_inr", 0.0), "fingerprint": order_fingerprint(ord_d),
        "note": args.note or "",
    })
    if args.status == "placed" and args.filled_qty and args.filled_price:
        _update_portfolio(ord_d["symbol"], ord_d["side"], int(args.filled_qty), float(args.filled_price), cfg, now)
    print("Logged %s for #%d %s." % (args.status, args.index, ord_d["symbol"]))
    return 0


def cmd_paper_fill(args, cfg):
    if not cfg.get("paper", True):
        raise TicketError("Refused: paper-fill only runs when 'paper' is true in trader/config.json.")
    orders_doc = N.read_json(args.orders or DEFAULT_ORDERS)
    if not orders_doc or len(orders_doc.get("orders", [])) < args.index:
        raise TicketError("Order #%d not found." % args.index)
    ord_d = orders_doc["orders"][args.index - 1]
    now = N.parse_now(args.now) if args.now else N.now_ist()
    risk_doc = N.read_json(DEFAULT_RISK_STATE)
    ready, reason = _is_ready(ord_d, risk_doc, N.read_json(DEFAULT_BROKER_STATE), now.date().isoformat())
    if not ready:
        raise TicketError("Refused paper-fill: " + reason)

    fill_px = float(args.price if args.price is not None else ord_d["entry"])
    qty = int(ord_d["qty"])
    sym = N.norm_symbol(ord_d["symbol"])

    fills_doc = N.read_json(DEFAULT_PAPER_FILLS, {"fills": []}) or {"fills": []}
    fills = list(fills_doc.get("fills") or [])
    fill_rec = {
        "date": now.date().isoformat(), "at": now.isoformat(), "symbol": sym,
        "exchange": "NSE", "product": ord_d.get("product", "MIS"), "side": ord_d["side"],
        "qty": qty, "price": fill_px, "stop": ord_d.get("stop"), "target": ord_d.get("target"),
        "trailing": ord_d.get("trailing"), "max_loss_inr": ord_d.get("max_loss_inr", 0.0),
        "fingerprint": order_fingerprint(ord_d),
    }
    fills.append(fill_rec)
    N.write_json(DEFAULT_PAPER_FILLS, {"fills": fills})

    _append_broker_event({
        "type": "paper_fill", "date": now.date().isoformat(), "at": now.isoformat(),
        "symbol": sym, "side": ord_d["side"], "qty": qty, "price": fill_px,
        "max_loss_inr": ord_d.get("max_loss_inr", 0.0), "fingerprint": order_fingerprint(ord_d),
    })
    _update_portfolio(sym, ord_d["side"], qty, fill_px, cfg, now)
    if args.json:
        print(json.dumps(fill_rec, indent=2))
    else:
        print("PAPER FILL recorded: %s %d %s @ %s -> %s" % (ord_d["side"].upper(), qty, sym, N.inr(fill_px), DEFAULT_PAPER_FILLS))
    return 0


def cmd_fill(args, cfg):
    now = N.parse_now(args.now) if args.now else N.now_ist()
    sym = N.norm_symbol(args.symbol)
    pf = _update_portfolio(sym, args.side, args.qty, args.price, cfg, now)
    _append_broker_event({
        "type": "fill", "date": now.date().isoformat(), "at": now.isoformat(),
        "symbol": sym, "side": args.side, "qty": args.qty, "price": args.price,
    })
    print("Updated portfolio: %s %d %s @ %s (cash %s)." % (args.side.upper(), args.qty, sym, N.inr(args.price), N.inr(pf["cash"])))
    return 0


def cmd_flatten(args, cfg):
    now = N.parse_now(args.now) if args.now else N.now_ist()
    sym = N.norm_symbol(args.symbol)
    qty = args.qty
    if qty is None:
        pf = N.read_json(DEFAULT_PORTFOLIO, {}) or {}
        qty = int((pf.get("positions") or {}).get(sym, {}).get("qty", 0))
    if not qty or qty <= 0:
        raise TicketError("No open shares for %s (pass --qty)." % sym)
    order = {
        "symbol": sym, "exchange": "NSE", "product": cfg.get("product", "MIS"),
        "side": "sell", "qty": int(qty), "order_type": "market", "entry": None,
        "stop": None, "target": None, "why": "Square off %s before safety cutoff (%s IST)." % (sym, cfg.get("safety_cutoff", "15:20")),
    }
    out = {"created_by": "trader-broker", "created_at": now.isoformat(), "market": "NSE",
           "currency": "INR", "paper": bool(cfg.get("paper", True)), "orders": [order]}
    N.write_json(DEFAULT_ORDERS, out)
    print("Wrote flatten ticket: SELL %d %s (MIS market) -> %s. Run trader-risk next." % (qty, sym, DEFAULT_ORDERS))
    return 0


def cmd_log(args, cfg):
    doc = N.read_json(DEFAULT_BROKER_STATE, {"events": []}) or {"events": []}
    if args.json:
        print(json.dumps(doc, indent=2))
        return 0
    for ev in doc.get("events", []):
        print("  %s  %-10s %-10s %s" % (ev.get("at", "")[:19], ev.get("type", ""), ev.get("symbol", ""), ev.get("qty", "")))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Order Tickets & Broker Ledger (trader-broker)")
    sub = p.add_subparsers(dest="cmd")

    ss = sub.add_parser("show", help="Render tickets and broker payloads")
    ss.add_argument("--orders", default=DEFAULT_ORDERS)
    ss.add_argument("--index", type=int)
    ss.add_argument("--broker", choices=("upstox", "angelone"))
    ss.add_argument("--now")
    ss.add_argument("--json", action="store_true")

    sa = sub.add_parser("approve", help="Record explicit yes for one order")
    sa.add_argument("--orders", default=DEFAULT_ORDERS)
    sa.add_argument("--index", type=int, required=True)
    sa.add_argument("--reply", required=True)
    sa.add_argument("--now")

    sd = sub.add_parser("decline", help="Record no for one order")
    sd.add_argument("--orders", default=DEFAULT_ORDERS)
    sd.add_argument("--index", type=int, required=True)
    sd.add_argument("--note")
    sd.add_argument("--now")

    sr = sub.add_parser("result", help="Log broker placement outcome")
    sr.add_argument("--orders", default=DEFAULT_ORDERS)
    sr.add_argument("--index", type=int, required=True)
    sr.add_argument("--status", required=True, choices=("placed", "denied", "failed"))
    sr.add_argument("--broker-id")
    sr.add_argument("--filled-qty", type=int)
    sr.add_argument("--filled-price", type=float)
    sr.add_argument("--note")
    sr.add_argument("--now")

    sp = sub.add_parser("paper-fill", help="Simulate paper fill into trader/paper_fills.json (paper: true only)")
    sp.add_argument("--orders", default=DEFAULT_ORDERS)
    sp.add_argument("--index", type=int, default=1)
    sp.add_argument("--price", type=float)
    sp.add_argument("--now")
    sp.add_argument("--json", action="store_true")

    sf = sub.add_parser("fill", help="Record fill in portfolio")
    sf.add_argument("--symbol", required=True)
    sf.add_argument("--side", required=True, choices=("buy", "sell"))
    sf.add_argument("--qty", type=int, required=True)
    sf.add_argument("--price", type=float, required=True)
    sf.add_argument("--now")

    sx = sub.add_parser("flatten", help="Write square-off sell order")
    sx.add_argument("--symbol", required=True)
    sx.add_argument("--qty", type=int)
    sx.add_argument("--now")

    sl = sub.add_parser("log", help="Print decision log")
    sl.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 1
    try:
        cfg = N.load_config(DEFAULT_CONFIG)
        return {
            "show": cmd_show, "approve": cmd_approve, "decline": cmd_decline,
            "result": cmd_result, "paper-fill": cmd_paper_fill, "fill": cmd_fill,
            "flatten": cmd_flatten, "log": cmd_log,
        }[args.cmd](args, cfg)
    except N.NSEError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
