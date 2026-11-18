"""Offline tests for shared/nse_common.py."""
import datetime
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
import nse_common as N  # noqa: E402


class Clock(unittest.TestCase):
    def test_holidays_and_weekends(self):
        self.assertFalse(N.is_trading_day(datetime.date(2026, 11, 24)))  # Guru Nanak
        self.assertFalse(N.is_trading_day(datetime.date(2026, 11, 21)))  # Saturday
        self.assertTrue(N.is_trading_day(datetime.date(2026, 11, 18)))
        self.assertTrue(N.is_trading_day(datetime.date(2026, 11, 8), {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}))

    def test_parse_now_ist(self):
        dt = N.parse_now("2026-11-18 10:15")
        self.assertEqual(dt.utcoffset(), datetime.timedelta(hours=5, minutes=30))
        self.assertEqual(N.parse_now("2026-11-18T04:45:00Z").strftime("%H:%M"), "10:15")

    def test_market_state(self):
        self.assertEqual(N.market_state(N.parse_now("2026-11-18 09:05")), "pre_open")
        self.assertEqual(N.market_state(N.parse_now("2026-11-18 09:15")), "open")
        self.assertEqual(N.market_state(N.parse_now("2026-11-18 15:30")), "post_close")


class Money(unittest.TestCase):
    def test_inr_grouping(self):
        self.assertEqual(N.inr(123456.5, symbol="Rs "), "Rs 1,23,456.50")
        self.assertEqual(N.inr(12345678, 0, symbol=""), "1,23,45,678")
        self.assertEqual(N.inr(999.999, symbol=""), "1,000.00")

    def test_ticks(self):
        self.assertEqual(N.tick_size(249.99), 0.01)
        self.assertEqual(N.tick_size(2450), 0.10)
        self.assertEqual(N.round_tick(2451.37, "down"), 2451.3)
        self.assertEqual(N.round_tick(2451.31, "up"), 2451.4)
        self.assertEqual(N.round_tick(612.33, "nearest"), 612.35)

    def test_charges(self):
        ch = N.intraday_charges(100, 1000, 1000)  # Rs 1 lakh each side
        self.assertEqual(ch["brokerage"], 40.0)
        self.assertEqual(ch["stt"], 25.0)
        self.assertEqual(ch["stamp_duty"], 3.0)
        self.assertAlmostEqual(ch["exchange_txn"], 5.94, places=2)
        self.assertAlmostEqual(ch["gst"], round((40 + 5.94 + 0.2) * 0.18, 2), places=2)
        be = N.breakeven_price(100, 1000)
        self.assertGreater(be, 1000)


class Symbols(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(N.norm_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(N.norm_symbol("NSE:M&M"), "M&M")
        self.assertEqual(N.yahoo_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO.NS")
        with self.assertRaises(N.NSEError):
            N.norm_symbol("500325.BO")
        self.assertIn("RELIANCE", N.LIQUID_UNIVERSE)


class Trailing(unittest.TestCase):
    def test_ratchet(self):
        s = N.trail_init(1000, 990)
        s, ev = N.trail_update(s, 1004, 998)          # +0.4%: not active
        self.assertIsNone(ev)
        self.assertEqual(s["stop"], 990)
        s, ev = N.trail_update(s, 1005, 1001)         # +0.5%: activates, stop 1005*0.994=998.97 -> 998.95 (0.05 tick)
        self.assertEqual(ev, "activated")
        self.assertEqual(s["stop"], 998.95)
        s, ev = N.trail_update(s, 1020, 1010)         # 1020*0.994 = 1013.88 -> 1013.8
        self.assertEqual(s["stop"], 1013.8)
        s, ev = N.trail_update(s, 1015, 1014)         # lower high: stop does not move down
        self.assertEqual(s["stop"], 1013.8)
        s, ev = N.trail_update(s, 1016, 1013.5)       # low through stop
        self.assertEqual(ev, "stopped")
        self.assertEqual(s["exit_price"], 1013.8)


class Parsing(unittest.TestCase):
    def test_rss(self):
        xml = ("<rss><channel><title>NSE</title><item><title>RELIANCE - Outcome of Board Meeting</title>"
               "<link>https://x</link><pubDate>Wed, 18 Nov 2026 10:05:00 +0530</pubDate>"
               "<description>&lt;b&gt;Results&lt;/b&gt;</description></item></channel></rss>")
        it = N.parse_rss(xml)[0]
        self.assertEqual(it["published"].strftime("%H:%M"), "10:05")
        self.assertEqual(it["summary"], "Results")

    def test_nse_date(self):
        self.assertEqual(N.parse_nse_date("18-NOV-2026 17:45:01").hour, 17)
        self.assertEqual(N.parse_nse_date("18-11-2026").day, 18)

    def test_yahoo_bars(self):
        t0 = int(datetime.datetime(2026, 11, 18, 9, 15, tzinfo=N.IST).timestamp())
        doc = {"chart": {"result": [{"meta": {}, "timestamp": [t0, t0 + 300, t0 + 437],
               "indicators": {"quote": [{"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
                                         "close": [1, None, 3], "volume": [10, 20, 30]}]}}]}}
        bars, _ = N.bars_from_yahoo(doc, 5)
        self.assertEqual(len(bars), 1)  # null close dropped, off-grid tick dropped
        self.assertEqual(bars[0]["t"].strftime("%H:%M"), "09:15")


if __name__ == "__main__":
    unittest.main()
