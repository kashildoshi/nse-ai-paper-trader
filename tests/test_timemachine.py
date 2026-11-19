"""Offline tests for trader-timemachine (NSE trailing SL + 15:20 cutoff)."""
import datetime
import importlib.util
import os
import random
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


tm = load_mod("trader-timemachine", "timemachine")


def synth_sessions(n_days=22):
    rng = random.Random(7)
    bars = []
    d = datetime.date(2026, 10, 1)
    made = 0
    while made < n_days:
        if N.is_trading_day(d):
            px = 1000.0
            t0 = datetime.datetime.combine(d, N.OPEN, N.IST)
            for s in range(75):
                ret = rng.uniform(-0.0015, 0.0020)
                o = px
                c = round(o * (1 + ret), 2)
                h = round(max(o, c) * 1.0015, 2)
                l = round(min(o, c) * 0.9985, 2)
                bars.append({"t": t0 + datetime.timedelta(minutes=5 * s), "o": o, "h": h, "l": l, "c": c, "v": 100000})
                px = c
            made += 1
        d += datetime.timedelta(days=1)
    return bars


class TestTimeMachine(unittest.TestCase):
    def test_determinism_and_outcomes(self):
        bars = synth_sessions(22)
        mc1 = tm.simulate_trade(bars, 1000.0, 990.0, target_price=1020.0, replays=200, seed=19, plan_date=datetime.date(2026, 11, 18))
        mc2 = tm.simulate_trade(bars, 1000.0, 990.0, target_price=1020.0, replays=200, seed=19, plan_date=datetime.date(2026, 11, 18))
        self.assertEqual(mc1["win_prob_pct"], mc2["win_prob_pct"])
        self.assertEqual(mc1["outcomes"], mc2["outcomes"])
        self.assertAlmostEqual(sum(mc1["outcomes"].values()), 100.0, delta=0.1)
        self.assertLessEqual(mc1["avg_r_net"], mc1["avg_r_gross"])


if __name__ == "__main__":
    unittest.main()
