"""Offline tests for trader-broker (Upstox/Angel One payloads, paper-fill, approval)."""
import importlib.util
import json
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_mod(skill, name):
    p = os.path.join(ROOT, ".agent", "skills", skill, name + ".py")
    spec = importlib.util.spec_from_file_location("t_" + skill + "_" + name, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


broker_m = load_mod("trader-broker", "ticket")
risk_m = load_mod("trader-risk", "check")


class TestBroker(unittest.TestCase):
    def test_payloads_approval_and_paper_fill(self):
        ord_d = {
            "symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "buy",
            "qty": 10, "order_type": "market", "entry": 2460.0, "stop": 2445.0, "target": 2490.0,
            "cost_inr": 24600.0, "max_loss_inr": 200.0,
        }
        up = broker_m.build_broker_payload(ord_d, "upstox", isin_map={"RELIANCE": "INE002A01018"})
        self.assertEqual(up["entry_order"]["instrument_token"], "NSE_EQ|INE002A01018")
        self.assertEqual(up["entry_order"]["product"], "I")
        self.assertEqual(up["stop_loss_order"]["order_type"], "SL-M")

        ag = broker_m.build_broker_payload(ord_d, "angelone", angel_tokens={"RELIANCE": "2885"})
        self.assertEqual(ag["entry_order"]["tradingsymbol"], "RELIANCE-EQ")
        self.assertEqual(ag["entry_order"]["producttype"], "INTRADAY")

        ok_yes, _ = broker_m._validate_yes("yes", ord_d)
        self.assertTrue(ok_yes)
        bad_all, _ = broker_m._validate_yes("yes to all", ord_d)
        self.assertFalse(bad_all)

        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            try:
                os.chdir(td)
                os.makedirs("trader/state", exist_ok=True)
                with open("trader/config.json", "w") as f:
                    json.dump({"budget": 100000, "paper": True, "broker": "upstox"}, f)
                with open("trader/orders.json", "w") as f:
                    json.dump({"orders": [ord_d]}, f)
                # Must be blocked before risk pass
                self.assertEqual(broker_m.main(["paper-fill", "--index", "1", "--now", "2026-11-18 10:15"]), 1)
                # Run risk check -> PASS
                self.assertEqual(risk_m.main(["--now", "2026-11-18 10:15"]), 0)
                # Now paper-fill succeeds
                self.assertEqual(broker_m.main(["paper-fill", "--index", "1", "--now", "2026-11-18 10:15"]), 0)
                self.assertTrue(os.path.exists("trader/paper_fills.json"))
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
