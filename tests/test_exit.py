"""Offline tests for trader-exit (NSE ratcheting trailing stop + 15:20 cutoff)."""
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


exit_m = load_mod("trader-exit", "exit")


class TestExit(unittest.TestCase):
    def setUp(self):
        self.cfg = N.load_config(None)

    def test_plan_tick_rounding_and_trailing_block(self):
        p = exit_m.build_plan(
            "RELIANCE", 2462.50, setup="orb", date_str="2026-11-18",
            entry_time_str="09:40 IST", entry_bar=4,
            or_high=2460.0, or_low=2445.25, target_rule="2R", cfg=self.cfg
        )
        # (2460 + 2445.25)/2 = 2452.625 -> tick 0.10 down -> 2452.60
        self.assertEqual(p["stop"], 2452.6)
        self.assertEqual(p["exit_mode"], "trailing")
        self.assertEqual(p["safety_cutoff"], "15:20")
        self.assertEqual(p["trailing"]["activate_pct"], 0.5)
        self.assertEqual(p["trailing"]["trail_pct"], 0.6)
        self.assertIn("at_stop_per_share", p["charges_estimate"])

    def test_refusals(self):
        with self.assertRaises(N.NSEError):
            exit_m.build_plan("RELIANCE", 2460.0, setup="level", stop_rule="price:2465.0", cfg=self.cfg)
        with self.assertRaises(N.NSEError):
            exit_m.build_plan("RELIANCE", 2460.0, setup="level", stop_rule="price:2459.9", cfg=self.cfg)  # < 2 ticks
        with self.assertRaises(N.NSEError):
            exit_m.build_plan("RELIANCE", 2460.0, setup="level", stop_rule="price:2440.0", entry_time_str="15:20 IST", cfg=self.cfg)

    def test_trail_ratchet_never_down_and_cutoff(self):
        p = exit_m.build_plan(
            "RELIANCE", 1000.0, setup="bull_flag", date_str="2026-11-18",
            entry_time_str="10:00 IST", entry_bar=1, pattern_low=990.0, target_rule="none", cfg=self.cfg
        )
        t0 = datetime.datetime(2026, 11, 18, 10, 5, tzinfo=N.IST)
        bars = [
            {"t": t0, "o": 1000, "h": 1020, "l": 999, "c": 1018, "v": 10000},   # +2% -> trail active, SL = 1013.80
            {"t": t0 + datetime.timedelta(minutes=5), "o": 1018, "h": 1019, "l": 1014.5, "c": 1015, "v": 10000},  # SL stays 1013.80
        ]
        p2, msg, ev = exit_m.update_trail_from_bars(p, bars, now=t0 + datetime.timedelta(minutes=10))
        self.assertIsNone(ev)
        self.assertTrue(p2["trailing"]["active"])
        self.assertEqual(p2["trailing"]["current_stop"], 1013.8)

        # Now hit trailing stop on next bar
        bars_hit = bars + [{"t": t0 + datetime.timedelta(minutes=10), "o": 1015, "h": 1016, "l": 1013.0, "c": 1013.5, "v": 10000}]
        p3, msg3, ev3 = exit_m.update_trail_from_bars(p, bars_hit, now=t0 + datetime.timedelta(minutes=15))
        self.assertEqual(ev3[0], "stopped")
        self.assertEqual(ev3[1], 1013.8)


if __name__ == "__main__":
    unittest.main()
