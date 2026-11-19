"""Offline tests for trader-scanner (NSE edition)."""
import datetime
import importlib.util
import json
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys_shared = os.path.join(ROOT, "shared")
import sys
if sys_shared not in sys.path:
    sys.path.insert(0, sys_shared)
import nse_common as N


def load_mod(skill, name):
    p = os.path.join(ROOT, ".agent", "skills", skill, name + ".py")
    spec = importlib.util.spec_from_file_location("t_" + skill + "_" + name, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scan = load_mod("trader-scanner", "scan")


def make_bars(day, rows):
    t0 = datetime.datetime.combine(day, N.OPEN, N.IST)
    return [
        {"t": t0 + datetime.timedelta(minutes=5 * i), "o": o, "h": h, "l": l, "c": c, "v": v}
        for i, (o, h, l, c, v) in enumerate(rows)
    ]


class TestScanner(unittest.TestCase):
    def setUp(self):
        self.day = datetime.date(2026, 11, 18)

    def test_universe_csv_and_fallback(self):
        csv_txt = (
            "Company Name,Industry,Symbol,Series,ISIN Code\n"
            "Reliance Industries Ltd.,Oil Gas,RELIANCE,EQ,INE002A01018\n"
            "Tata Consultancy Services Ltd.,IT,TCS,EQ,INE467B01029\n"
            "Some SME Stock,Misc,SMESTOCK,BE,INE999A01011\n"
        )
        with tempfile.TemporaryDirectory() as td:
            csv_p = os.path.join(td, "ind.csv")
            out_p = os.path.join(td, "universe.txt")
            with open(csv_p, "w", encoding="utf-8") as f:
                f.write(csv_txt)
            clean, _ = scan.build_universe(offline=csv_p, out_path=out_p)
            self.assertEqual([r[0] for r in clean], ["RELIANCE", "TCS"])
            self.assertEqual(clean[0][2], "INE002A01018")

    def test_orb_positive_and_volume_reject(self):
        # First 3 bars (09:15, 09:20, 09:25) range 995..1010; bar 3 (09:30) closes at 1015 -> earliest ORB hit at 09:35 IST
        bars = make_bars(self.day, [
            (1000, 1008, 995, 1004, 100000),
            (1004, 1010, 998, 1006, 90000),
            (1006, 1009, 1000, 1005, 80000),
            (1005, 1017, 1004, 1015, 160000),  # 1.78x vol
        ])
        hit = scan.detect_orb(bars, or_minutes=15, min_vol_ratio=1.5)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["setup"], "orb")
        self.assertEqual(hit["time"], "09:35 IST")
        self.assertGreaterEqual(hit["volume_ratio"], 1.5)

        # Low volume breakout rejected
        bars_low_v = make_bars(self.day, [
            (1000, 1008, 995, 1004, 100000),
            (1004, 1010, 998, 1006, 90000),
            (1006, 1009, 1000, 1005, 80000),
            (1005, 1017, 1004, 1015, 95000),  # 1.06x < 1.5x
        ])
        self.assertIsNone(scan.detect_orb(bars_low_v, or_minutes=15, min_vol_ratio=1.5))

    def test_bull_flag_positive_and_negative(self):
        bars = make_bars(self.day, [
            (1000, 1006, 998, 1005, 150000),
            (1005, 1014, 1004, 1013, 160000),
            (1013, 1022, 1012, 1020, 170000),
            (1020, 1029, 1019, 1027, 160000),
            (1027, 1028, 1022, 1024, 65000),
            (1024, 1026, 1021, 1023, 60000),
            (1023, 1027, 1022, 1025, 55000),
            (1025, 1036, 1024, 1034, 210000),
        ])
        hit = scan.detect_bull_flag(bars, min_vol_ratio=1.5)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["setup"], "bull_flag")
        self.assertEqual(hit["levels"]["flag_low"], 1021.0)
        # Negative: flat bars without pole
        flat = make_bars(self.day, [(1000, 1002, 999, 1001, 100000)] * 9)
        self.assertIsNone(scan.detect_bull_flag(flat))

    def test_triangle_positive_and_negative(self):
        bars = make_bars(self.day, [
            (1000, 1014, 994, 1004, 90000),
            (1004, 1013, 996, 1008, 85000),
            (1008, 1014, 998, 1006, 80000),
            (1006, 1012, 1000, 1010, 75000),
            (1010, 1013, 1003, 1008, 70000),
            (1008, 1012, 1005, 1010, 65000),
            (1010, 1013, 1006, 1011, 60000),
            (1011, 1020, 1010, 1018, 180000),
        ])
        hit = scan.detect_triangle(bars, min_vol_ratio=1.5)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["setup"], "triangle")
        # Negative: widening range fails
        wide = make_bars(self.day, [
            (1000, 1002, 998, 1000, 80000),
            (1000, 1003, 997, 1001, 80000),
            (1001, 1004, 996, 1002, 80000),
            (1002, 1010, 990, 1003, 80000),
            (1003, 1015, 985, 1004, 80000),
            (1004, 1016, 984, 1005, 80000),
            (1005, 1025, 1004, 1024, 180000),
        ])
        self.assertIsNone(scan.detect_triangle(wide))

    def test_vwap_reclaim_and_liquidity_filter(self):
        bars = make_bars(self.day, [
            (1000, 1006, 998, 1004, 140000),
            (1004, 1008, 1002, 1006, 120000),
            (1006, 1007, 999, 1001, 90000),
            (1001, 1002, 994, 995, 80000),
            (995, 999, 992, 996, 75000),
            (996, 999, 993, 997, 70000),
            (997, 1011, 996, 1010, 195000),
        ])
        hit = scan.detect_vwap_reclaim(bars, min_vol_ratio=1.5)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["setup"], "vwap_reclaim")

        # Illiquid stock fails liquidity check
        thin = make_bars(self.day, [(30, 31, 29, 30, 100)] * 5)
        ok, _ = scan.passes_liquidity(thin, min_price=50, min_turnover_cr=25)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
