#!/usr/bin/env python3
"""Entry Timer for trader-entry (NSE India edition). Standard library only. Long side only.

    py entry.py check RELIANCE                              # 15m ORB (09:15-09:30 IST), 5m bars
    py entry.py check RELIANCE --setup bull_flag --write    # bull flag trigger -> trader/entry.json
    py entry.py check RELIANCE --setup triangle --write     # triangle breakout trigger
    py entry.py check RELIANCE --setup vwap_reclaim --write # VWAP reclaim trigger
    py entry.py check RELIANCE --setup level --level 2480   # first close above a price
    py entry.py watch RELIANCE --until 15:00                # bar by bar until trigger or expiry
    py entry.py watchlist                                   # check every hit in trader/watchlist.json

All times are IST (UTC+05:30). Session 09:15-15:30 IST; default entry window
09:30-15:00 IST; hard cutoff at 15:20 IST safety square-off.
Triggers require a COMPLETED bar closing strictly above the pattern level with
volume >= --min-volume-x (default 1.5x average prior bar volume).

Exit codes: check 0 = triggered, 1 = waiting/expired/no-data/error.
watch 0 = triggered, 1 = expired/stopped. watchlist 0 = >=1 triggered.
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as N

DEFAULT_WATCHLIST = "trader/watchlist.json"
DEFAULT_ENTRY = "trader/entry.json"
DEFAULT_CONFIG = "trader/config.json"
INTERVALS = {"1m": 60, "5m": 300}


class EntryError(N.NSEError):
    pass


def _load_offline(sym, offline_path, interval_min, now):
    if os.path.isdir(offline_path):
        cands = [
            os.path.join(offline_path, "%s.json" % sym),
            os.path.join(offline_path, "%s.json.gz" % sym),
            os.path.join(offline_path, "%s.NS.json" % sym),
            os.path.join(offline_path, "%s.NS.json.gz" % sym),
        ]
        src = next((c for c in cands if os.path.exists(c)), None)
        if not src:
            raise EntryError("No offline chart for %s in %s." % (sym, offline_path))
    else:
        src = offline_path
    doc = json.loads(N.read_maybe_gz(src))
    if isinstance(doc, dict) and sym in doc and "chart" not in doc:
        doc = doc[sym]
    if isinstance(doc, list):
        raw_bars = doc
    elif isinstance(doc, dict) and "bars" in doc:
        raw_bars = doc["bars"]
    elif isinstance(doc, dict) and "chart" in doc:
        bars, _ = N.bars_from_yahoo(doc, interval_min, drop_partial_at=now)
        return bars, "offline (%s)" % os.path.basename(src)
    elif isinstance(doc, dict) and "data" in doc and isinstance(doc["data"], dict) and "candles" in doc["data"]:
        return N.bars_from_upstox(doc, interval_min, drop_partial_at=now), "offline (%s)" % os.path.basename(src)
    elif isinstance(doc, dict) and "data" in doc and isinstance(doc["data"], list):
        return N.bars_from_angel(doc, interval_min, drop_partial_at=now), "offline (%s)" % os.path.basename(src)
    else:
        raise EntryError("Unrecognized offline file format in %s." % src)

    out = []
    for b in raw_bars:
        dt = b["t"] if isinstance(b["t"], datetime.datetime) else N.parse_now(str(b["t"]))
        dt = N.to_ist(dt)
        if now is not None and dt + datetime.timedelta(minutes=interval_min) > now:
            continue
        out.append({"t": dt, "o": float(b["o"]), "h": float(b["h"]),
                    "l": float(b["l"]), "c": float(b["c"]), "v": int(b.get("v", 0))})
    out.sort(key=lambda x: x["t"])
    return out, "offline (%s)" % os.path.basename(src)


def _vwap_at(bars, upto_idx):
    cum_pv = 0.0
    cum_v = 0
    for b in bars[: upto_idx + 1]:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        v = max(1, int(b["v"]))
        cum_pv += tp * v
        cum_v += v
    return round(cum_pv / cum_v, 2) if cum_v else 0.0


def _vol_ratio(bars, idx):
    prior = [b["v"] for b in bars[:idx] if b["v"] > 0]
    if not prior:
        return 0.0
    avg_v = sum(prior[-12:]) / len(prior[-12:])
    return round(bars[idx]["v"] / avg_v, 2) if avg_v > 0 else 0.0


def _opening_range(bars, day, or_minutes=15, interval_min=5):
    start_dt = datetime.datetime.combine(day, N.OPEN, N.IST)
    or_end = start_dt + datetime.timedelta(minutes=or_minutes)
    or_bars = [b for b in bars if start_dt <= b["t"] < or_end]
    expected = max(1, or_minutes // interval_min)
    if not or_bars:
        return None, False, or_end
    return {
        "high": round(max(b["h"] for b in or_bars), 2),
        "low": round(min(b["l"] for b in or_bars), 2),
        "minutes": or_minutes,
        "bars": len(or_bars),
        "expected": expected,
    }, len(or_bars) >= expected, or_end


def _detect_trigger(bars, setup, level, min_vol_x, or_minutes, interval_min, day, before_t, after_t):
    """Evaluate bars inside the session and return (status, details_dict)."""
    orr, or_complete, or_end = _opening_range(bars, day, or_minutes, interval_min)
    no_before_dt = datetime.datetime.combine(day, before_t, N.IST)
    no_after_dt = datetime.datetime.combine(day, after_t, N.IST)

    def _in_window(bar_close_dt):
        return no_before_dt <= bar_close_dt <= no_after_dt

    if setup == "orb":
        if not or_complete or orr is None:
            return "waiting", {"reason": "Opening range (09:15-%s) still forming." % N.fmt_time(or_end),
                               "opening_range": orr}
        or_high, or_low = orr["high"], orr["low"]
        or_mid = N.round_tick((or_high + or_low) / 2.0, "down")
        for idx, b in enumerate(bars):
            close_dt = b["t"] + datetime.timedelta(minutes=interval_min)
            if b["t"] < or_end or not _in_window(close_dt):
                continue
            if b["c"] > or_high:
                vr = _vol_ratio(bars, idx)
                if vr >= float(min_vol_x):
                    return "triggered", {
                        "entry": N.round_tick(b["c"], "nearest"),
                        "close_dt": close_dt,
                        "bar_index": idx + 1,
                        "volume_ratio": vr,
                        "vwap": _vwap_at(bars, idx),
                        "opening_range": {"high": or_high, "low": or_low, "minutes": or_minutes},
                        "levels": {"or_high": or_high, "or_low": or_low, "pattern_low": or_mid,
                                   "trigger_level": or_high},
                    }
        return "waiting", {"reason": "No completed bar closed above OR high %s with >= %.1fx volume."
                           % (N.inr(or_high), min_vol_x),
                           "opening_range": {"high": or_high, "low": or_low, "minutes": or_minutes},
                           "levels": {"or_high": or_high, "or_low": or_low, "trigger_level": or_high}}

    if setup == "level":
        if level is None:
            raise EntryError("Setup 'level' requires --level PRICE.")
        lvl = float(level)
        for idx, b in enumerate(bars):
            close_dt = b["t"] + datetime.timedelta(minutes=interval_min)
            if not _in_window(close_dt):
                continue
            if b["c"] > lvl:
                vr = _vol_ratio(bars, idx)
                if vr >= float(min_vol_x):
                    pat_low = N.round_tick(min(x["l"] for x in bars[max(0, idx - 3): idx + 1]), "down")
                    return "triggered", {
                        "entry": N.round_tick(b["c"], "nearest"),
                        "close_dt": close_dt,
                        "bar_index": idx + 1,
                        "volume_ratio": vr,
                        "vwap": _vwap_at(bars, idx),
                        "opening_range": ({"high": orr["high"], "low": orr["low"], "minutes": or_minutes}
                                          if orr else {"high": None, "low": None, "minutes": or_minutes}),
                        "levels": {"trigger_level": round(lvl, 2), "pattern_low": pat_low},
                    }
        return "waiting", {"reason": "No completed bar closed above %s with >= %.1fx volume."
                           % (N.inr(lvl), min_vol_x),
                           "levels": {"trigger_level": round(lvl, 2)}}

    if setup == "bull_flag":
        n = len(bars)
        for idx in range(7, n):
            close_dt = bars[idx]["t"] + datetime.timedelta(minutes=interval_min)
            if not _in_window(close_dt):
                continue
            brk = bars[idx]
            for flag_len in range(3, 9):
                f_start = idx - flag_len
                if f_start < 3:
                    continue
                flag_bars = bars[f_start:idx]
                flag_high = max(x["h"] for x in flag_bars)
                flag_low = min(x["l"] for x in flag_bars)
                if brk["c"] <= flag_high:
                    continue
                for pole_len in range(3, 9):
                    p_start = f_start - pole_len
                    if p_start < 0:
                        continue
                    pole_bars = bars[p_start:f_start]
                    pole_low = min(x["l"] for x in pole_bars)
                    pole_high = max(x["h"] for x in pole_bars)
                    if pole_low <= 0 or (pole_high - pole_low) / pole_low * 100.0 < 1.2:
                        continue
                    if pole_bars[-1]["c"] < pole_low + 0.6 * (pole_high - pole_low):
                        continue
                    if flag_low < pole_high - 0.55 * (pole_high - pole_low) or flag_high > pole_high * 1.004:
                        continue
                    pole_vol = sum(x["v"] for x in pole_bars) / len(pole_bars)
                    flag_vol = sum(x["v"] for x in flag_bars) / len(flag_bars)
                    if pole_vol > 0 and flag_vol > pole_vol * 0.95:
                        continue
                    vr = _vol_ratio(bars, idx)
                    if vr >= float(min_vol_x):
                        return "triggered", {
                            "entry": N.round_tick(brk["c"], "nearest"),
                            "close_dt": close_dt,
                            "bar_index": idx + 1,
                            "volume_ratio": vr,
                            "vwap": _vwap_at(bars, idx),
                            "opening_range": ({"high": orr["high"], "low": orr["low"], "minutes": or_minutes}
                                              if orr else {"high": None, "low": None, "minutes": or_minutes}),
                            "levels": {"pole_low": round(pole_low, 2), "pole_high": round(pole_high, 2),
                                       "flag_high": round(flag_high, 2), "flag_low": round(flag_low, 2),
                                       "pattern_low": N.round_tick(flag_low, "down"),
                                       "trigger_level": round(flag_high, 2)},
                        }
        return "waiting", {"reason": "Bull flag breakout not yet triggered."}

    if setup == "triangle":
        n = len(bars)
        for idx in range(6, n):
            close_dt = bars[idx]["t"] + datetime.timedelta(minutes=interval_min)
            if not _in_window(close_dt):
                continue
            brk = bars[idx]
            for w_len in range(min(15, idx), 5, -1):
                win = bars[idx - w_len: idx]
                half = w_len // 2
                h1, h2 = win[:half], win[half:]
                hi1, lo1 = max(x["h"] for x in h1), min(x["l"] for x in h1)
                hi2, lo2 = max(x["h"] for x in h2), min(x["l"] for x in h2)
                if lo2 <= lo1 or hi2 > hi1 * 1.002:
                    continue
                r1, r2 = hi1 - lo1, hi2 - lo2
                if r1 <= 0 or r2 >= r1 * 0.85:
                    continue
                upper = round(max(hi1, hi2) if abs(hi1 - hi2) / hi1 <= 0.002 else hi2, 2)
                lower = round(lo2, 2)
                if brk["c"] <= upper:
                    continue
                vr = _vol_ratio(bars, idx)
                if vr >= float(min_vol_x):
                    return "triggered", {
                        "entry": N.round_tick(brk["c"], "nearest"),
                        "close_dt": close_dt,
                        "bar_index": idx + 1,
                        "volume_ratio": vr,
                        "vwap": _vwap_at(bars, idx),
                        "opening_range": ({"high": orr["high"], "low": orr["low"], "minutes": or_minutes}
                                          if orr else {"high": None, "low": None, "minutes": or_minutes}),
                        "levels": {"triangle_upper": upper, "triangle_lower": lower,
                                   "pattern_low": N.round_tick(lower, "down"),
                                   "trigger_level": upper},
                    }
        return "waiting", {"reason": "Triangle breakout not yet triggered."}

    if setup == "vwap_reclaim":
        below = 0
        recent_low = None
        for idx in range(3, len(bars)):
            b = bars[idx]
            vw = _vwap_at(bars, idx)
            close_dt = b["t"] + datetime.timedelta(minutes=interval_min)
            if b["c"] < vw:
                below += 1
                recent_low = b["l"] if recent_low is None else min(recent_low, b["l"])
            elif b["c"] > vw and below >= 2 and _in_window(close_dt):
                vr = _vol_ratio(bars, idx)
                if vr >= float(min_vol_x):
                    rec_low = round(recent_low if recent_low is not None else b["l"], 2)
                    return "triggered", {
                        "entry": N.round_tick(b["c"], "nearest"),
                        "close_dt": close_dt,
                        "bar_index": idx + 1,
                        "volume_ratio": vr,
                        "vwap": vw,
                        "opening_range": ({"high": orr["high"], "low": orr["low"], "minutes": or_minutes}
                                          if orr else {"high": None, "low": None, "minutes": or_minutes}),
                        "levels": {"vwap": vw, "reclaim_low": rec_low,
                                   "pattern_low": N.round_tick(rec_low, "down"),
                                   "trigger_level": vw},
                    }
                below = 0
                recent_low = None
            else:
                below = 0
                recent_low = None
        return "waiting", {"reason": "VWAP reclaim not yet triggered."}

    raise EntryError("Unknown setup %r." % setup)


def evaluate_entry(sym, bars, setup="orb", level=None, min_vol_x=1.5, or_minutes=15,
                   interval_min=5, now=None, cfg=None, source="yahoo"):
    now = N.to_ist(now) if now else N.now_ist()
    day = bars[0]["t"].date() if bars else now.date()
    cfg = cfg or N.load_config(DEFAULT_CONFIG)
    before_t = N.config_time(cfg, "no_entries_before", N.NO_ENTRIES_BEFORE)
    after_t = N.config_time(cfg, "no_entries_after", N.NO_ENTRIES_AFTER)
    cutoff_t = N.config_time(cfg, "safety_cutoff", N.SAFETY_CUTOFF)

    if not N.is_trading_day(now.date(), cfg) and not bars:
        return {"symbol": sym, "status": "no-data", "setup": setup, "date": now.date().isoformat(),
                "reason": N.closed_reason(now.date(), cfg), "source": source}

    if now.time() < N.OPEN and not bars:
        return {"symbol": sym, "status": "waiting", "setup": setup, "date": now.date().isoformat(),
                "reason": "NSE opens at 09:15 IST (now %s)." % N.fmt_time(now), "source": source}

    if not bars:
        return {"symbol": sym, "status": "no-data", "setup": setup, "date": now.date().isoformat(),
                "reason": "No completed %dm bars available yet." % interval_min, "source": source}

    status, info = _detect_trigger(bars, setup, level, min_vol_x, or_minutes, interval_min, day, before_t, after_t)
    if status == "waiting" and (now.time() >= after_t or now.time() >= cutoff_t):
        return {
            "symbol": sym,
            "status": "expired",
            "setup": setup,
            "date": day.isoformat(),
            "interval": "%dm" % interval_min,
            "reason": "No trigger before cutoff (%s IST; safety square-off %s IST)."
                      % (after_t.strftime("%H:%M"), cutoff_t.strftime("%H:%M")),
            "opening_range": info.get("opening_range"),
            "levels": info.get("levels", {}),
            "source": source,
        }

    if status == "waiting":
        return {
            "symbol": sym,
            "status": "waiting",
            "setup": setup,
            "date": day.isoformat(),
            "interval": "%dm" % interval_min,
            "last_close": round(bars[-1]["c"], 2),
            "last_bar_time": N.fmt_time(bars[-1]["t"] + datetime.timedelta(minutes=interval_min)),
            "reason": info.get("reason", "Waiting for trigger."),
            "opening_range": info.get("opening_range"),
            "levels": info.get("levels", {}),
            "source": source,
        }

    close_dt = info["close_dt"]
    return {
        "symbol": sym,
        "status": "triggered",
        "setup": setup,
        "entry": info["entry"],
        "time": N.fmt_time(close_dt),
        "bar_index": info["bar_index"],
        "interval": "%dm" % interval_min,
        "date": day.isoformat(),
        "opening_range": info["opening_range"],
        "levels": info["levels"],
        "volume_ratio": info["volume_ratio"],
        "vwap": info["vwap"],
        "source": source,
    }


def _watchlist_level_for(sym, setup_arg, level_arg):
    wl = N.read_json(DEFAULT_WATCHLIST)
    if not wl:
        return setup_arg or "orb", level_arg
    for h in wl.get("hits", []):
        if h.get("symbol") == sym:
            s = setup_arg or h.get("setup") or "orb"
            lvl = level_arg if level_arg is not None else (h.get("levels") or {}).get("trigger_level")
            return s, lvl
    return setup_arg or "orb", level_arg


def _fetch(sym, day, interval_min, feed, now, cfg, offline):
    if offline:
        return _load_offline(sym, offline, interval_min, now)
    return N.get_intraday_bars(sym, day, interval_min=interval_min, feed=feed, now=now, cfg=cfg)


def _print_result(res):
    st = res["status"].upper()
    if st == "TRIGGERED":
        print("%s  %s  %s  TRIGGERED at %s (%s)  bar #%d  vol %.2fx  vwap %s  [%s]"
              % (res["symbol"], res["date"], res["setup"], N.inr(res["entry"]), res["time"],
                 res["bar_index"], res["volume_ratio"], N.inr(res["vwap"]), res["source"]))
        print("This is the trigger bar close, not an order. Nothing was placed. Next: trader-exit.")
    else:
        print("%s  %s  %s  %s  %s"
              % (res["symbol"], res["date"], res["setup"], st, res.get("reason", "")))


def cmd_check(args, cfg):
    sym = N.norm_symbol(args.symbol)
    now = N.parse_now(args.now) if args.now else N.now_ist()
    interval_min = INTERVALS[args.interval] // 60
    feed = args.feed or cfg.get("data_feed") or "yahoo"
    setup, level = _watchlist_level_for(sym, args.setup, args.level)
    bars, src = _fetch(sym, now.date(), interval_min, feed, now, cfg, args.offline)
    res = evaluate_entry(sym, bars, setup=setup, level=level, min_vol_x=args.min_volume_x,
                         or_minutes=args.or_minutes, interval_min=interval_min, now=now, cfg=cfg, source=src)
    if args.write and res["status"] == "triggered":
        N.write_json(DEFAULT_ENTRY, res)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        _print_result(res)
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(args, cfg):
    sym = N.norm_symbol(args.symbol)
    now = N.parse_now(args.now) if args.now else N.now_ist()
    interval_min = INTERVALS[args.interval] // 60
    feed = args.feed or cfg.get("data_feed") or "yahoo"
    setup, level = _watchlist_level_for(sym, args.setup, args.level)
    until_t = N.hhmm(args.until) if args.until else N.config_time(cfg, "no_entries_after", N.NO_ENTRIES_AFTER)

    # Simulated replay when --now or --offline is used
    if args.offline or args.now:
        all_bars, src = _fetch(sym, now.date(), interval_min, feed, None, cfg, args.offline)
        day_bars = [b for b in all_bars if b["t"].date() == (all_bars[-1]["t"].date() if all_bars else now.date())]
        last_res = None
        for i in range(1, len(day_bars) + 1):
            sub = day_bars[:i]
            close_dt = sub[-1]["t"] + datetime.timedelta(minutes=interval_min)
            if close_dt.time() > until_t:
                break
            res = evaluate_entry(sym, sub, setup=setup, level=level, min_vol_x=args.min_volume_x,
                                 or_minutes=args.or_minutes, interval_min=interval_min, now=close_dt, cfg=cfg, source=src)
            last_res = res
            if not args.json:
                print("%s  close %s  %s" % (N.fmt_time(close_dt), N.inr(sub[-1]["c"]), res["status"].upper()))
            if res["status"] == "triggered":
                N.write_json(DEFAULT_ENTRY, res)
                if args.json:
                    print(json.dumps(res, indent=2))
                else:
                    _print_result(res)
                return 0
        if last_res is None:
            last_res = evaluate_entry(sym, [], setup=setup, level=level, now=now, cfg=cfg, source=src)
        if args.json:
            print(json.dumps(last_res, indent=2))
        else:
            _print_result(last_res)
        return 1

    # Live loop
    while True:
        now = N.now_ist()
        bars, src = _fetch(sym, now.date(), interval_min, feed, now, cfg, None)
        res = evaluate_entry(sym, bars, setup=setup, level=level, min_vol_x=args.min_volume_x,
                             or_minutes=args.or_minutes, interval_min=interval_min, now=now, cfg=cfg, source=src)
        if res["status"] == "triggered":
            N.write_json(DEFAULT_ENTRY, res)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                _print_result(res)
            return 0
        if res["status"] == "expired" or now.time() >= until_t:
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                _print_result(res)
            return 1
        if not args.json:
            print("%s  %s  %s" % (N.fmt_time(now), sym, res["status"].upper()))
        N.SLEEP(max(5, args.every))


def cmd_watchlist(args, cfg):
    wl = N.read_json(args.file or DEFAULT_WATCHLIST)
    if not wl or not wl.get("hits"):
        raise EntryError("Watchlist %s has no hits." % (args.file or DEFAULT_WATCHLIST))
    now = N.parse_now(args.now) if args.now else N.now_ist()
    interval_min = INTERVALS[args.interval] // 60
    feed = args.feed or cfg.get("data_feed") or "yahoo"
    results = []
    triggered_any = False
    for h in wl["hits"]:
        sym = N.norm_symbol(h["symbol"])
        setup = args.setup or h.get("setup") or "orb"
        level = (h.get("levels") or {}).get("trigger_level")
        try:
            bars, src = _fetch(sym, now.date(), interval_min, feed, now, cfg, args.offline)
            res = evaluate_entry(sym, bars, setup=setup, level=level, min_vol_x=args.min_volume_x,
                                 or_minutes=args.or_minutes, interval_min=interval_min, now=now, cfg=cfg, source=src)
        except N.NSEError as e:
            res = {"symbol": sym, "status": "no-data", "setup": setup, "date": now.date().isoformat(), "reason": str(e)}
        results.append(res)
        if res["status"] == "triggered":
            triggered_any = True
            if args.write:
                N.write_json(DEFAULT_ENTRY, res)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            _print_result(r)
    return 0 if triggered_any else 1


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Entry Timer (trader-entry)")
    sub = p.add_subparsers(dest="cmd")

    def add_common(sp):
        sp.add_argument("--setup", choices=("orb", "bull_flag", "triangle", "vwap_reclaim", "level"),
                        help="Pattern rule (defaults to watchlist setup or orb)")
        sp.add_argument("--level", type=float, help="Breakout price for --setup level")
        sp.add_argument("--interval", default="5m", choices=("5m", "1m"), help="Bar interval (default 5m)")
        sp.add_argument("--or-minutes", type=int, default=15, help="Opening range minutes (default 15)")
        sp.add_argument("--min-volume-x", type=float, default=1.5, help="Min bar volume ratio (default 1.5)")
        sp.add_argument("--feed", choices=("yahoo", "upstox", "angelone"), help="Market data feed")
        sp.add_argument("--now", help="Pin IST clock 'YYYY-MM-DD HH:MM'")
        sp.add_argument("--offline", help="Read saved chart JSON or folder")
        sp.add_argument("--write", action="store_true", help="Write trader/entry.json when triggered")
        sp.add_argument("--json", action="store_true", help="Print JSON output")

    sc = sub.add_parser("check", help="Check one NSE symbol now")
    sc.add_argument("symbol")
    add_common(sc)

    sw = sub.add_parser("watch", help="Watch one symbol bar by bar")
    sw.add_argument("symbol")
    sw.add_argument("--until", help="Stop at HH:MM IST (default config no_entries_after)")
    sw.add_argument("--every", type=int, default=30, help="Poll seconds (default 30)")
    add_common(sw)

    sl = sub.add_parser("watchlist", help="Check all hits in trader/watchlist.json")
    sl.add_argument("--file", default=DEFAULT_WATCHLIST)
    add_common(sl)

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 1
    try:
        cfg = N.load_config(DEFAULT_CONFIG)
        if args.cmd == "check":
            return cmd_check(args, cfg)
        if args.cmd == "watch":
            return cmd_watch(args, cfg)
        return cmd_watchlist(args, cfg)
    except N.NSEError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
