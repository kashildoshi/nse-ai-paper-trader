#!/usr/bin/env python3
"""Telegram Alerts & 15:30 IST Daily Insights Reporter for NSE Paper Trading.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env (via python-dotenv).
Sends instant alerts on every auto-entry and auto-exit, plus the full Daily
Performance Report at 15:30 IST (Net PnL, Win Rate %, Profit Factor,
Peak Capture %, and Best/Worst pattern). Always logs messages to
trader/telegram_outbox.jsonl so local runs and tests stay auditable even when
Telegram credentials are unset.

    py telegram_reporter.py report
    py telegram_reporter.py test
"""

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "shared"))
import nse_common as N
from paper_broker import DEFAULT_DB, PaperBroker

DEFAULT_OUTBOX = os.path.join(ROOT, "trader", "telegram_outbox.jsonl")
PLACEHOLDER_TOKENS = ("your_telegram_bot_token_here", "your_telegram_chat_id_here", "")


class TelegramReporter:
    def __init__(self, bot_token=None, chat_id=None, outbox_path=DEFAULT_OUTBOX, timeout=10):
        load_dotenv(os.path.join(ROOT, ".env"))
        self.bot_token = (bot_token if bot_token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
        self.outbox_path = outbox_path
        self.timeout = timeout

    def is_configured(self):
        return (
            self.bot_token not in PLACEHOLDER_TOKENS
            and self.chat_id not in PLACEHOLDER_TOKENS
            and ":" in self.bot_token
        )

    def _log_outbox(self, kind, text, sent, detail=""):
        os.makedirs(os.path.dirname(self.outbox_path) or ".", exist_ok=True)
        rec = {
            "timestamp": N.now_ist().isoformat(),
            "kind": kind,
            "sent": bool(sent),
            "detail": detail,
            "text": text,
        }
        with open(self.outbox_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def send_message(self, text, kind="message", parse_mode="HTML"):
        if not self.is_configured():
            self._log_outbox(kind, text, False, "telegram_not_configured")
            return {"sent": False, "logged": True, "reason": "telegram_not_configured", "text": text}

        url = "https://api.telegram.org/bot%s/sendMessage" % self.bot_token
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            ok = resp.status_code == 200
            detail = "ok" if ok else ("http_%d: %s" % (resp.status_code, resp.text[:160]))
            self._log_outbox(kind, text, ok, detail)
            return {"sent": ok, "logged": True, "status_code": resp.status_code, "detail": detail, "text": text}
        except Exception as e:
            self._log_outbox(kind, text, False, str(e))
            return {"sent": False, "logged": True, "reason": str(e), "text": text}

    def format_entry(self, pos):
        odds = ("%.1f%%" % pos["win_prob_pct"]) if pos.get("win_prob_pct") is not None else "N/A"
        tgt = N.inr(pos["target_price"]) if pos.get("target_price") is not None else "Trailing-only"
        deployed = pos["qty"] * pos["entry_price"]
        return "\n".join([
            "<b>[PAPER ENTRY] %s (%s)</b>" % (pos["symbol"], pos["pattern"].upper()),
            "Qty: %d shares | Deployed: %s" % (pos["qty"], N.inr(deployed)),
            "Signal: %s -> Fill (+0.03%% slip): %s" % (N.inr(pos["signal_price"]), N.inr(pos["entry_price"])),
            "Initial SL: %s | Target: %s" % (N.inr(pos["initial_stop"]), tgt),
            "Trailing SL: Arms at +%g%%, trails %g%% below peak" % (pos.get("trail_activate_pct", 0.5), pos.get("trail_pct", 0.6)),
            "TimeMachine Win Odds: %s | Safety Cutoff: 15:20 IST" % odds,
            "Time: %s" % pos.get("entry_time", "")[:19],
        ])

    def notify_entry(self, pos):
        return self.send_message(self.format_entry(pos), kind="entry")

    def format_exit(self, trade):
        ch = json.loads(trade.get("charges_json") or "{}")
        sign = "+" if trade["net_pnl"] >= 0 else ""
        return "\n".join([
            "<b>[PAPER EXIT - %s] %s (%s)</b>" % (trade["exit_reason"], trade["symbol"], trade["pattern"].upper()),
            "Qty: %d shares | Entry: %s -> Exit (-0.03%% slip): %s"
            % (trade["qty"], N.inr(trade["entry_price"]), N.inr(trade["exit_price"])),
            "High Water: %s | Final SL: %s | Peak Capture: %.1f%%"
            % (N.inr(trade["high_water"]), N.inr(trade["final_stop"]), trade["peak_capture_pct"]),
            "Gross PnL: %s | Taxes/Charges: %s (STT %s, Brok %s, GST %s)"
            % (N.inr(trade["gross_pnl"]), N.inr(trade["charges"]),
               N.inr(ch.get("stt", 0)), N.inr(ch.get("brokerage", 0)), N.inr(ch.get("gst", 0))),
            "<b>Net PnL: %s%s</b>" % (sign, N.inr(trade["net_pnl"])),
            "Time: %s" % trade.get("exit_time", "")[:19],
        ])

    def notify_exit(self, trade):
        return self.send_message(self.format_exit(trade), kind="exit")

    def format_daily_report(self, m, date_str=None):
        d_label = date_str or N.now_ist().date().isoformat()
        best = m.get("best_pattern") or "-"
        worst = m.get("worst_pattern") or "-"
        pat_lines = []
        for pat, st in sorted((m.get("pattern_stats") or {}).items(), key=lambda kv: kv[1]["net_pnl"], reverse=True):
            pat_lines.append(
                "  - %s: %d trade(s), Net %s, Win %.1f%%, PeakCap %.1f%%"
                % (pat, st["trades"], N.inr(st["net_pnl"]), st["win_rate_pct"], st["peak_capture_pct"])
            )
        best_line = "%s (%s)" % (best, N.inr(m["pattern_stats"][best]["net_pnl"])) if best in (m.get("pattern_stats") or {}) else "-"
        worst_line = "%s (%s)" % (worst, N.inr(m["pattern_stats"][worst]["net_pnl"])) if worst in (m.get("pattern_stats") or {}) else "-"

        lines = [
            "<b>[15:30 IST DAILY PAPER REPORT] %s</b>" % d_label,
            "Equity: %s (%+.2f%% vs %s)" % (N.inr(m["equity"]), m["return_pct"], N.inr(m["initial_capital"])),
            "Net PnL: %s (Gross %s | Intraday STT/Taxes/Brok: %s)"
            % (N.inr(m["net_pnl"]), N.inr(m["gross_pnl"]), N.inr(m["total_charges"])),
            "Trades: %d (%dW / %dL) | Win Rate: %.1f%%"
            % (m["total_trades"], m["wins"], m["losses"], m["win_rate_pct"]),
            "Profit Factor: %.2f | Avg Peak Capture: %.1f%%"
            % (m["profit_factor"], m["avg_peak_capture_pct"]),
            "Best Pattern: %s" % best_line,
            "Worst Pattern: %s" % worst_line,
        ]
        if pat_lines:
            lines.append("Pattern Breakdown:")
            lines.extend(pat_lines)
        return "\n".join(lines)

    def send_daily_report(self, broker, date_str=None):
        m = broker.get_metrics(date_str=date_str)
        text = self.format_daily_report(m, date_str=date_str)
        return self.send_message(text, kind="daily_report")


def main(argv=None):
    p = argparse.ArgumentParser(description="NSE Telegram Alerts & 15:30 IST Daily Reporter")
    p.add_argument("cmd", nargs="?", default="report", choices=("report", "test"))
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--date", help="Filter trades by YYYY-MM-DD")
    args = p.parse_args(argv)

    rep = TelegramReporter()
    if args.cmd == "test":
        res = rep.send_message("<b>[NSE PAPER BOT]</b> Telegram reporter connectivity check OK.", kind="test")
        print("Sent=%s | %s" % (res["sent"], res.get("detail") or res.get("reason")))
        return 0

    broker = PaperBroker(db_path=args.db)
    res = rep.send_daily_report(broker, date_str=args.date)
    print(res["text"])
    print("Telegram delivered=%s (%s)" % (res["sent"], res.get("detail") or res.get("reason")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
