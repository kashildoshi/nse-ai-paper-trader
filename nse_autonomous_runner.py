#!/usr/bin/env python3
"""Autonomous NSE Paper Trading Runner (3m Scanner + 10s Trailing SL + 15:20 Cutoff + 15:30 Report).

Orchestrates the 11 converted NSE skills + PaperBroker (SQLite WAL) + TelegramReporter:
  * Every 3 minutes (09:15-15:20 IST): scans liquid NSE stocks (.NS) via yfinance,
    detects ORB, Bull Flag, Triangle & VWAP Reclaim patterns with volume & liquidity
    filters, checks trader-exit + trader-budget + trader-risk + trader-timemachine
    (requires win_prob_pct > 65%), and opens paper positions (+0.03% slippage).
  * Every 10 seconds (09:15-15:20 IST): monitors open positions, ratchets the
    Trailing Stop-Loss (0.6% below peak high_water once profit >= 0.5%), and
    auto-exits on SL/target/15:20 IST safety cutoff (with -0.03% slippage + Indian taxes).
  * At 15:20 IST: safety square-off of all remaining open positions.
  * At 15:30 IST: sends the full Daily Performance Report via Telegram.

    py nse_autonomous_runner.py                  # continuous loop (schedule)
    py nse_autonomous_runner.py --once           # run one scan + monitor cycle
    py nse_autonomous_runner.py --demo-session   # offline full-day simulation into paper_trades.db
"""

import argparse
import datetime
import importlib.util
import math
import os
import random
import sys
import time

import pandas as pd
import schedule
import yfinance as yf

ROOT = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(ROOT, ".agent", "skills")
sys.path.insert(0, os.path.join(ROOT, "shared"))
import nse_common as N
from paper_broker import DEFAULT_DB, INITIAL_CAPITAL, PaperBroker
from telegram_reporter import DEFAULT_OUTBOX, TelegramReporter


def _load_skill(folder, module_name):
    path = os.path.join(SKILLS_DIR, folder, module_name + ".py")
    spec = importlib.util.spec_from_file_location("skill_" + module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scan_mod = _load_skill("trader-scanner", "scan")
exit_mod = _load_skill("trader-exit", "exit")
tm_mod = _load_skill("trader-timemachine", "timemachine")
size_mod = _load_skill("trader-budget", "size")
risk_mod = _load_skill("trader-risk", "check")


def _df_to_bars(df, interval_min=5, drop_partial_at=None):
    if df is None or df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] for c in df.columns]
    bars = []
    for ts, row in df.iterrows():
        dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        dt = N.to_ist(dt)
        if drop_partial_at is not None and interval_min < 1440 and dt + datetime.timedelta(minutes=interval_min) > drop_partial_at:
            continue
        o, h, l, c = row.get("Open"), row.get("High"), row.get("Low"), row.get("Close")
        v = row.get("Volume", 0)
        if any(pd.isna(x) for x in (o, h, l, c)):
            continue
        bars.append({"t": dt, "o": float(o), "h": float(h), "l": float(l),
                     "c": float(c), "v": 0 if pd.isna(v) else int(v)})
    bars.sort(key=lambda b: b["t"])
    return bars


def fetch_bars_yfinance(sym, period="60d", interval="5m", drop_partial_at=None):
    """Fetch .NS bars using yfinance, falling back to nse_common Yahoo chart JSON."""
    ys = N.yahoo_symbol(sym)
    interval_min = 1440 if interval == "1d" else int(interval.rstrip("m"))
    try:
        df = yf.Ticker(ys).history(period=period, interval=interval, auto_adjust=False, prepost=False)
        bars = _df_to_bars(df, interval_min=interval_min, drop_partial_at=drop_partial_at)
        if bars:
            return bars
    except Exception:
        pass
    try:
        doc = N.fetch_yahoo_chart(sym, interval=interval, rng=period)
        bars, _ = N.bars_from_yahoo(doc, interval_min=interval_min, drop_partial_at=drop_partial_at)
        return bars
    except N.NSEError:
        return []


def fetch_latest_tick_yfinance(sym):
    """Fetch latest price, high, low for an open position."""
    ys = N.yahoo_symbol(sym)
    try:
        df = yf.Ticker(ys).history(period="1d", interval="1m", auto_adjust=False, prepost=False)
        if df is not None and not df.empty:
            last = df.iloc[-1]
            return float(last["Close"]), float(last["High"]), float(last["Low"])
    except Exception:
        pass
    try:
        doc = N.fetch_yahoo_chart(sym, interval="1m", rng="1d")
        bars, meta = N.bars_from_yahoo(doc, interval_min=1)
        if bars:
            b = bars[-1]
            return float(b["c"]), float(b["h"]), float(b["l"])
        if meta.get("regularMarketPrice"):
            p = float(meta["regularMarketPrice"])
            return p, p, p
    except Exception:
        pass
    return None


class AutonomousRunner:
    def __init__(self, db_path=DEFAULT_DB, outbox_path=DEFAULT_OUTBOX, min_win_prob=65.0,
                 max_symbols=25, replays=2000, cfg=None, data_provider=None, tick_provider=None):
        self.cfg = dict(cfg or N.load_config(os.path.join(ROOT, "trader", "config.json")))
        if not self.cfg.get("budget"):
            self.cfg["budget"] = INITIAL_CAPITAL
        self.cfg["paper"] = True  # Paper mode enforced for autonomous runner
        self.broker = PaperBroker(db_path=db_path, initial_capital=float(self.cfg["budget"]), cfg=self.cfg)
        self.reporter = TelegramReporter(outbox_path=outbox_path)
        self.min_win_prob = float(min_win_prob)
        self.max_symbols = int(max_symbols)
        self.replays = int(replays)
        self.data_provider = data_provider or (lambda s: fetch_bars_yfinance(s, period="60d", interval="5m"))
        self.tick_provider = tick_provider or fetch_latest_tick_yfinance
        self.last_report_date = None
        self.last_cutoff_date = None
        self.entered_today = set()

    def _universe_symbols(self):
        rows, _ = scan_mod.load_universe(os.path.join(ROOT, "trader", "universe.txt"))
        syms = [r[0] for r in rows]
        return syms[: self.max_symbols] if self.max_symbols > 0 else syms

    def _detect_best_pattern(self, today_bars, min_vol_ratio=1.5):
        detectors = [
            ("orb", lambda b: scan_mod.detect_orb(b, or_minutes=15, min_vol_ratio=min_vol_ratio)),
            ("bull_flag", lambda b: scan_mod.detect_bull_flag(b, min_vol_ratio=min_vol_ratio)),
            ("triangle", lambda b: scan_mod.detect_triangle(b, min_vol_ratio=min_vol_ratio)),
            ("vwap_reclaim", lambda b: scan_mod.detect_vwap_reclaim(b, min_vol_ratio=min_vol_ratio)),
        ]
        for setup_id, fn in detectors:
            hit = fn(today_bars)
            if hit:
                return hit
        return None

    def _build_broker_log_snapshot(self, today_str):
        metrics = self.broker.get_metrics(date_str=today_str)
        events = []
        for p in metrics["open_positions"]:
            events.append({
                "type": "paper_fill", "date": today_str, "symbol": p["symbol"], "side": "buy",
                "max_loss_inr": round((p["entry_price"] - p["initial_stop"]) * p["qty"], 2),
            })
        for t in metrics["closed_trades"]:
            events.append({
                "type": "paper_fill", "date": today_str, "symbol": t["symbol"], "side": "buy",
                "max_loss_inr": round((t["entry_price"] - t["initial_stop"]) * t["qty"], 2),
            })
        return {"events": events}, metrics

    def scan_and_enter_cycle(self, now=None, min_sessions=20):
        now = N.to_ist(now) if now else N.now_ist()
        today = now.date()
        today_str = today.isoformat()
        if not N.is_trading_day(today, self.cfg):
            return {"status": "closed", "entries": []}

        nb = N.config_time(self.cfg, "no_entries_before", N.NO_ENTRIES_BEFORE)
        na = N.config_time(self.cfg, "no_entries_after", N.NO_ENTRIES_AFTER)
        cutoff = N.config_time(self.cfg, "safety_cutoff", N.SAFETY_CUTOFF)
        if now.time() < nb or now.time() > na or now.time() >= cutoff:
            return {"status": "outside_entry_window", "entries": []}

        broker_log, metrics = self._build_broker_log_snapshot(today_str)
        open_syms = {p["symbol"] for p in metrics["open_positions"]}
        portfolio = {
            "cash": metrics["cash"],
            "positions": {p["symbol"]: {"qty": p["qty"], "avg_price": p["entry_price"]} for p in metrics["open_positions"]},
        }
        acc = risk_mod._seed_acc_from_broker_log(broker_log, portfolio, self.cfg, today_str)

        entries = []
        skipped = []
        min_price = float(self.cfg.get("min_price_inr", 50))
        min_turn_cr = float(self.cfg.get("min_avg_turnover_cr", 25))

        for sym in self._universe_symbols():
            if sym in open_syms or (today_str, sym) in self.entered_today:
                continue
            bars_all = self.data_provider(sym)
            if not bars_all:
                continue
            today_bars = [b for b in bars_all if b["t"].date() == today and b["t"] + datetime.timedelta(minutes=5) <= now]
            if not today_bars:
                continue
            ok_liq, _ = scan_mod.passes_liquidity(today_bars, min_price=min_price, min_turnover_cr=min_turn_cr)
            if not ok_liq:
                continue
            hit = self._detect_best_pattern(today_bars, min_vol_ratio=1.5)
            if not hit:
                continue

            lvls = hit.get("levels") or {}
            try:
                plan = exit_mod.build_plan(
                    sym, hit["price"], setup=hit["setup"], date_str=today_str,
                    entry_time_str=hit["time"], entry_bar=hit["bar_index"] + 1,
                    or_high=lvls.get("or_high"), or_low=lvls.get("or_low"),
                    pattern_low=lvls.get("pattern_low"), target_rule="2R",
                    trail_activate=self.cfg.get("trail_activate_pct", 0.5),
                    trail_pct=self.cfg.get("trail_pct", 0.6),
                    flat_by=self.cfg.get("safety_cutoff", N.SAFETY_CUTOFF),
                    cfg=self.cfg,
                )
                mc = tm_mod.simulate_trade(
                    bars_all, plan["entry"], plan["stop"], target_price=plan.get("target"),
                    trail_activate_pct=plan["trailing"]["activate_pct"],
                    trail_pct=plan["trailing"]["trail_pct"],
                    start_slot=tm_mod._entry_slot_from_str(plan["entry_time"]),
                    until_str=plan["safety_cutoff"],
                    replays=self.replays, seed=19, plan_date=today,
                    min_sessions=min_sessions, cfg=self.cfg,
                )
            except N.NSEError as e:
                skipped.append({"symbol": sym, "reason": str(e)})
                continue

            if mc["win_prob_pct"] <= self.min_win_prob:
                skipped.append({"symbol": sym, "setup": hit["setup"],
                                "reason": "win_prob %.1f%% <= %.1f%%" % (mc["win_prob_pct"], self.min_win_prob)})
                continue

            try:
                order, _ = size_mod.size_plan_order(plan, self.cfg, portfolio=portfolio, now=now)
            except N.NSEError as e:
                skipped.append({"symbol": sym, "reason": str(e)})
                continue

            verdict = risk_mod.check_order_dict(order, self.cfg, portfolio=portfolio,
                                              broker_log=broker_log, now=now, state_acc=acc)
            if verdict["verdict"] != "PASS":
                skipped.append({"symbol": sym, "reason": "; ".join(verdict["blocks"])})
                continue

            pos = self.broker.open_position(
                sym, hit["setup"], order["qty"], order["entry"], order["stop"],
                target_price=order.get("target"), win_prob_pct=mc["win_prob_pct"],
                now=now, trail_activate_pct=order["trailing"]["activate_pct"],
                trail_pct=order["trailing"]["trail_pct"],
            )
            self.entered_today.add((today_str, sym))
            open_syms.add(sym)
            portfolio["cash"] -= order["qty"] * pos["entry_price"]
            portfolio["positions"][sym] = {"qty": order["qty"], "avg_price": pos["entry_price"]}
            self.reporter.notify_entry(pos)
            entries.append(pos)

        return {"status": "ok", "entries": entries, "skipped": skipped}

    def monitor_positions_cycle(self, now=None):
        now = N.to_ist(now) if now else N.now_ist()
        open_pos = self.broker.get_open_positions()
        exits = []
        for pos in open_pos:
            tick = self.tick_provider(pos["symbol"])
            if not tick:
                continue
            ltp, hi, lo = tick
            trade = self.broker.update_price_and_check_exit(pos["symbol"], ltp, bar_high=hi, bar_low=lo, now=now)
            if trade:
                self.reporter.notify_exit(trade)
                exits.append(trade)
        return exits

    def safety_square_off_cycle(self, now=None):
        now = N.to_ist(now) if now else N.now_ist()
        d_str = now.date().isoformat()
        if self.last_cutoff_date == d_str:
            return []
        open_pos = self.broker.get_open_positions()
        if not open_pos:
            self.last_cutoff_date = d_str
            return []
        px_map = {}
        for pos in open_pos:
            t = self.tick_provider(pos["symbol"])
            px_map[pos["symbol"]] = t[0] if t else pos["last_price"]
        closed = self.broker.square_off_all(price_map=px_map, reason="SAFETY_CUTOFF_1520", now=now)
        for tr in closed:
            self.reporter.notify_exit(tr)
        self.last_cutoff_date = d_str
        return closed

    def daily_report_cycle(self, now=None):
        now = N.to_ist(now) if now else N.now_ist()
        d_str = now.date().isoformat()
        if self.last_report_date == d_str:
            return None
        res = self.reporter.send_daily_report(self.broker, date_str=d_str)
        self.last_report_date = d_str
        return res


# ---------------------------------------------------------------------------
# Deterministic offline demo session (also used by tests & quick preview)
# ---------------------------------------------------------------------------

def _synth_60d_and_today(sym, pattern, base_px, day, seed=42):
    """Build 25 bullish-biased prior sessions (so TimeMachine win odds > 65%) + today's setup day."""
    rng = random.Random(seed)
    bars = []
    # 25 trading days before `day`
    prev_days = []
    cur = day - datetime.timedelta(days=1)
    while len(prev_days) < 25:
        if N.is_trading_day(cur):
            prev_days.append(cur)
        cur -= datetime.timedelta(days=1)
    prev_days.reverse()

    for d in prev_days:
        px = base_px
        t0 = datetime.datetime.combine(d, N.OPEN, N.IST)
        for i in range(75):
            # Mild positive intraday drift + small wicks -> realistic favorable simulation pool
            ret = rng.uniform(-0.0012, 0.0022)
            o = px
            c = round(o * (1.0 + ret), 2)
            h = round(max(o, c) * (1.0 + rng.uniform(0.0005, 0.0018)), 2)
            l = round(min(o, c) * (1.0 - rng.uniform(0.0004, 0.0013)), 2)
            v = rng.randint(60000, 140000)
            bars.append({"t": t0 + datetime.timedelta(minutes=5 * i), "o": o, "h": h, "l": l, "c": c, "v": v})
            px = c

    # Today's bars up to 10:15 IST triggering the chosen pattern
    t0 = datetime.datetime.combine(day, N.OPEN, N.IST)
    if pattern == "orb":
        # 3 OR bars (09:15, 09:20, 09:25) between base and base+8, then 09:45 breakout
        seq = [
            (base_px, base_px + 6, base_px - 4, base_px + 3, 90000),
            (base_px + 3, base_px + 8, base_px - 2, base_px + 5, 80000),
            (base_px + 5, base_px + 7, base_px, base_px + 4, 85000),
            (base_px + 4, base_px + 14, base_px + 3, base_px + 13, 180000),  # 09:30 bar closes > OR high (+8) on 2.1x vol
        ]
    elif pattern == "bull_flag":
        # Pole (4 bars +2%), tight lower-vol flag (4 bars), breakout bar
        b = base_px
        seq = [
            (b, b + 5, b - 2, b + 4, 150000),
            (b + 4, b + 12, b + 3, b + 11, 160000),
            (b + 11, b + 20, b + 10, b + 19, 170000),
            (b + 19, b + 28, b + 18, b + 26, 160000),
            (b + 26, b + 27, b + 22, b + 24, 65000),
            (b + 24, b + 25, b + 21, b + 23, 60000),
            (b + 23, b + 26, b + 21, b + 25, 55000),
            (b + 25, b + 26, b + 22, b + 24, 60000),
            (b + 24, b + 34, b + 23, b + 33, 210000),  # high-vol breakout > flag_high (27)
        ]
    elif pattern == "triangle":
        # 8 bars narrowing range with higher lows and flat resistance, then breakout
        b = base_px
        seq = [
            (b, b + 14, b - 6, b + 4, 90000),
            (b + 4, b + 13, b - 4, b + 8, 85000),
            (b + 8, b + 14, b - 2, b + 6, 80000),
            (b + 6, b + 12, b, b + 10, 75000),
            (b + 10, b + 13, b + 3, b + 8, 70000),
            (b + 8, b + 12, b + 5, b + 10, 65000),
            (b + 10, b + 13, b + 6, b + 11, 60000),
            (b + 11, b + 12, b + 7, b + 11, 60000),
            (b + 11, b + 20, b + 10, b + 19, 195000),  # breakout > 14 on >2x vol
        ]
    else:  # vwap_reclaim
        b = base_px
        seq = [
            (b, b + 6, b - 2, b + 4, 140000),
            (b + 4, b + 8, b + 2, b + 6, 120000),
            (b + 6, b + 7, b - 1, b + 1, 90000),
            (b + 1, b + 2, b - 6, b - 5, 80000),
            (b - 5, b - 1, b - 8, b - 4, 75000),
            (b - 4, b - 1, b - 7, b - 3, 70000),
            (b - 3, b + 11, b - 4, b + 10, 195000),  # strong reclaim above VWAP on high volume
        ]

    for i, (o, h, l, c, v) in enumerate(seq):
        bars.append({"t": t0 + datetime.timedelta(minutes=5 * i),
                     "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "v": v})
    return bars


def run_demo_session(db_path=DEFAULT_DB, outbox_path=DEFAULT_OUTBOX):
    """Run an offline end-to-end demo trading day populating paper_trades.db + Telegram outbox."""
    day = datetime.date(2026, 11, 18)
    data_map = {
        "RELIANCE": _synth_60d_and_today("RELIANCE", "orb", 2450.0, day, seed=11),
        "TCS": _synth_60d_and_today("TCS", "bull_flag", 3900.0, day, seed=22),
        "HDFCBANK": _synth_60d_and_today("HDFCBANK", "triangle", 1680.0, day, seed=33),
        "INFY": _synth_60d_and_today("INFY", "vwap_reclaim", 1850.0, day, seed=44),
    }

    # Simulated 10s price paths after entry:
    # RELIANCE: rallies +1.4% then pulls back -> TRAILED_SL profit lock!
    # TCS: hits +2R -> TARGET!
    # HDFCBANK: drifts slightly up, stays open until 15:20 -> SAFETY_CUTOFF_1520!
    ticks = {"RELIANCE": (2463.0, 2463.0, 2463.0), "TCS": (3933.0, 3933.0, 3933.0), "HDFCBANK": (1699.0, 1699.0, 1699.0)}

    cfg = N.load_config(None)
    cfg["budget"] = 100000.0
    cfg["max_trades_per_day"] = 5
    cfg["max_loss_per_trade_inr"] = 1500.0
    cfg["max_daily_loss_inr"] = 4500.0
    cfg["min_avg_turnover_cr"] = 10.0

    runner = AutonomousRunner(
        db_path=db_path, outbox_path=outbox_path, min_win_prob=65.0, max_symbols=4,
        replays=500, cfg=cfg, data_provider=lambda s: data_map.get(s, []),
        tick_provider=lambda s: ticks.get(s),
    )
    runner.broker.reset(100000.0)
    # Limit universe to our 4 demo stocks
    runner._universe_symbols = lambda: ["RELIANCE", "TCS", "HDFCBANK", "INFY"]

    scan_t = datetime.datetime.combine(day, datetime.time(10, 15), N.IST)
    res_scan = runner.scan_and_enter_cycle(now=scan_t)

    # Step 10s monitoring: push highs up, ratchet trailing stops, trigger exits
    t1 = datetime.datetime.combine(day, datetime.time(11, 0), N.IST)
    ticks["RELIANCE"] = (2496.0, 2498.0, 2480.0)   # +1.4% -> trailing arms & ratchets ~2483.0
    ticks["TCS"] = (3955.0, 3958.0, 3930.0)        # arms trailing stop
    ticks["HDFCBANK"] = (1704.0, 1705.0, 1696.0)
    runner.monitor_positions_cycle(now=t1)

    t2 = datetime.datetime.combine(day, datetime.time(12, 30), N.IST)
    ticks["RELIANCE"] = (2480.0, 2490.0, 2479.0)   # dips through ratcheted trailing SL -> TRAILED_SL!
    ticks["TCS"] = (3982.0, 3990.0, 3952.0)        # hits 2R target or ratchets high
    ticks["HDFCBANK"] = (1712.0, 1714.0, 1701.0)
    runner.monitor_positions_cycle(now=t2)

    t_cut = datetime.datetime.combine(day, datetime.time(15, 20), N.IST)
    ticks["HDFCBANK"] = (1710.0, 1711.0, 1708.0)
    runner.safety_square_off_cycle(now=t_cut)

    t_rep = datetime.datetime.combine(day, datetime.time(15, 30), N.IST)
    rep = runner.daily_report_cycle(now=t_rep)
    return {"scan": res_scan, "metrics": runner.broker.get_metrics(), "report": rep}


def run_live_loop(runner):
    print("NSE Autonomous Runner started (paper mode). Scanning every 3m, monitoring SL every 10s.")

    def job_scan():
        now = N.now_ist()
        res = runner.scan_and_enter_cycle(now=now)
        if res["entries"]:
            print("[%s] Opened %d paper trade(s): %s" % (N.fmt_time(now), len(res["entries"]),
                                                          [e["symbol"] for e in res["entries"]]))

    def job_monitor():
        now = N.now_ist()
        if not N.is_trading_day(now.date(), runner.cfg):
            return
        if now.time() < N.OPEN:
            return
        if now.time() >= N.hhmm("15:20") and now.time() < N.hhmm("15:30"):
            runner.safety_square_off_cycle(now=now)
            return
        if now.time() >= N.hhmm("15:30"):
            runner.daily_report_cycle(now=now)
            return
        runner.monitor_positions_cycle(now=now)

    schedule.every(3).minutes.do(job_scan)
    schedule.every(10).seconds.do(job_monitor)
    schedule.every().day.at("15:20").do(lambda: runner.safety_square_off_cycle())
    schedule.every().day.at("15:30").do(lambda: runner.daily_report_cycle())

    job_scan()
    while True:
        schedule.run_pending()
        time.sleep(1)


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Autonomous Paper Trading Runner")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--min-win-prob", type=float, default=65.0)
    p.add_argument("--max-symbols", type=int, default=25)
    p.add_argument("--replays", type=int, default=2000)
    p.add_argument("--once", action="store_true", help="Run one scan + monitor cycle and exit")
    p.add_argument("--demo-session", action="store_true", help="Run offline synthetic session into paper_trades.db")
    args = p.parse_args(argv)

    if args.demo_session:
        out = run_demo_session(db_path=args.db)
        m = out["metrics"]
        print("Demo session finished: %d closed trade(s), Equity %s (%+.2f%%), Net PnL %s, Win Rate %.1f%%, Peak Capture %.1f%%"
              % (m["total_trades"], N.inr(m["equity"]), m["return_pct"], N.inr(m["net_pnl"]),
                 m["win_rate_pct"], m["avg_peak_capture_pct"]))
        return 0

    runner = AutonomousRunner(db_path=args.db, min_win_prob=args.min_win_prob,
                              max_symbols=args.max_symbols, replays=args.replays)
    if args.once:
        res = runner.scan_and_enter_cycle()
        exits = runner.monitor_positions_cycle()
        print("Cycle status=%s | entries=%d | exits=%d" % (res["status"], len(res["entries"]), len(exits)))
        return 0

    run_live_loop(runner)
    return 0


if __name__ == "__main__":
    sys.exit(main())
