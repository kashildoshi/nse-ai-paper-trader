"""Offline tests for trader-risk (NSE INR checks, no US PDT)."""
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


risk = load_mod("trader-risk", "check")


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.cfg = N.load_config(os.path.join(ROOT, ".agent", "skills", "trader-budget", "config.example.json"))
        self.order = {
            "symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "buy",
            "qty": 50, "order_type": "market", "entry": 1000.0, "stop": 990.0, "target": 1020.0,
            "trailing": {"activate_pct": 0.5, "trail_pct": 0.6},
        }

    def test_pass_and_pdt_ignored(self):
        cfg = dict(self.cfg)
        cfg["_notes"] = ["pdt_guard is a US rule and is ignored on NSE."]
        v = risk.check_order_dict(self.order, cfg, now=N.parse_now("2026-11-18 10:15"))
        self.assertEqual(v["verdict"], "PASS")
        self.assertIn("pdt_guard is a US rule", v["warnings"][0])

    def test_blocks(self):
        # 15:20 cutoff
        v_cut = risk.check_order_dict(self.order, self.cfg, now=N.parse_now("2026-11-18 15:20"))
        self.assertEqual(v_cut["verdict"], "BLOCK")
        self.assertTrue(any("safety cutoff" in b for b in v_cut["blocks"]))

        # NSE holiday (2026-11-24 Guru Nanak Dev)
        v_hol = risk.check_order_dict(self.order, self.cfg, now=N.parse_now("2026-11-24 10:15"))
        self.assertEqual(v_hol["verdict"], "BLOCK")
        self.assertTrue(any("closed" in b for b in v_hol["blocks"]))

        # Invalid tick for >=1000 (tick is 0.10, .03 is invalid)
        bad_tick = dict(self.order, entry=1000.03)
        v_tk = risk.check_order_dict(bad_tick, self.cfg, now=N.parse_now("2026-11-18 10:15"))
        self.assertEqual(v_tk["verdict"], "BLOCK")
        self.assertTrue(any("tick" in b for b in v_tk["blocks"]))

        # Exit never blocked by cutoff
        pf = {"cash": 50000.0, "positions": {"RELIANCE": {"qty": 50, "avg_price": 1000.0}}}
        sell_ord = dict(self.order, side="sell", stop=None, target=None)
        v_ex = risk.check_order_dict(sell_ord, self.cfg, portfolio=pf, now=N.parse_now("2026-11-18 15:25"))
        self.assertEqual(v_ex["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
