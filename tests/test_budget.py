"""Offline tests for trader-budget (NSE INR sizing incl. charges)."""
import importlib.util
import json
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


budget_m = load_mod("trader-budget", "size")


class TestBudget(unittest.TestCase):
    def test_charges_reduce_share_count_when_tight(self):
        cfg = N.load_config(os.path.join(ROOT, ".agent", "skills", "trader-budget", "config.example.json"))
        self.assertTrue(cfg["paper"])
        self.assertEqual(cfg["budget"], 100000)
        # Raw 1000 / 10 = 100 shares, but charges (~₹88) push 100*10+88 > 1000, so must drop to <= 91 shares!
        plan = {
            "symbol": "RELIANCE", "date": "2026-11-18", "setup": "orb",
            "entry": 1000.0, "stop": 990.0, "target": 1020.0, "risk_per_share": 10.0,
            "trailing": {"activate_pct": 0.5, "trail_pct": 0.6}, "safety_cutoff": "15:20",
        }
        order, _ = budget_m.size_plan_order(plan, cfg, now=N.parse_now("2026-11-18 10:15"))
        self.assertLess(order["qty"], 100)
        self.assertLessEqual(order["max_loss_inr"], 1000.0)
        self.assertEqual(order["exchange"], "NSE")
        self.assertEqual(order["product"], "MIS")
        self.assertEqual(order["flat_by"], "15:20")
        self.assertGreater(order["breakeven_price"], 1000.0)


if __name__ == "__main__":
    unittest.main()
