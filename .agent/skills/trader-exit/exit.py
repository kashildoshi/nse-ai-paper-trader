#!/usr/bin/env python3
"""Exit Planner for trader-exit (NSE India edition). Standard library only. Long side only.

Replaces fixed end-of-day exits with a ratcheting Trailing Stop-Loss plus a
15:20 IST safety cutoff (ahead of broker MIS auto-square-off).

    py exit.py plan --write                                # from trader/entry.json
    py exit.py plan --stop or-mid --target 2R --write      # ORB midpoint stop, 2R optional target
    py exit.py plan --symbol RELIANCE --entry 2462.5 --setup bull_flag --pattern-low 2448.0 --write
    py exit.py trail --high 2476.0                         # update trailing stop from manual high
    py exit.py trail --offline bars.json --now "2026-11-18 11:30"
    py exit.py show                                        # print current trader/plan.json

Trailing stop rule (default):
  - Initial hard stop comes from the pattern (or-mid, or-low, pattern-low, pct:X, atr:K, price:X)
    rounded DOWN to the NSE tick (N.tick_size).
  - Once price reaches +0.5% above entry (--trail-activate 0.5), the trailing stop arms and
    trails 0.6% below high_water (--trail-pct 0.6), rounded DOWN to the NSE tick.
  - Ratchets UP only; never moves down.
  - Safety cutoff at 15:20 IST: square off anything still open.

Exit codes: plan 0 = valid plan, 1 = refused/error; trail 0 = updated/ok, 1 = error; show 0 = exists.
"""

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as N

DEFAULT_ENTRY = "trader/entry.json"
DEFAULT_PLAN = "trader/plan.json"
DEFAULT_CONFIG = "trader/config.json"
NOTE = "A stop is a plan, not a guarantee: a gap or circuit limit on NSE can fill past it."


class ExitError(N.NSEError):
    pass


def _load_bars_for_atr(sym, entry_dt, offline, feed, cfg):
    if offline:
        doc = json.loads(N.read_maybe_gz(offline))
        if isinstance(doc, dict) and sym in doc and "chart" not in doc:
            doc = doc[sym]
        if isinstance(doc, list):
            bars = [{"t": N.to_ist(b["t"] if isinstance(b["t"], datetime.datetime) else N.parse_now(str(b["t"]))),
                     "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"]),
                     "v": int(b.get("v", 0))} for b in doc]
        elif isinstance(doc, dict) and "chart" in doc:
            bars, _ = N.bars_from_yahoo(doc, 5, drop_partial_at=entry_dt)
        elif isinstance(doc, dict) and "bars" in doc:
            bars = [{"t": N.to_ist(N.parse_now(str(b["t"]))), "o": float(b["o"]), "h": float(b["h"]),
                     "l": float(b["l"]), "c": float(b["c"]), "v": int(b.get("v", 0))} for b in doc["bars"]]
        else:
            raise ExitError("Unrecognized offline bars format in %s." % offline)
    else:
        bars, _ = N.get_intraday_bars(sym, entry_dt.date(), interval_min=5, feed=feed, now=entry_dt, cfg=cfg)
    return [b for b in bars if b["t"] <= entry_dt]


def _atr(bars, period=14):
    if len(bars) < 2:
        if not bars:
            raise ExitError("Need intraday bars for an atr:K stop.")
        return max(0.01, bars[0]["h"] - bars[0]["l"]), len(bars)
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    sample = trs[-period:]
    return sum(sample) / len(sample), len(sample)


def build_plan(symbol, entry, setup="orb", date_str=None, entry_time_str=None, entry_bar=None,
               interval="5m", or_high=None, or_low=None, pattern_low=None, stop_rule=None,
               target_rule="2R", trail_activate=None, trail_pct=None, flat_by=None,
               cfg=None, offline=None, feed=None):
    cfg = cfg or N.load_config(DEFAULT_CONFIG)
    sym = N.norm_symbol(symbol)
    entry_p = N.round_tick(float(entry), "nearest")
    setup_l = (setup or "orb").lower()

    act_pct = float(trail_activate if trail_activate is not None else cfg.get("trail_activate_pct", 0.5))
    tr_pct = float(trail_pct if trail_pct is not None else cfg.get("trail_pct", 0.6))
    cutoff_str = flat_by or cfg.get("safety_cutoff") or cfg.get("flat_by") or N.SAFETY_CUTOFF
    cutoff_t = N.hhmm(cutoff_str)
    no_after_t = N.config_time(cfg, "no_entries_after", N.NO_ENTRIES_AFTER)

    today = N.now_ist().date()
    d_str = date_str or today.isoformat()
    d_obj = datetime.date.fromisoformat(d_str)

    # Parse entry time if provided
    entry_t = None
    if entry_time_str:
        m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(entry_time_str))
        if m:
            entry_t = datetime.time(int(m.group(1)), int(m.group(2)))
            if entry_t >= cutoff_t:
                raise ExitError("Refused: entry time %s is at or after safety cutoff %s IST."
                                % (entry_t.strftime("%H:%M"), cutoff_t.strftime("%H:%M")))
            if entry_t > no_after_t:
                raise ExitError("Refused: entry time %s is after no_entries_after %s IST."
                                % (entry_t.strftime("%H:%M"), no_after_t.strftime("%H:%M")))

    # Determine initial stop rule
    rule = stop_rule
    if not rule:
        if setup_l == "orb":
            rule = "or-mid"
        elif setup_l in ("bull_flag", "triangle", "vwap_reclaim"):
            rule = "pattern-low"
        else:
            raise ExitError("Setup %r needs an explicit --stop (e.g. price:X, pct:1.0, atr:1.5, pattern-low)." % setup_l)

    rule_l = rule.lower().strip()
    stop_detail = ""
    if rule_l == "or-mid":
        if or_high is None or or_low is None:
            raise ExitError("Stop rule 'or-mid' needs opening range high and low.")
        raw_stop = (float(or_high) + float(or_low)) / 2.0
        stop_label = "opening-range midpoint (%s + %s)/2" % (N.inr(or_high), N.inr(or_low))
    elif rule_l == "or-low":
        if or_low is None:
            raise ExitError("Stop rule 'or-low' needs opening range low.")
        raw_stop = float(or_low)
        stop_label = "opening-range low"
    elif rule_l == "pattern-low":
        p_low = pattern_low if pattern_low is not None else or_low
        if p_low is None:
            raise ExitError("Stop rule 'pattern-low' needs pattern_low (from entry.json or --pattern-low).")
        raw_stop = float(p_low)
        stop_label = "pattern invalidation low"
    elif rule_l.startswith("price:"):
        raw_stop = float(rule_l.split(":", 1)[1])
        stop_label = "explicit stop price"
    elif rule_l.startswith("pct:"):
        pct = float(rule_l.split(":", 1)[1])
        if pct <= 0:
            raise ExitError("Stop percent must be positive.")
        raw_stop = entry_p * (1.0 - pct / 100.0)
        stop_label = "%g%% below entry" % pct
    elif rule_l.startswith("atr:"):
        k = float(rule_l.split(":", 1)[1])
        if k <= 0:
            raise ExitError("ATR multiplier must be positive.")
        t_use = entry_t or N.now_ist().time()
        entry_dt = datetime.datetime.combine(d_obj, t_use, N.IST)
        bars = _load_bars_for_atr(sym, entry_dt, offline, feed or cfg.get("data_feed") or "yahoo", cfg)
        atr_val, used_n = _atr(bars, 14)
        raw_stop = entry_p - k * atr_val
        stop_label = "%.2fx ATR(%d)" % (k, used_n)
        if used_n < 14:
            stop_detail = "ATR averaged %d available bars (< 14)." % used_n
    else:
        raise ExitError("Unknown stop rule %r (use or-mid, or-low, pattern-low, price:X, pct:X, atr:K)." % rule)

    stop_p = N.round_tick(raw_stop, "down")
    if stop_p >= entry_p:
        raise ExitError("Refused: stop %s is at or above entry %s (long side only)."
                        % (N.inr(stop_p), N.inr(entry_p)))

    risk = round(entry_p - stop_p, 2)
    min_risk = max(entry_p * 0.0005, 2.0 * N.tick_size(entry_p))
    if risk < min_risk - 1e-9:
        raise ExitError("Refused: risk %s is too tight (< %s, max of 0.05%% or 2 NSE ticks)."
                        % (N.inr(risk), N.inr(min_risk)))

    t_rule_str = (target_rule or "2R").strip()
    t_rule_l = t_rule_str.lower()
    if t_rule_l == "none":
        target_p = None
        ref_target = N.round_tick(entry_p + 2.0 * risk, "nearest")
        target_r = round((ref_target - entry_p) / risk, 2)
        t_label = "none (trailing stop only; 2R reference %s)" % N.inr(ref_target)
        rr = target_r
    elif t_rule_l.startswith("price:"):
        target_p = N.round_tick(float(t_rule_l.split(":", 1)[1]), "nearest")
        if target_p <= entry_p:
            raise ExitError("Refused: target %s must be above entry %s." % (N.inr(target_p), N.inr(entry_p)))
        ref_target = target_p
        rr = round((target_p - entry_p) / risk, 2)
        target_r = rr
        t_label = "explicit target price (%s)" % N.inr(target_p)
    elif t_rule_l.endswith("r"):
        r_mult = float(t_rule_l[:-1])
        if r_mult <= 0:
            raise ExitError("Target R multiple must be positive.")
        target_p = N.round_tick(entry_p + r_mult * risk, "nearest")
        ref_target = target_p
        rr = round((target_p - entry_p) / risk, 2)
        target_r = r_mult
        t_label = "%gR (%g times initial risk)" % (r_mult, r_mult)
    else:
        raise ExitError("Unknown target %r (use 2R, 1R, 1.5R, price:X, or none)." % target_rule)

    tr_state = N.trail_init(entry_p, stop_p, activate_pct=act_pct, trail_pct=tr_pct)
    trailing_block = {
        "activate_pct": tr_state["activate_pct"],
        "trail_pct": tr_state["trail_pct"],
        "activate_at": tr_state["activate_at"],
        "high_water": tr_state["high_water"],
        "current_stop": tr_state["stop"],
        "active": False,
        "updated_at": None,
    }

    ch_stop = N.intraday_charges(1, entry_p, stop_p, cfg)
    ch_tgt = N.intraday_charges(1, entry_p, ref_target, cfg)
    be_p = N.breakeven_price(1, entry_p, cfg)
    be_move = round((be_p - entry_p), 2) if be_p is not None else 0.0

    plan = {
        "symbol": sym,
        "market": "NSE",
        "currency": "INR",
        "date": d_str,
        "setup": setup_l,
        "entry": entry_p,
        "entry_time": entry_time_str or N.fmt_time(N.now_ist()),
        "entry_bar": entry_bar,
        "interval": interval,
        "opening_range": {
            "high": round(float(or_high), 2) if or_high is not None else None,
            "low": round(float(or_low), 2) if or_low is not None else None,
            "minutes": 15,
        },
        "stop": stop_p,
        "stop_rule": rule_l,
        "stop_label": stop_label,
        "stop_detail": stop_detail,
        "target": target_p,
        "target_r": float(target_r),
        "target_label": t_label,
        "risk_per_share": risk,
        "reward_risk": rr,
        "exit_mode": "trailing",
        "trailing": trailing_block,
        "flat_by": cutoff_t.strftime("%H:%M"),
        "safety_cutoff": cutoff_t.strftime("%H:%M"),
        "charges_estimate": {"at_stop_per_share": ch_stop, "at_target_per_share": ch_tgt},
        "breakeven_move": be_move,
        "created_at": N.now_ist().isoformat(),
        "note": NOTE,
    }
    return plan


def update_trail_from_bars(plan, bars, now=None):
    """Replay bars through N.trail_update and return (updated_plan, status_msg, exit_event)."""
    now = N.to_ist(now) if now else N.now_ist()
    cutoff_t = N.hhmm(plan.get("safety_cutoff") or plan.get("flat_by") or N.SAFETY_CUTOFF)
    tr = dict(plan.get("trailing") or {})
    state = {
        "entry": float(plan["entry"]),
        "initial_stop": float(plan["stop"]),
        "stop": float(tr.get("current_stop", plan["stop"])),
        "high_water": float(tr.get("high_water", plan["entry"])),
        "activate_pct": float(tr.get("activate_pct", 0.5)),
        "trail_pct": float(tr.get("trail_pct", 0.6)),
        "active": bool(tr.get("active", False)),
        "activate_at": float(tr.get("activate_at", N.round_tick(float(plan["entry"]) * 1.005, "up"))),
    }

    post_bars = bars
    entry_t_str = plan.get("entry_time")
    if entry_t_str and plan.get("date"):
        m_et = re.match(r"^\s*(\d{1,2}):(\d{2})", str(entry_t_str))
        if m_et:
            e_dt = datetime.datetime.combine(
                datetime.date.fromisoformat(plan["date"]),
                datetime.time(int(m_et.group(1)), int(m_et.group(2))),
                N.IST,
            )
            post_bars = [b for b in bars if (b["t"] + datetime.timedelta(minutes=5)) > e_dt]

    prev_stop = state["stop"]
    exit_event = None
    for b in post_bars:
        bar_close_dt = b["t"] + datetime.timedelta(minutes=5)
        if bar_close_dt.time() >= cutoff_t:
            exit_event = ("cutoff", float(b["c"]), N.fmt_time(bar_close_dt))
            break
        state, ev = N.trail_update(state, b["h"], b["l"], b["c"])
        if ev == "stopped":
            exit_event = ("stopped", state["exit_price"], N.fmt_time(bar_close_dt))
            break
        if plan.get("target") is not None and b["h"] >= float(plan["target"]):
            exit_event = ("target", float(plan["target"]), N.fmt_time(bar_close_dt))
            break

    if exit_event is None and now.time() >= cutoff_t:
        px = bars[-1]["c"] if bars else state["high_water"]
        exit_event = ("cutoff", float(px), N.fmt_time(now))

    new_plan = dict(plan)
    new_tr = dict(tr)
    new_tr["high_water"] = round(state["high_water"], 2)
    new_tr["current_stop"] = max(float(tr.get("current_stop", plan["stop"])), float(state["stop"]))
    new_tr["active"] = bool(state["active"])
    new_tr["updated_at"] = now.isoformat()
    new_plan["trailing"] = new_tr

    if exit_event:
        kind, px, tm = exit_event
        if kind == "cutoff":
            msg = "SAFETY CUTOFF %s IST: exit %s now (close %s)." % (cutoff_t.strftime("%H:%M"), plan["symbol"], N.inr(px))
        elif kind == "target":
            msg = "TARGET HIT at %s (%s) for %s." % (N.inr(px), tm, plan["symbol"])
        else:
            msg = "STOPPED at %s at %s for %s." % (N.inr(px), tm, plan["symbol"])
    elif new_tr["current_stop"] > prev_stop + 1e-9:
        msg = "Move your stop-loss order to %s (high water %s, trailing active)." % (
            N.inr(new_tr["current_stop"]), N.inr(new_tr["high_water"]))
    else:
        msg = "Hold stop at %s (high water %s, arms at %s)." % (
            N.inr(new_tr["current_stop"]), N.inr(new_tr["high_water"]), N.inr(new_tr["activate_at"]))
    return new_plan, msg, exit_event


def _print_plan(p):
    tr = p.get("trailing") or {}
    tgt = N.inr(p["target"]) if p.get("target") is not None else "trailing-only"
    print("Exit plan: %s long (%s), %s | %s" % (p["symbol"], p["market"], p["date"], p["setup"]))
    print("  Entry          %s at %s" % (N.inr(p["entry"]), p.get("entry_time") or "-"))
    print("  Initial stop   %s  (%s)" % (N.inr(p["stop"]), p.get("stop_label") or p.get("stop_rule")))
    print("  Trailing rule  arms at %s (+%g%%), trails %g%% below high water"
          % (N.inr(tr.get("activate_at")), tr.get("activate_pct", 0.5), tr.get("trail_pct", 0.6)))
    print("  Live trail     current SL %s | high water %s | active=%s"
          % (N.inr(tr.get("current_stop", p["stop"])), N.inr(tr.get("high_water", p["entry"])), tr.get("active", False)))
    print("  Target         %s  (%s)" % (tgt, p.get("target_label") or ""))
    print("  Risk a share   %s (%.2f%% of entry) | Reward:risk %.2f"
          % (N.inr(p["risk_per_share"]), p["risk_per_share"] / p["entry"] * 100.0, p["reward_risk"]))
    print("  Safety cutoff  %s IST: square off anything still open" % (p.get("safety_cutoff") or "15:20"))
    print("  Note           %s" % p.get("note", NOTE))


def cmd_plan(args, cfg):
    if args.entry is not None:
        sym = args.symbol or (N.read_json(DEFAULT_ENTRY, {}) or {}).get("symbol")
        if not sym:
            raise ExitError("Pass --symbol with --entry.")
        setup = args.setup or "level"
        date_str = args.date
        entry_time = args.time
        entry_bar = args.entry_bar
        interval = args.interval or "5m"
        or_high = args.or_high
        or_low = args.or_low
        pat_low = args.pattern_low
        entry_p = args.entry
    else:
        ent = N.read_json(args.from_file or DEFAULT_ENTRY)
        if not ent:
            raise ExitError("No %s found: run trader-entry first or pass --symbol and --entry." % (args.from_file or DEFAULT_ENTRY))
        if ent.get("status") != "triggered":
            raise ExitError("Refused: %s status is %r (not triggered). Wait for trigger or pass --entry."
                            % (args.from_file or DEFAULT_ENTRY, ent.get("status")))
        sym = args.symbol or ent.get("symbol")
        setup = args.setup or ent.get("setup") or "orb"
        date_str = args.date or ent.get("date")
        entry_time = args.time or ent.get("time")
        entry_bar = args.entry_bar if args.entry_bar is not None else ent.get("bar_index")
        interval = args.interval or ent.get("interval") or "5m"
        orr = ent.get("opening_range") or {}
        lvls = ent.get("levels") or {}
        or_high = args.or_high if args.or_high is not None else orr.get("high")
        or_low = args.or_low if args.or_low is not None else orr.get("low")
        pat_low = args.pattern_low if args.pattern_low is not None else lvls.get("pattern_low")
        entry_p = ent.get("entry")

    plan = build_plan(
        sym, entry_p, setup=setup, date_str=date_str, entry_time_str=entry_time,
        entry_bar=entry_bar, interval=interval, or_high=or_high, or_low=or_low,
        pattern_low=pat_low, stop_rule=args.stop, target_rule=args.target,
        trail_activate=args.trail_activate, trail_pct=args.trail_pct, flat_by=args.flat_by,
        cfg=cfg, offline=args.offline, feed=args.feed,
    )
    if args.write:
        N.write_json(DEFAULT_PLAN, plan)
    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        _print_plan(plan)
    return 0


def cmd_trail(args, cfg):
    plan_path = args.plan or DEFAULT_PLAN
    plan = N.read_json(plan_path)
    if not plan:
        raise ExitError("No %s found: run `exit.py plan --write` first." % plan_path)
    now = N.parse_now(args.now) if args.now else N.now_ist()
    if args.high is not None or args.low is not None:
        hi = float(args.high if args.high is not None else plan["trailing"]["high_water"])
        lo = float(args.low) if args.low is not None else None
        fake_bar = {"t": now - datetime.timedelta(minutes=5), "o": hi, "h": hi, "l": lo if lo is not None else hi, "c": hi, "v": 1}
        tr = dict(plan.get("trailing") or {})
        state = {
            "entry": float(plan["entry"]), "initial_stop": float(plan["stop"]),
            "stop": float(tr.get("current_stop", plan["stop"])),
            "high_water": float(tr.get("high_water", plan["entry"])),
            "activate_pct": float(tr.get("activate_pct", 0.5)),
            "trail_pct": float(tr.get("trail_pct", 0.6)),
            "active": bool(tr.get("active", False)),
            "activate_at": float(tr.get("activate_at", 0.0)),
        }
        prev = state["stop"]
        state, ev = N.trail_update(state, fake_bar["h"], lo, fake_bar["c"])
        new_plan = dict(plan)
        new_tr = dict(tr)
        new_tr["high_water"] = round(state["high_water"], 2)
        new_tr["current_stop"] = max(prev, float(state["stop"]))
        new_tr["active"] = bool(state["active"])
        new_tr["updated_at"] = now.isoformat()
        new_plan["trailing"] = new_tr
        exit_ev = ("stopped", state["exit_price"], N.fmt_time(now)) if ev == "stopped" else None
        if now.time() >= N.hhmm(plan.get("safety_cutoff") or N.SAFETY_CUTOFF):
            msg = "SAFETY CUTOFF %s IST: exit %s now." % (plan.get("safety_cutoff") or N.SAFETY_CUTOFF, plan["symbol"])
        elif ev == "stopped":
            msg = "STOPPED at %s at %s for %s." % (N.inr(state["exit_price"]), N.fmt_time(now), plan["symbol"])
        elif new_tr["current_stop"] > prev + 1e-9:
            msg = "Move your stop-loss order to %s (high water %s)." % (N.inr(new_tr["current_stop"]), N.inr(new_tr["high_water"]))
        else:
            msg = "Hold stop at %s (high water %s, arms at %s)." % (
                N.inr(new_tr["current_stop"]), N.inr(new_tr["high_water"]), N.inr(new_tr["activate_at"]))
    else:
        feed = args.feed or cfg.get("data_feed") or "yahoo"
        if args.offline:
            doc = json.loads(N.read_maybe_gz(args.offline))
            if isinstance(doc, list):
                bars = [{"t": N.to_ist(N.parse_now(str(b["t"]))), "o": float(b["o"]), "h": float(b["h"]),
                         "l": float(b["l"]), "c": float(b["c"]), "v": int(b.get("v", 0))} for b in doc]
            elif isinstance(doc, dict) and "chart" in doc:
                bars, _ = N.bars_from_yahoo(doc, 5, drop_partial_at=now)
            elif isinstance(doc, dict) and "bars" in doc:
                bars = [{"t": N.to_ist(N.parse_now(str(b["t"]))), "o": float(b["o"]), "h": float(b["h"]),
                         "l": float(b["l"]), "c": float(b["c"]), "v": int(b.get("v", 0))} for b in doc["bars"]]
            else:
                raise ExitError("Unrecognized offline file format.")
        else:
            day = datetime.date.fromisoformat(plan["date"])
            bars, _ = N.get_intraday_bars(plan["symbol"], day, interval_min=5, feed=feed, now=now, cfg=cfg)
        new_plan, msg, exit_ev = update_trail_from_bars(plan, bars, now=now)

    if not args.no_write:
        N.write_json(plan_path, new_plan)
    if args.json:
        print(json.dumps({"plan": new_plan, "message": msg, "exit_event": exit_ev}, indent=2))
    else:
        print(msg)
    return 0


def cmd_show(args, cfg):
    plan = N.read_json(args.plan or DEFAULT_PLAN)
    if not plan:
        raise ExitError("No %s found." % (args.plan or DEFAULT_PLAN))
    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        _print_plan(plan)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Exit Planner & Trailing Stop Manager (trader-exit)")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("plan", help="Create an exit plan with ratcheting trailing stop")
    sp.add_argument("--from", dest="from_file", default=DEFAULT_ENTRY, help="Entry file (default trader/entry.json)")
    sp.add_argument("--symbol")
    sp.add_argument("--entry", type=float)
    sp.add_argument("--setup")
    sp.add_argument("--date")
    sp.add_argument("--time")
    sp.add_argument("--entry-bar", type=int)
    sp.add_argument("--interval", default="5m")
    sp.add_argument("--or-high", type=float)
    sp.add_argument("--or-low", type=float)
    sp.add_argument("--pattern-low", type=float)
    sp.add_argument("--stop", help="or-mid|or-low|pattern-low|price:X|pct:X|atr:K")
    sp.add_argument("--target", default="2R", help="2R (default), 1R, 1.5R, price:X, or none")
    sp.add_argument("--trail-activate", type=float, help="Activate trailing at +X%% (default 0.5)")
    sp.add_argument("--trail-pct", type=float, help="Trail X%% below high water (default 0.6)")
    sp.add_argument("--flat-by", help="Safety cutoff HH:MM IST (default 15:20)")
    sp.add_argument("--feed", choices=("yahoo", "upstox", "angelone"))
    sp.add_argument("--offline", help="Saved bars JSON for atr:K")
    sp.add_argument("--now", help="Pin IST clock")
    sp.add_argument("--write", action="store_true", help="Write trader/plan.json")
    sp.add_argument("--json", action="store_true")

    st = sub.add_parser("trail", help="Update ratcheting trailing stop from bars or --high")
    st.add_argument("--plan", default=DEFAULT_PLAN)
    st.add_argument("--high", type=float, help="Latest high price (manual mode)")
    st.add_argument("--low", type=float, help="Latest low price (optional)")
    st.add_argument("--feed", choices=("yahoo", "upstox", "angelone"))
    st.add_argument("--offline", help="Saved session bars JSON")
    st.add_argument("--now", help="Pin IST clock")
    st.add_argument("--write", action="store_true", help="Save updated trailing stop (default on)")
    st.add_argument("--no-write", action="store_true", help="Preview without saving trader/plan.json")
    st.add_argument("--json", action="store_true")

    ss = sub.add_parser("show", help="Show current trader/plan.json")
    ss.add_argument("--plan", default=DEFAULT_PLAN)
    ss.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 1
    try:
        cfg = N.load_config(DEFAULT_CONFIG)
        if args.cmd == "plan":
            return cmd_plan(args, cfg)
        if args.cmd == "trail":
            return cmd_trail(args, cfg)
        return cmd_show(args, cfg)
    except N.NSEError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
