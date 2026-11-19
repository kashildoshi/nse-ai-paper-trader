"""Offline tests for trader-pelosi (NSE Bulk/Block Deals & Insider Tracker)."""
import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_mod(skill, name):
    p = os.path.join(ROOT, ".agent", "skills", skill, name + ".py")
    spec = importlib.util.spec_from_file_location("t_" + skill + "_" + name, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


deals_m = load_mod("trader-pelosi", "deals")


class TestPelosiNSE(unittest.TestCase):
    def test_bulk_block_and_pit_normalizers(self):
        b = deals_m.normalize_deal_row({
            "BD_DT_DATE": "18-Nov-2026", "BD_SYMBOL": "RELIANCE",
            "BD_CLIENT_NAME": "ALPHA FUND", "BD_BUY_SELL": "BUY",
            "BD_QTY_TRD": "500000", "BD_TP_WATP": "2460.00"
        }, "bulk")
        self.assertEqual(b["symbol"], "RELIANCE")
        self.assertEqual(b["side"], "BUY")
        self.assertEqual(b["value_cr"], 123.0)

        pit = deals_m.normalize_pit_row({
            "symbol": "TATAMOTORS", "company": "Tata Motors Ltd",
            "acqName": "Promoter Group Co", "personCategory": "Promoter Group",
            "tdpTransactionType": "Buy", "acqMode": "Market Purchase",
            "secAcq": "100000", "secVal": "92000000", "date": "18-NOV-2026"
        })
        self.assertTrue(pit["is_promoter_market_buy"])
        self.assertEqual(pit["value_cr"], 9.2)

        agg = deals_m.aggregate_by_symbol([b, pit])
        self.assertEqual(len(agg), 2)
        self.assertEqual(agg[0]["symbol"], "RELIANCE")

    def test_403_fallback_to_rss(self):
        rss_items = [{"title": "RELIANCE - Promoter Market Purchase", "summary": "Promoter buy", "link": "https://x", "published": None}]
        with mock.patch.object(deals_m.N.NSESession, "get_json", side_effect=deals_m.N.NSEError("NSE refused (HTTP 403)")):
            with mock.patch.object(deals_m.N, "fetch_rss", return_value=rss_items):
                rows, notes = deals_m.fetch_insider(days=3, promoters_only=True, buys_only=True)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["symbol"], "RELIANCE")
                self.assertTrue(any("403" in n for n in notes))

    def test_watchlist_crosscheck(self):
        with tempfile.TemporaryDirectory() as td:
            wl = os.path.join(td, "wl.json")
            pf = os.path.join(td, "pf.json")
            with open(wl, "w") as f:
                json.dump({"hits": [{"symbol": "RELIANCE"}]}, f)
            with open(pf, "w") as f:
                json.dump({"positions": {"TCS": {"qty": 10}}}, f)
            rows = [{"symbol": "RELIANCE", "deal_type": "bulk", "date": "2026-11-18", "value_cr": 12.0},
                    {"symbol": "INFY", "deal_type": "bulk", "date": "2026-11-18", "value_cr": 5.0}]
            hits, watched = deals_m.cross_check_watchlist(rows, wl, pf)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["symbol"], "RELIANCE")
            self.assertEqual(watched, ["RELIANCE", "TCS"])


if __name__ == "__main__":
    unittest.main()
