"""Offline end-to-end tests for PaperBroker (WAL), TelegramReporter, AutonomousRunner & Dashboard."""
import datetime
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "shared"))

import nse_common as N
from paper_broker import PaperBroker
from telegram_reporter import TelegramReporter
import nse_autonomous_runner as runner_mod
import dashboard


class TestAutonomousSystem(unittest.TestCase):
    def test_paper_broker_wal_slippage_charges_trailing_and_cutoff(self):
        with tempfile.TemporaryDirectory() as td:
            db_p = os.path.join(td, "paper_trades.db")
            broker = PaperBroker(db_path=db_p, initial_capital=100000.0)
            with sqlite3.connect(db_p) as conn:
                jm = conn.execute("PRAGMA journal_mode;").fetchone()[0]
                self.assertEqual(jm.lower(), "wal")

            # Open position: 20 shares @ signal 1000 -> +0.03% buy slippage = 1000.30
            t0 = N.parse_now("2026-11-18 10:00")
            pos = broker.open_position("RELIANCE", "orb", 20, 1000.0, 990.0, target_price=1030.0, win_prob_pct=62.5, now=t0)
            self.assertEqual(pos["entry_price"], 1000.30)

            # +0.4% (1004.30): trailing NOT active yet
            self.assertIsNone(broker.update_price_and_check_exit("RELIANCE", 1004.0, bar_high=1004.3, bar_low=1000.0,
                                                                 now=N.parse_now("2026-11-18 10:15")))
            p1 = broker.get_open_positions()[0]
            self.assertEqual(p1["trail_active"], 0)
            self.assertEqual(p1["current_stop"], 990.0)

            # +1.5% (1015.30): activates (+0.5% exceeded) and trails 0.6% below 1015.30 -> 1009.208 -> tick 1009.20
            self.assertIsNone(broker.update_price_and_check_exit("RELIANCE", 1014.0, bar_high=1015.3, bar_low=1005.0,
                                                                 now=N.parse_now("2026-11-18 10:30")))
            p2 = broker.get_open_positions()[0]
            self.assertEqual(p2["trail_active"], 1)
            self.assertEqual(p2["high_water"], 1015.3)
            self.assertEqual(p2["current_stop"], 1009.2)

            # Price pulls back slightly without touching stop -> stop does NOT drop
            self.assertIsNone(broker.update_price_and_check_exit("RELIANCE", 1011.0, bar_high=1012.0, bar_low=1010.0,
                                                                 now=N.parse_now("2026-11-18 10:40")))
            p3 = broker.get_open_positions()[0]
            self.assertEqual(p3["current_stop"], 1009.2)

            # Price hits trailing stop (bar_low <= 1009.2) -> exits at 1009.20 - 0.03% slippage = 1008.90
            tr = broker.update_price_and_check_exit("RELIANCE", 1009.0, bar_high=1010.0, bar_low=1008.5,
                                                    now=N.parse_now("2026-11-18 10:45"))
            self.assertIsNotNone(tr)
            self.assertEqual(tr["exit_reason"], "TRAILED_SL")
            self.assertEqual(tr["exit_price"], round(1009.2 * (1 - 0.0003), 2))
            self.assertGreater(tr["charges"], 0)
            self.assertGreater(tr["peak_capture_pct"], 50.0)
            self.assertEqual(tr["net_pnl"], round(tr["gross_pnl"] - tr["charges"], 2))

            # Open second trade and test 15:20 safety cutoff
            broker.open_position("TCS", "bull_flag", 10, 2000.0, 1980.0, win_prob_pct=58.0, now=N.parse_now("2026-11-18 11:00"))
            tr2 = broker.update_price_and_check_exit("TCS", 2008.0, bar_high=2009.0, bar_low=2005.0,
                                                     now=N.parse_now("2026-11-18 15:20"))
            self.assertIsNotNone(tr2)
            self.assertEqual(tr2["exit_reason"], "SAFETY_CUTOFF_1520")

            m = broker.get_metrics()
            self.assertEqual(m["total_trades"], 2)
            self.assertIn("orb", m["pattern_stats"])
            self.assertIn("bull_flag", m["pattern_stats"])

            # Dashboard query helper reads the exact WAL DB cleanly
            dash_data = dashboard.load_dashboard_data(db_path=db_p)
            self.assertEqual(dash_data["metrics"]["total_trades"], 2)
            self.assertEqual(len(dash_data["trades_df"]), 2)
            self.assertFalse(dash_data["snaps_df"].empty)

    def test_telegram_reporter_and_mock_send(self):
        with tempfile.TemporaryDirectory() as td:
            db_p = os.path.join(td, "paper_trades.db")
            outbox = os.path.join(td, "outbox.jsonl")
            broker = PaperBroker(db_path=db_p)
            pos = broker.open_position("RELIANCE", "orb", 10, 1000.0, 990.0, target_price=1020.0, win_prob_pct=60.0)
            tr = broker.close_position(pos["id"], 1015.0, reason="TRAILED_SL")

            # Unconfigured reporter logs locally without throwing
            rep_local = TelegramReporter(bot_token="", chat_id="", outbox_path=outbox)
            r1 = rep_local.notify_entry(pos)
            r2 = rep_local.notify_exit(tr)
            r3 = rep_local.send_daily_report(broker)
            self.assertFalse(r1["sent"])
            self.assertTrue(r1["logged"])
            self.assertIn("Net PnL", r2["text"])
            self.assertIn("Profit Factor", r3["text"])
            self.assertIn("Peak Capture", r3["text"])

            # Configured reporter POSTs to Telegram API
            rep_live = TelegramReporter(bot_token="123456:ABC", chat_id="-100999", outbox_path=outbox)
            fake_resp = mock.MagicMock(status_code=200, text="ok")
            with mock.patch("telegram_reporter.requests.post", return_value=fake_resp) as m_post:
                res = rep_live.send_daily_report(broker)
                self.assertTrue(res["sent"])
                self.assertEqual(m_post.call_count, 1)

    def test_runner_demo_session_and_win_odds_gate(self):
        with tempfile.TemporaryDirectory() as td:
            db_p = os.path.join(td, "demo.db")
            outbox = os.path.join(td, "outbox.jsonl")
            out = runner_mod.run_demo_session(db_path=db_p, outbox_path=outbox)
            m = out["metrics"]
            self.assertGreaterEqual(m["total_trades"], 2)
            self.assertGreater(m["total_charges"], 0)
            self.assertIsNotNone(m["best_pattern"])
            self.assertTrue(os.path.exists(outbox))

            # Verify gate rejects setups with <= 55% TimeMachine win odds
            day = datetime.date(2026, 11, 18)
            bars_bad = runner_mod._synth_60d_and_today("SBIN", "orb", 800.0, day, seed=99)
            cfg = N.load_config(None)
            cfg["budget"] = 100000.0
            cfg["min_avg_turnover_cr"] = 1.0
            r = runner_mod.AutonomousRunner(
                db_path=os.path.join(td, "gate.db"),
                outbox_path=os.path.join(td, "gate.jsonl"),
                min_win_prob=99.9,  # force win odds gate rejection
                replays=100,
                cfg=cfg,
                data_provider=lambda s: bars_bad if s == "SBIN" else [],
            )
            r._universe_symbols = lambda: ["SBIN"]
            res = r.scan_and_enter_cycle(now=datetime.datetime.combine(day, datetime.time(10, 0), N.IST))
            self.assertEqual(len(res["entries"]), 0)
            self.assertTrue(any("win_prob" in s["reason"] for s in res["skipped"]))


if __name__ == "__main__":
    unittest.main()
