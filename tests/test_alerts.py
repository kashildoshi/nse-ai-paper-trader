"""Offline tests for trader-alerts (NSE clock, trail rule, 15:20 cutoff)."""
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


alerts = load_mod("trader-alerts", "alerts")


class TestAlerts(unittest.TestCase):
    def test_from_plan_trail_and_cutoff(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            try:
                os.chdir(td)
                os.makedirs("trader/state", exist_ok=True)
                with open("trader/plan.json", "w") as f:
                    json.dump({
                        "symbol": "RELIANCE", "date": "2026-11-18", "entry": 1000.0, "stop": 990.0,
                        "target": 1020.0, "target_label": "2R", "safety_cutoff": "15:20",
                        "trailing": {"activate_pct": 0.5, "trail_pct": 0.6, "current_stop": 990.0, "high_water": 1000.0, "active": False}
                    }, f)
                self.assertEqual(alerts.main(["add", "--from-plan"]), 0)
                with open("trader/alerts.json") as f:
                    adoc = json.load(f)
                ids = {r["id"] for r in adoc["rules"]}
                self.assertIn("reliance-plan-trail", ids)
                self.assertIn("reliance-safety-cutoff", ids)

                # Step 1: price rises to 1020 -> trail ratchets to 1013.80
                with open(os.path.join(td, "q.json"), "w") as f:
                    json.dump({"RELIANCE": {"price": 1018.0, "high": 1020.0, "low": 1002.0, "prev_close": 1000.0}}, f)
                rc = alerts.main(["check", "--offline", os.path.join(td, "q.json"), "--now", "2026-11-18 11:00"])
                self.assertEqual(rc, 0)
                with open("trader/alerts.json") as f:
                    adoc2 = json.load(f)
                tr_rule = [r for r in adoc2["rules"] if r["id"] == "reliance-plan-trail"][0]
                self.assertEqual(tr_rule["price"], 1013.8)

                # Step 2: hit 15:20 cutoff -> rings once
                with open(os.path.join(td, "q.json"), "w") as f:
                    json.dump({"RELIANCE": {"price": 1016.0, "high": 1017.0, "low": 1015.0, "prev_close": 1000.0}}, f)
                rc2 = alerts.main(["check", "--offline", os.path.join(td, "q.json"), "--now", "2026-11-18 15:20"])
                self.assertEqual(rc2, 0)
                rc3 = alerts.main(["check", "--offline", os.path.join(td, "q.json"), "--now", "2026-11-18 15:21"])
                self.assertEqual(rc3, 1)  # did not re-ring
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
