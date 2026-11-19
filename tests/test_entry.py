"""Offline tests for trader-entry (NSE edition)."""
import datetime
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
import nse_common as N


def load_mod(skill, name):
    p = os.path.join(ROOT, ".agent", "skills", skill, name + ".py")
    spec = importlib.util.spec_from_file_location("t_" + skill + "_" + name, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


entry = load_mod("trader-entry", "entry")


def make_bars(day, rows):
    t0 = datetime.datetime.combine(day, N.OPEN, N.IST)
    return [{"t": t0 + datetime.timedelta(minutes=5 * i), "o": o, "h": h, "l": l, "c": c, "v": v}
            for i, (o, h, l, c, v) in enumerate(rows)]


class TestEntry(unittest.TestCase):
    def setUp(self):
        self.day = datetime.date(2026, 11, 18)
        self.cfg = N.load_config(None)

    def test_orb_triggered_waiting_and_expired(self):
        bars_wait = make_bars(self.day, [
            (2450, 2460, 2445, 2455, 100000),
            (2455, 2462, 2450, 2458, 90000),
            (2458, 2461, 2452, 2459, 80000),
            (2459, 2463, 2454, 2460, 120000),  # close <= 2462
        ])
        res_w = entry.evaluate_entry("RELIANCE", bars_wait, setup="orb", now=N.parse_now("2026-11-18 09:35"), cfg=self.cfg)
        self.assertEqual(res_w["status"], "waiting")

        res_exp = entry.evaluate_entry("RELIANCE", bars_wait, setup="orb", now=N.parse_now("2026-11-18 15:21"), cfg=self.cfg)
        self.assertEqual(res_exp["status"], "expired")

        bars_trig = bars_wait + [
            {"t": datetime.datetime.combine(self.day, datetime.time(9, 35), N.IST),
             "o": 2460, "h": 2472, "l": 2459, "c": 2470.0, "v": 200000}
        ]
        res_t = entry.evaluate_entry("RELIANCE", bars_trig, setup="orb", now=N.parse_now("2026-11-18 09:40"), cfg=self.cfg)
        self.assertEqual(res_t["status"], "triggered")
        self.assertEqual(res_t["entry"], 2470.0)
        self.assertEqual(res_t["time"], "09:40 IST")
        self.assertIn("pattern_low", res_t["levels"])
        self.assertGreater(res_t["vwap"], 0)


if __name__ == "__main__":
    unittest.main()
