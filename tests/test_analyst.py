"""Offline tests for trader-analyst (NSE quarterly results & Ind-AS XBRL)."""
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


facts_m = load_mod("trader-analyst", "facts")


class TestAnalystNSE(unittest.TestCase):
    def test_json_growth_and_xbrl_parser(self):
        # 5 quarters (in lakhs) so index 0 vs 4 gives YoY
        rows = [
            {"toDate": "30-Sep-2026", "totalIncome": "110000", "reProLossBefTax": "22000", "netProfit": "16500", "eps": "24.5", "consolidated": "Consolidated"},
            {"toDate": "30-Jun-2026", "totalIncome": "105000", "reProLossBefTax": "20500", "netProfit": "15500", "eps": "23.0", "consolidated": "Consolidated"},
            {"toDate": "31-Mar-2026", "totalIncome": "102000", "reProLossBefTax": "19500", "netProfit": "14800", "eps": "22.0", "consolidated": "Consolidated"},
            {"toDate": "31-Dec-2025", "totalIncome": "101000", "reProLossBefTax": "19000", "netProfit": "14500", "eps": "21.5", "consolidated": "Consolidated"},
            {"toDate": "30-Sep-2025", "totalIncome": "100000", "reProLossBefTax": "18000", "netProfit": "15000", "eps": "22.2", "consolidated": "Consolidated"},
        ]
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "results-TCS.json"), "w") as f:
                json.dump(rows, f)
            rep = facts_m.analyze_symbol("TCS", quarters_n=5, offline=td)
            q0 = rep["quarters"][0]
            self.assertEqual(q0["revenue_cr"], 1100.0)
            self.assertEqual(q0["net_profit_cr"], 165.0)
            self.assertEqual(q0["yoy_rev_pct"], 10.0)
            self.assertEqual(q0["yoy_pat_pct"], 10.0)
            self.assertIsNotNone(rep["ttm"])
            self.assertEqual(rep["ttm"]["revenue_cr"], 4180.0)

        xbrl = """<?xml version="1.0"?>
        <xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin">
          <xbrli:context id="c1"><xbrli:period><xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>
          <in-bse-fin:RevenueFromOperations contextRef="c1" unitRef="INR" decimals="-5">1500000000</in-bse-fin:RevenueFromOperations>
          <in-bse-fin:ProfitLossForPeriod contextRef="c1" unitRef="INR" decimals="-5">225000000</in-bse-fin:ProfitLossForPeriod>
          <in-bse-fin:DilutedEarningsLossPerShareFromContinuingOperations contextRef="c1" unitRef="INR" decimals="2">18.40</in-bse-fin:DilutedEarningsLossPerShareFromContinuingOperations>
        </xbrli:xbrl>"""
        parsed = facts_m.parse_indas_xbrl(xbrl)
        self.assertEqual(parsed["revenue_cr"], 150.0)
        self.assertEqual(parsed["net_profit_cr"], 22.5)
        self.assertEqual(parsed["eps_diluted"], 18.4)


if __name__ == "__main__":
    unittest.main()
