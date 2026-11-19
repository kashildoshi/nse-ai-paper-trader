#!/usr/bin/env python3
"""Autonomous SQLite Paper Broker (WAL mode) for Indian NSE Intraday Trading.

Starts with ₹1,00,000 virtual capital, simulates 0.03% slippage on entry and exit,
deducts Indian intraday MIS equity charges (brokerage, 0.025% sell STT, NSE txn,
SEBI fee, 0.003% buy stamp duty, 18% GST), enforces the ratcheting Trailing Stop-Loss
(0.6% below peak high_water once profit exceeds 0.5%), and enforces the 15:20 IST
safety square-off.

    py paper_broker.py init
    py paper_broker.py status
    py paper_broker.py reset
"""

import argparse
import contextlib
import datetime
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "shared"))
import nse_common as N

DEFAULT_DB = os.path.join(ROOT, "paper_trades.db")
INITIAL_CAPITAL = 100000.0
SLIPPAGE_PCT = 0.0003  # 0.03% per side


class PaperBrokerError(N.NSEError):
    pass


class PaperBroker:
    def __init__(self, db_path=DEFAULT_DB, initial_capital=INITIAL_CAPITAL, slippage_pct=SLIPPAGE_PCT, cfg=None):
        self.db_path = db_path
        self.initial_capital = float(initial_capital)
        self.slippage_pct = float(slippage_pct)
        self.cfg = cfg or N.load_config(os.path.join(ROOT, "trader", "config.json"))
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    @contextlib.contextmanager
    def _tx(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)) or ".", exist_ok=True)
        with self._tx() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS account_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL UNIQUE,
                    pattern TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    signal_price REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    initial_stop REAL NOT NULL,
                    current_stop REAL NOT NULL,
                    target_price REAL,
                    high_water REAL NOT NULL,
                    trail_active INTEGER NOT NULL DEFAULT 0,
                    trail_activate_pct REAL NOT NULL DEFAULT 0.5,
                    trail_pct REAL NOT NULL DEFAULT 0.6,
                    last_price REAL NOT NULL,
                    win_prob_pct REAL,
                    entry_time TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id INTEGER,
                    symbol TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    signal_entry_price REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    signal_exit_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    initial_stop REAL NOT NULL,
                    final_stop REAL NOT NULL,
                    target_price REAL,
                    high_water REAL NOT NULL,
                    trail_active INTEGER NOT NULL,
                    exit_reason TEXT NOT NULL,
                    gross_pnl REAL NOT NULL,
                    charges REAL NOT NULL,
                    charges_json TEXT NOT NULL,
                    net_pnl REAL NOT NULL,
                    peak_profit_inr REAL NOT NULL,
                    peak_capture_pct REAL NOT NULL,
                    win_prob_pct REAL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    cash REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    equity REAL NOT NULL,
                    open_positions INTEGER NOT NULL
                );
                """
            )
            cur = conn.execute("SELECT key FROM account_meta WHERE key = 'initial_capital'").fetchone()
            if not cur:
                now_iso = N.now_ist().isoformat()
                defaults = [
                    ("initial_capital", str(self.initial_capital)),
                    ("cash", str(self.initial_capital)),
                    ("realized_pnl", "0.0"),
                    ("created_at", now_iso),
                    ("updated_at", now_iso),
                ]
                conn.executemany("INSERT OR REPLACE INTO account_meta (key, value) VALUES (?, ?)", defaults)
                conn.execute(
                    "INSERT INTO equity_snapshots (timestamp, cash, unrealized_pnl, realized_pnl, equity, open_positions) "
                    "VALUES (?, ?, 0.0, 0.0, ?, 0)",
                    (now_iso, self.initial_capital, self.initial_capital),
                )

    def reset(self, initial_capital=None):
        cap = float(initial_capital if initial_capital is not None else self.initial_capital)
        with self._tx() as conn:
            conn.executescript("DELETE FROM positions; DELETE FROM trades; DELETE FROM equity_snapshots; DELETE FROM account_meta;")
            now_iso = N.now_ist().isoformat()
            conn.executemany(
                "INSERT INTO account_meta (key, value) VALUES (?, ?)",
                [
                    ("initial_capital", str(cap)),
                    ("cash", str(cap)),
                    ("realized_pnl", "0.0"),
                    ("created_at", now_iso),
                    ("updated_at", now_iso),
                ],
            )
            conn.execute(
                "INSERT INTO equity_snapshots (timestamp, cash, unrealized_pnl, realized_pnl, equity, open_positions) "
                "VALUES (?, ?, 0.0, 0.0, ?, 0)",
                (now_iso, cap, cap),
            )

    def _meta(self, conn):
        rows = conn.execute("SELECT key, value FROM account_meta").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def _record_snapshot(self, conn, ts_iso):
        meta = self._meta(conn)
        cash = float(meta.get("cash", self.initial_capital))
        realized = float(meta.get("realized_pnl", 0.0))
        pos_rows = conn.execute("SELECT qty, entry_price, last_price FROM positions").fetchall()
        deployed = sum(r["qty"] * r["entry_price"] for r in pos_rows)
        unrealized = sum((r["last_price"] - r["entry_price"]) * r["qty"] for r in pos_rows)
        equity = round(cash + deployed + unrealized, 2)
        conn.execute(
            "INSERT INTO equity_snapshots (timestamp, cash, unrealized_pnl, realized_pnl, equity, open_positions) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts_iso, round(cash, 2), round(unrealized, 2), round(realized, 2), equity, len(pos_rows)),
        )

    def get_open_positions(self):
        with self._tx() as conn:
            rows = conn.execute("SELECT * FROM positions ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def get_closed_trades(self, date_str=None):
        with self._tx() as conn:
            if date_str:
                rows = conn.execute(
                    "SELECT * FROM trades WHERE exit_time LIKE ? ORDER BY id ASC", (date_str + "%",)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def open_position(self, symbol, pattern, qty, signal_price, initial_stop, target_price=None,
                      win_prob_pct=None, now=None, trail_activate_pct=0.5, trail_pct=0.6):
        now = N.to_ist(now) if now else N.now_ist()
        sym = N.norm_symbol(symbol)
        qty = int(qty)
        if qty <= 0:
            raise PaperBrokerError("Quantity must be positive.")
        sig_px = float(signal_price)
        fill_px = round(sig_px * (1.0 + self.slippage_pct), 2)
        init_sl = float(initial_stop)
        if init_sl >= fill_px:
            raise PaperBrokerError("Initial stop %.2f must be below slipped entry %.2f." % (init_sl, fill_px))

        cost = round(qty * fill_px, 2)
        ts_iso = now.isoformat()

        with self._tx() as conn:
            existing = conn.execute("SELECT id FROM positions WHERE symbol = ?", (sym,)).fetchone()
            if existing:
                raise PaperBrokerError("Position already open for %s." % sym)
            meta = self._meta(conn)
            cash = float(meta.get("cash", 0.0))
            if cost > cash + 1e-6:
                raise PaperBrokerError("Insufficient virtual cash (%s) for %s (%s)."
                                       % (N.inr(cash), sym, N.inr(cost)))

            new_cash = round(cash - cost, 2)
            cur = conn.execute(
                """
                INSERT INTO positions (
                    symbol, pattern, qty, signal_price, entry_price, initial_stop, current_stop,
                    target_price, high_water, trail_active, trail_activate_pct, trail_pct,
                    last_price, win_prob_pct, entry_time, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                (sym, pattern, qty, sig_px, fill_px, init_sl, init_sl,
                 float(target_price) if target_price is not None else None,
                 fill_px, float(trail_activate_pct), float(trail_pct),
                 fill_px, float(win_prob_pct) if win_prob_pct is not None else None,
                 ts_iso, ts_iso),
            )
            pos_id = cur.lastrowid
            conn.execute("INSERT OR REPLACE INTO account_meta (key, value) VALUES ('cash', ?)", (str(new_cash),))
            conn.execute("INSERT OR REPLACE INTO account_meta (key, value) VALUES ('updated_at', ?)", (ts_iso,))
            self._record_snapshot(conn, ts_iso)
            row = conn.execute("SELECT * FROM positions WHERE id = ?", (pos_id,)).fetchone()
            return dict(row)

    def close_position(self, position_id, signal_exit_price, reason="MANUAL", now=None):
        now = N.to_ist(now) if now else N.now_ist()
        ts_iso = now.isoformat()
        sig_ex = float(signal_exit_price)
        exit_px = round(sig_ex * (1.0 - self.slippage_pct), 2)

        with self._tx() as conn:
            pos = conn.execute("SELECT * FROM positions WHERE id = ?", (int(position_id),)).fetchone()
            if not pos:
                raise PaperBrokerError("Open position id %s not found." % position_id)
            pos = dict(pos)
            qty = int(pos["qty"])
            entry_px = float(pos["entry_price"])
            hw = max(float(pos["high_water"]), exit_px)

            gross_pnl = round((exit_px - entry_px) * qty, 2)
            ch = N.intraday_charges(qty, entry_px, exit_px, self.cfg)
            charges_total = float(ch["total"])
            net_pnl = round(gross_pnl - charges_total, 2)

            peak_profit = round(max(0.0, (pos["high_water"] - entry_px) * qty), 2)
            if pos["high_water"] > entry_px and gross_pnl > 0:
                peak_capture = round(min(100.0, max(0.0, ((exit_px - entry_px) / (pos["high_water"] - entry_px)) * 100.0)), 2)
            else:
                peak_capture = 0.0

            cur = conn.execute(
                """
                INSERT INTO trades (
                    position_id, symbol, pattern, qty, signal_entry_price, entry_price,
                    signal_exit_price, exit_price, initial_stop, final_stop, target_price,
                    high_water, trail_active, exit_reason, gross_pnl, charges, charges_json,
                    net_pnl, peak_profit_inr, peak_capture_pct, win_prob_pct, entry_time, exit_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (pos["id"], pos["symbol"], pos["pattern"], qty, pos["signal_price"], entry_px,
                 sig_ex, exit_px, pos["initial_stop"], pos["current_stop"], pos["target_price"],
                 pos["high_water"], pos["trail_active"], reason, gross_pnl, charges_total,
                 json.dumps(ch), net_pnl, peak_profit, peak_capture, pos["win_prob_pct"],
                 pos["entry_time"], ts_iso),
            )
            trade_id = cur.lastrowid
            conn.execute("DELETE FROM positions WHERE id = ?", (pos["id"],))

            meta = self._meta(conn)
            cash = float(meta.get("cash", 0.0))
            realized = float(meta.get("realized_pnl", 0.0))
            proceeds = round(qty * exit_px - charges_total, 2)
            new_cash = round(cash + proceeds, 2)
            new_realized = round(realized + net_pnl, 2)

            conn.execute("INSERT OR REPLACE INTO account_meta (key, value) VALUES ('cash', ?)", (str(new_cash),))
            conn.execute("INSERT OR REPLACE INTO account_meta (key, value) VALUES ('realized_pnl', ?)", (str(new_realized),))
            conn.execute("INSERT OR REPLACE INTO account_meta (key, value) VALUES ('updated_at', ?)", (ts_iso,))
            self._record_snapshot(conn, ts_iso)

            tr = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
            return dict(tr)

    def update_price_and_check_exit(self, symbol, current_price, bar_high=None, bar_low=None, now=None):
        """Update live LTP/high/low for an open position, ratchet the trailing stop, and
        close automatically if stop, target, or 15:20 IST safety cutoff triggers.
        Returns closed trade dict if exited, else None."""
        now = N.to_ist(now) if now else N.now_ist()
        sym = N.norm_symbol(symbol)
        ltp = float(current_price)
        hi = float(bar_high) if bar_high is not None else ltp
        lo = float(bar_low) if bar_low is not None else ltp
        cutoff_t = N.config_time(self.cfg, "safety_cutoff", N.SAFETY_CUTOFF)

        with self._tx() as conn:
            row = conn.execute("SELECT * FROM positions WHERE symbol = ?", (sym,)).fetchone()
            if not row:
                return None
            pos = dict(row)

        entry_px = float(pos["entry_price"])
        cur_stop = float(pos["current_stop"])
        hw = float(pos["high_water"])
        active = bool(pos["trail_active"])
        act_pct = float(pos["trail_activate_pct"])
        tr_pct = float(pos["trail_pct"])

        # 1. Conservative stop test against the stop in force
        if lo <= cur_stop:
            reason = "TRAILED_SL" if active and cur_stop > float(pos["initial_stop"]) + 1e-9 else "INITIAL_SL"
            return self.close_position(pos["id"], cur_stop, reason=reason, now=now)

        # 2. Ratchet high_water and trailing stop (only ever moves UP)
        if hi > hw:
            hw = hi
        profit_pct = (hw - entry_px) / entry_px * 100.0
        if not active and profit_pct >= act_pct - 1e-9:
            active = True
        if active:
            candidate_stop = N.trail_stop_for(hw, tr_pct)
            if candidate_stop > cur_stop:
                cur_stop = candidate_stop

        # 3. Optional take-profit target check
        if pos["target_price"] is not None and hi >= float(pos["target_price"]):
            return self.close_position(pos["id"], float(pos["target_price"]), reason="TARGET", now=now)

        # 4. 15:20 IST safety cutoff
        if now.time() >= cutoff_t:
            return self.close_position(pos["id"], ltp, reason="SAFETY_CUTOFF_1520", now=now)

        # Persist updated position state
        ts_iso = now.isoformat()
        with self._tx() as conn:
            conn.execute(
                "UPDATE positions SET high_water = ?, current_stop = ?, trail_active = ?, "
                "last_price = ?, updated_at = ? WHERE id = ?",
                (round(hw, 2), round(cur_stop, 2), 1 if active else 0, round(ltp, 2), ts_iso, pos["id"]),
            )
            self._record_snapshot(conn, ts_iso)
        return None

    def square_off_all(self, price_map=None, reason="SAFETY_CUTOFF_1520", now=None):
        now = N.to_ist(now) if now else N.now_ist()
        closed = []
        for pos in self.get_open_positions():
            sym = pos["symbol"]
            px = float((price_map or {}).get(sym, pos["last_price"]))
            closed.append(self.close_position(pos["id"], px, reason=reason, now=now))
        return closed

    def get_metrics(self, date_str=None):
        with self._tx() as conn:
            meta = self._meta(conn)
            pos_rows = [dict(r) for r in conn.execute("SELECT * FROM positions ORDER BY id ASC").fetchall()]
            if date_str:
                trade_rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM trades WHERE exit_time LIKE ? ORDER BY id ASC", (date_str + "%",)
                ).fetchall()]
            else:
                trade_rows = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()]
            snaps = [dict(r) for r in conn.execute("SELECT * FROM equity_snapshots ORDER BY id ASC").fetchall()]

        init_cap = float(meta.get("initial_capital", self.initial_capital))
        cash = float(meta.get("cash", init_cap))
        deployed = sum(p["qty"] * p["entry_price"] for p in pos_rows)
        unrealized = round(sum((p["last_price"] - p["entry_price"]) * p["qty"] for p in pos_rows), 2)
        realized_all = float(meta.get("realized_pnl", 0.0))
        equity = round(cash + deployed + unrealized, 2)

        total_trades = len(trade_rows)
        gross_pnl = round(sum(t["gross_pnl"] for t in trade_rows), 2)
        total_charges = round(sum(t["charges"] for t in trade_rows), 2)
        net_pnl = round(sum(t["net_pnl"] for t in trade_rows), 2)

        wins_list = [t["net_pnl"] for t in trade_rows if t["net_pnl"] > 0]
        losses_list = [abs(t["net_pnl"]) for t in trade_rows if t["net_pnl"] <= 0]
        wins = len(wins_list)
        losses = len(losses_list)
        win_rate_pct = round((wins / total_trades) * 100.0, 2) if total_trades else 0.0

        sum_wins = sum(wins_list)
        sum_losses = sum(losses_list)
        if sum_losses > 0:
            profit_factor = round(sum_wins / sum_losses, 2)
        elif sum_wins > 0:
            profit_factor = 99.99
        else:
            profit_factor = 0.0

        cap_trades = [t["peak_capture_pct"] for t in trade_rows if t["peak_profit_inr"] > 0]
        avg_peak_capture_pct = round(sum(cap_trades) / len(cap_trades), 2) if cap_trades else 0.0

        pat_stats = {}
        for t in trade_rows:
            p = t["pattern"]
            st = pat_stats.setdefault(p, {"pattern": p, "trades": 0, "wins": 0, "net_pnl": 0.0,
                                          "gross_pnl": 0.0, "charges": 0.0, "captures": []})
            st["trades"] += 1
            if t["net_pnl"] > 0:
                st["wins"] += 1
            st["net_pnl"] = round(st["net_pnl"] + t["net_pnl"], 2)
            st["gross_pnl"] = round(st["gross_pnl"] + t["gross_pnl"], 2)
            st["charges"] = round(st["charges"] + t["charges"], 2)
            if t["peak_profit_inr"] > 0:
                st["captures"].append(t["peak_capture_pct"])

        for st in pat_stats.values():
            st["win_rate_pct"] = round((st["wins"] / st["trades"]) * 100.0, 2) if st["trades"] else 0.0
            caps = st.pop("captures")
            st["peak_capture_pct"] = round(sum(caps) / len(caps), 2) if caps else 0.0

        best_pattern = max(pat_stats.values(), key=lambda x: x["net_pnl"])["pattern"] if pat_stats else None
        worst_pattern = min(pat_stats.values(), key=lambda x: x["net_pnl"])["pattern"] if pat_stats else None

        return {
            "initial_capital": init_cap,
            "cash": round(cash, 2),
            "deployed": round(deployed, 2),
            "unrealized_pnl": unrealized,
            "realized_pnl": round(realized_all, 2),
            "equity": equity,
            "gross_pnl": gross_pnl,
            "total_charges": total_charges,
            "net_pnl": net_pnl,
            "return_pct": round((equity - init_cap) / init_cap * 100.0, 2) if init_cap else 0.0,
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": win_rate_pct,
            "profit_factor": profit_factor,
            "avg_peak_capture_pct": avg_peak_capture_pct,
            "best_pattern": best_pattern,
            "worst_pattern": worst_pattern,
            "pattern_stats": pat_stats,
            "open_positions": pos_rows,
            "closed_trades": trade_rows,
            "equity_snapshots": snaps,
        }


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Paper Broker (SQLite WAL)")
    p.add_argument("cmd", nargs="?", default="status", choices=("init", "status", "reset"))
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    broker = PaperBroker(db_path=args.db, initial_capital=args.capital)
    if args.cmd == "reset":
        broker.reset(args.capital)
        print("Reset %s to %s virtual capital." % (args.db, N.inr(args.capital)))
        return 0
    if args.cmd == "init":
        print("Initialized %s (WAL mode, %s capital)." % (args.db, N.inr(args.capital)))
        return 0

    m = broker.get_metrics()
    if args.json:
        print(json.dumps(m, indent=2))
        return 0
    print("Paper Broker (%s) | Equity %s (%+.2f%%) | Cash %s | Open %d | Closed %d"
          % (args.db, N.inr(m["equity"]), m["return_pct"], N.inr(m["cash"]),
             len(m["open_positions"]), m["total_trades"]))
    print("  Net PnL %s (Gross %s - Charges %s) | Win Rate %.1f%% | Profit Factor %.2f | Peak Capture %.1f%%"
          % (N.inr(m["net_pnl"]), N.inr(m["gross_pnl"]), N.inr(m["total_charges"]),
             m["win_rate_pct"], m["profit_factor"], m["avg_peak_capture_pct"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
