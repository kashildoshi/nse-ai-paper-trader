#!/usr/bin/env python3
"""Time Machine for trader-timemachine (NSE India edition). Standard library only.

Replays a planned NSE intraday trade 10,000 times from the stock's own recent
5-minute bars (09:15-15:30 IST), using the exact same exit rules as trader-exit:
  1. Initial hard stop
  2. Ratcheting trailing stop (arms at +0.5% above entry, trails 0.6% below high_water, only moves up)
  3. Optional take-profit target
  4. 15:20 IST safety cutoff (exits at the 15:20 bar close)

    py timemachine.py run                              # trader/plan.json, 10,000 replays, seed 19
    py timemachine.py run --write                      # save trader/timemachine.json
    py timemachine.py run --symbol RELIANCE --date 2026-11-18 --entry 2462.50 --stop 2445.00 --target 2497.50 --entry-time "10:15 IST"
    py timemachine.py chart --from trader/timemachine.json

Simulations built from recent bars are a reality check, not a prediction.
"""

import argparse
import datetime
import json
import math
import os
import random
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as N

DEFAULT_PLAN = "trader/plan.json"
DEFAULT_OUT = "trader/timemachine.json"
DEFAULT_CONFIG = "trader/config.json"
SESSION_BARS = 75       # 09:15 to 15:30 IST in 5-min bars (bars 0..74)
OPEN_MIN = 9 * 60 + 15
CUTOFF_MIN = 15 * 60 + 20
MIN_SESSIONS = 20
SAMPLE_PATHS = 160


class TimeMachineError(N.NSEError):
    pass


def _slot_of(dt):
    dt = N.to_ist(dt)
    mins = dt.hour * 60 + dt.minute - OPEN_MIN
    if mins < 0 or mins % 5 != 0:
        return None
    s = mins // 5
    return s if 0 <= s < SESSION_BARS else None


def _cutoff_slot(until_str="15:20"):
    t = N.hhmm(until_str or N.SAFETY_CUTOFF)
    mins = t.hour * 60 + t.minute - OPEN_MIN
    # bar closing at `until` starts at `until - 5 min`; slot index of last completed bar before/at cutoff
    s = max(1, min(SESSION_BARS - 1, (mins // 5) - 1))
    return s


def _load_60d_bars(sym, feed="yahoo", offline=None, cfg=None):
    if offline:
        doc = json.loads(N.read_maybe_gz(offline))
        if isinstance(doc, dict) and sym in doc and "chart" not in doc:
            doc = doc[sym]
        if isinstance(doc, list):
            out = []
            for b in doc:
                dt = b["t"] if isinstance(b["t"], datetime.datetime) else N.parse_now(str(b["t"]))
                out.append({"t": N.to_ist(dt), "o": float(b["o"]), "h": float(b["h"]),
                            "l": float(b["l"]), "c": float(b["c"]), "v": int(b.get("v", 0))})
            out.sort(key=lambda x: x["t"])
            return out
        if isinstance(doc, dict) and "chart" in doc:
            bars, _ = N.bars_from_yahoo(doc, 5, drop_partial_at=None)
            return bars
        if isinstance(doc, dict) and "bars" in doc:
            return _load_60d_bars(sym, feed=feed, offline=None, cfg=None) if False else [
                {"t": N.to_ist(N.parse_now(str(b["t"]))), "o": float(b["o"]), "h": float(b["h"]),
                 "l": float(b["l"]), "c": float(b["c"]), "v": int(b.get("v", 0))} for b in doc["bars"]
            ]
        raise TimeMachineError("Unrecognized offline bar format in %s." % offline)

    if feed == "upstox":
        isin = N.load_upstox_instruments()
        to_d = N.now_ist().date()
        from_d = to_d - datetime.timedelta(days=60)
        doc = N.fetch_upstox_history(sym, 5, to_d.isoformat(), from_d.isoformat(), isin)
        return N.bars_from_upstox(doc, 5)
    if feed in ("angelone", "angel"):
        tokens = N.load_angel_tokens()
        end_dt = N.now_ist()
        start_dt = end_dt - datetime.timedelta(days=60)
        doc = N.fetch_angel_candles(sym, 5, start_dt, end_dt, tokens)
        return N.bars_from_angel(doc, 5)
    doc = N.fetch_yahoo_chart(sym, interval="5m", rng="60d")
    bars, _ = N.bars_from_yahoo(doc, 5, drop_partial_at=None)
    return bars


def _group_sessions(bars, plan_date=None):
    """Group bars into {date: {slot: bar}} keeping full sessions before plan_date."""
    by_day = {}
    for b in bars:
        d = b["t"].date()
        if plan_date is not None and d >= plan_date:
            continue
        s = _slot_of(b["t"])
        if s is None:
            continue
        by_day.setdefault(d, {})[s] = b
    full = {}
    for d, slots in sorted(by_day.items()):
        # Accept >= 70 bars to tolerate minor feed gaps, interpolate missing from prev
        if len(slots) >= 70:
            full[d] = slots
    return full


def _build_pool(sessions):
    """pool[slot] = list of (o/pc, h/pc, l/pc, c/pc) ratios."""
    pool = {s: [] for s in range(1, SESSION_BARS)}
    for d, slots in sorted(sessions.items()):
        prev_c = None
        for s in range(SESSION_BARS):
            b = slots.get(s)
            if b is None:
                continue
            if prev_c and prev_c > 0 and s >= 1:
                pool[s].append((b["o"] / prev_c, b["h"] / prev_c, b["l"] / prev_c, b["c"] / prev_c))
            prev_c = b["c"]
    return pool


def _entry_slot_from_str(entry_time_str):
    if not entry_time_str:
        return 6  # ~09:45 IST
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(entry_time_str))
    if not m:
        return 6
    mins = int(m.group(1)) * 60 + int(m.group(2)) - OPEN_MIN
    # entry_time is the close time of the entry bar, so next bar starts at slot mins // 5
    return max(1, min(SESSION_BARS - 2, mins // 5))


def simulate_trade(bars_5m, entry_price, stop_price, target_price=None, trail_activate_pct=0.5,
                   trail_pct=0.6, start_slot=6, until_str="15:20", replays=10000, seed=19,
                   window=2, plan_date=None, min_sessions=MIN_SESSIONS, cfg=None):
    """Pure simulation engine callable from CLI or nse_autonomous_runner.py.
    Returns structured Monte Carlo + historical replay metrics."""
    entry_p = float(entry_price)
    stop_p = float(stop_price)
    target_p = float(target_price) if target_price is not None else None
    risk = entry_p - stop_p
    if risk <= 0:
        raise TimeMachineError("Stop (%s) must be below entry (%s)." % (stop_p, entry_p))

    sessions = _group_sessions(bars_5m, plan_date=plan_date)
    if len(sessions) < min_sessions:
        raise TimeMachineError("Need at least %d full historical sessions (got %d)." % (min_sessions, len(sessions)))

    pool = _build_pool(sessions)
    end_slot = _cutoff_slot(until_str)
    if start_slot >= end_slot:
        raise TimeMachineError("Entry slot (%d) is at or past cutoff (%s)." % (start_slot, until_str))

    rng = random.Random(int(seed))
    counts = {"target": 0, "trailed": 0, "stop": 0, "cutoff": 0}
    exit_prices = []
    r_gross_list = []
    r_net_list = []
    net_wins = 0
    gross_green = 0
    sample_paths = []
    hist_paths = {s: [] for s in range(start_slot, end_slot + 1)}

    for r_idx in range(int(replays)):
        px = entry_p
        state = N.trail_init(entry_p, stop_p, activate_pct=trail_activate_pct, trail_pct=trail_pct)
        outcome = "cutoff"
        exit_px = px
        path = [entry_p] if r_idx < SAMPLE_PATHS else None

        for s in range(start_slot, end_slot + 1):
            j_lo = max(1, s - window)
            j_hi = min(SESSION_BARS - 1, s + window)
            cands = []
            for j in range(j_lo, j_hi + 1):
                cands.extend(pool.get(j, ()))
            if not cands:
                # fallback to any populated slot
                for j in range(1, SESSION_BARS):
                    if pool.get(j):
                        cands = pool[j]
                        break
            ro, rh, rl, rc = rng.choice(cands)
            bh = px * rh
            bl = px * rl
            bc = px * rc

            state, ev = N.trail_update(state, bh, bl, bc)
            if ev == "stopped":
                exit_px = state["exit_price"]
                outcome = "trailed" if (state["active"] and state["stop"] > stop_p + 1e-9) else "stop"
                if path is not None:
                    path.append(round(exit_px, 2))
                break
            if target_p is not None and bh >= target_p:
                exit_px = target_p
                outcome = "target"
                if path is not None:
                    path.append(round(exit_px, 2))
                break
            px = bc
            exit_px = bc
            if s in hist_paths and len(hist_paths[s]) < 2000:
                hist_paths[s].append(bc)
            if path is not None:
                path.append(round(bc, 2))

        counts[outcome] += 1
        exit_prices.append(exit_px)
        ch = N.intraday_charges(1, entry_p, exit_px, cfg)["total"]
        gross_diff = exit_px - entry_p
        net_diff = gross_diff - ch
        if gross_diff > 0:
            gross_green += 1
        if net_diff > 0:
            net_wins += 1
        r_gross_list.append(gross_diff / risk)
        r_net_list.append(net_diff / risk)
        if path is not None:
            sample_paths.append(path)

    n = float(replays)
    exit_sorted = sorted(exit_prices)

    def pct(arr, q):
        if not arr:
            return entry_p
        idx = min(len(arr) - 1, max(0, int(q * len(arr))))
        return round(arr[idx], 2)

    # Historical ORB rule replay on the real sessions
    hist_counts = {"fired": 0, "target": 0, "trailed": 0, "stop": 0, "cutoff": 0}
    hist_r_net = []
    for d, slots in sorted(sessions.items()):
        or_bars = [slots[s] for s in (0, 1, 2) if s in slots]
        if len(or_bars) < 3:
            continue
        or_hi = max(b["h"] for b in or_bars)
        or_lo = min(b["l"] for b in or_bars)
        or_mid = N.round_tick((or_hi + or_lo) / 2.0, "down")
        if or_hi <= or_mid:
            continue
        entered_at = None
        for s in range(3, end_slot):
            b = slots.get(s)
            if b and b["c"] > or_hi:
                entered_at = (s + 1, b["c"])
                break
        if not entered_at:
            continue
        hist_counts["fired"] += 1
        e_slot, e_px = entered_at
        h_risk = max(0.05, e_px - or_mid)
        h_tgt = N.round_tick(e_px + 2.0 * h_risk, "nearest") if target_p is not None else None
        st = N.trail_init(e_px, or_mid, activate_pct=trail_activate_pct, trail_pct=trail_pct)
        h_out, h_ex = "cutoff", e_px
        for s in range(e_slot, end_slot + 1):
            b = slots.get(s)
            if not b:
                continue
            st, ev = N.trail_update(st, b["h"], b["l"], b["c"])
            if ev == "stopped":
                h_ex = st["exit_price"]
                h_out = "trailed" if (st["active"] and st["stop"] > or_mid + 1e-9) else "stop"
                break
            if h_tgt is not None and b["h"] >= h_tgt:
                h_ex = h_tgt
                h_out = "target"
                break
            h_ex = b["c"]
        hist_counts[h_out] += 1
        h_ch = N.intraday_charges(1, e_px, h_ex, cfg)["total"]
        hist_r_net.append((h_ex - e_px - h_ch) / h_risk)

    win_prob_pct = round(net_wins / n * 100.0, 2)
    green_pct = round(gross_green / n * 100.0, 2)
    avg_r_gross = round(sum(r_gross_list) / n, 3)
    avg_r_net = round(sum(r_net_list) / n, 3)

    if win_prob_pct >= 60.0 and avg_r_net > 0.1:
        verdict = "Favorable replay odds (net of intraday charges); still a simulation, not a prediction."
    elif win_prob_pct >= 52.0:
        verdict = "Modest edge in simulation (%.1f%% net profitable); sensitive to slippage and gaps." % win_prob_pct
    elif win_prob_pct >= 45.0:
        verdict = "Close to a coin flip (%.1f%% net profitable across %d replays)." % (win_prob_pct, replays)
    else:
        verdict = "Leans negative (%.1f%% net profitable, avg net R %+.3f)." % (win_prob_pct, avg_r_net)

    return {
        "sessions_used": len(sessions),
        "replays": int(replays),
        "seed": int(seed),
        "entry": entry_p,
        "initial_stop": stop_p,
        "target": target_p,
        "trailing": {"activate_pct": trail_activate_pct, "trail_pct": trail_pct},
        "cutoff": until_str,
        "outcomes": {k: round(v / n * 100.0, 2) for k, v in counts.items()},
        "outcome_counts": counts,
        "win_prob_pct": win_prob_pct,
        "green_pct": green_pct,
        "avg_r_gross": avg_r_gross,
        "avg_r_net": avg_r_net,
        "median_exit": pct(exit_sorted, 0.5),
        "p5_exit": pct(exit_sorted, 0.05),
        "p95_exit": pct(exit_sorted, 0.95),
        "verdict": verdict,
        "history": {
            **hist_counts,
            "avg_r_net": round(sum(hist_r_net) / len(hist_r_net), 3) if hist_r_net else 0.0,
        },
        "sample_paths": sample_paths[:30],
    }


def _print_mc(sym, mc):
    print("Time Machine: %s | %d replays (seed %d) over %d NSE 5m sessions | cutoff %s IST"
          % (sym, mc["replays"], mc["seed"], mc["sessions_used"], mc["cutoff"]))
    o = mc["outcomes"]
    print("  target        %6.2f%%" % o["target"])
    print("  trailed SL    %6.2f%%  (activated +%g%%, trailed %g%%)"
          % (o["trailed"], mc["trailing"]["activate_pct"], mc["trailing"]["trail_pct"]))
    print("  initial stop  %6.2f%%" % o["stop"])
    print("  cutoff 15:20  %6.2f%%" % o["cutoff"])
    print("  net win odds  %6.2f%%  (gross green %.2f%%)" % (mc["win_prob_pct"], mc["green_pct"]))
    print("  exit price    median %s (90%% range %s to %s)"
          % (N.inr(mc["median_exit"]), N.inr(mc["p5_exit"]), N.inr(mc["p95_exit"])))
    print("  average R     gross %+.3fR | net (after STT/charges) %+.3fR" % (mc["avg_r_gross"], mc["avg_r_net"]))
    h = mc["history"]
    print("History: ORB rule fired %d times (%d target, %d trailed, %d stop, %d cutoff), avg net R %+.3f."
          % (h["fired"], h["target"], h["trailed"], h["stop"], h["cutoff"], h["avg_r_net"]))
    print("Verdict: %s" % mc["verdict"])
    print("Note: Built from past 5m bars; not a prediction. Gaps, news, or NSE circuits can behave differently.")


def cmd_run(args, cfg):
    plan = N.read_json(args.plan) if not args.symbol else None
    if args.symbol:
        sym = N.norm_symbol(args.symbol)
        entry_p = float(args.entry)
        stop_p = float(args.stop)
        target_p = float(args.target) if args.target is not None else None
        date_str = args.date or N.now_ist().date().isoformat()
        entry_time = args.entry_time or "10:15 IST"
        act_pct = float(cfg.get("trail_activate_pct", 0.5))
        tr_pct = float(cfg.get("trail_pct", 0.6))
    else:
        plan = N.read_json(args.plan or DEFAULT_PLAN)
        if not plan:
            raise TimeMachineError("No %s found: run trader-exit first or pass --symbol/--entry/--stop."
                                   % (args.plan or DEFAULT_PLAN))
        sym = N.norm_symbol(plan["symbol"])
        entry_p = float(plan["entry"])
        stop_p = float(plan["stop"])
        target_p = plan.get("target")
        date_str = plan.get("date") or N.now_ist().date().isoformat()
        entry_time = plan.get("entry_time") or "10:15 IST"
        tr = plan.get("trailing") or {}
        act_pct = float(tr.get("activate_pct", cfg.get("trail_activate_pct", 0.5)))
        tr_pct = float(tr.get("trail_pct", cfg.get("trail_pct", 0.6)))

    plan_date = datetime.date.fromisoformat(date_str)
    feed = args.feed or cfg.get("data_feed") or "yahoo"
    bars = _load_60d_bars(sym, feed=feed, offline=args.offline, cfg=cfg)
    until_str = args.until or (plan.get("safety_cutoff") if plan else None) or cfg.get("safety_cutoff", N.SAFETY_CUTOFF)
    mc = simulate_trade(
        bars, entry_p, stop_p, target_price=target_p, trail_activate_pct=act_pct, trail_pct=tr_pct,
        start_slot=_entry_slot_from_str(entry_time), until_str=until_str,
        replays=args.replays, seed=args.seed, window=args.window, plan_date=plan_date,
        min_sessions=args.min_sessions, cfg=cfg,
    )
    out = {"symbol": sym, "date": date_str, "mc": mc}
    if args.write:
        N.write_json(DEFAULT_OUT, out)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        _print_mc(sym, mc)
    return 0


def cmd_chart(args, cfg):
    doc = N.read_json(args.from_file or DEFAULT_OUT)
    if not doc or "mc" not in doc:
        raise TimeMachineError("No %s found: run `timemachine.py run --write` first." % (args.from_file or DEFAULT_OUT))
    mc = doc["mc"]
    print("Outcome distribution for %s (%d replays, seed %d):" % (doc["symbol"], mc["replays"], mc["seed"]))
    for k in ("target", "trailed", "stop", "cutoff"):
        pct = mc["outcomes"][k]
        bars = "#" * int(round(pct / 2.0))
        print("  %-10s %6.2f%%  %s" % (k, pct, bars))
    print("  Median %s | 5th-95th: %s .. %s" % (N.inr(mc["median_exit"]), N.inr(mc["p5_exit"]), N.inr(mc["p95_exit"])))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Time Machine (trader-timemachine)")
    sub = p.add_subparsers(dest="cmd")

    sr = sub.add_parser("run", help="Run Monte Carlo + historical replay")
    sr.add_argument("--plan", default=DEFAULT_PLAN)
    sr.add_argument("--symbol")
    sr.add_argument("--date")
    sr.add_argument("--entry", type=float)
    sr.add_argument("--stop", type=float)
    sr.add_argument("--target", type=float)
    sr.add_argument("--entry-time")
    sr.add_argument("--until", help="Safety cutoff time IST (default 15:20)")
    sr.add_argument("--replays", type=int, default=10000)
    sr.add_argument("--seed", type=int, default=19)
    sr.add_argument("--window", type=int, default=2)
    sr.add_argument("--min-sessions", type=int, default=MIN_SESSIONS)
    sr.add_argument("--feed", choices=("yahoo", "upstox", "angelone"))
    sr.add_argument("--offline", help="Saved 60d 5m chart JSON")
    sr.add_argument("--now", help="Pin IST clock")
    sr.add_argument("--write", action="store_true")
    sr.add_argument("--json", action="store_true")

    sc = sub.add_parser("chart", help="ASCII chart from saved trader/timemachine.json")
    sc.add_argument("--from", dest="from_file", default=DEFAULT_OUT)

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 1
    try:
        cfg = N.load_config(DEFAULT_CONFIG)
        if args.cmd == "run":
            return cmd_run(args, cfg)
        return cmd_chart(args, cfg)
    except N.NSEError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
