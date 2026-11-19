"""Offline tests for trader-news (NSE corporate announcements & Indian RSS)."""
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


news_m = load_mod("trader-news", "news")


class TestNewsNSE(unittest.TestCase):
    def test_tags_aliases_and_offline_fetch(self):
        tags = news_m.tag_text("Outcome of Board Meeting: approved Q2 financial results and interim dividend")
        self.assertIn("board_meeting", tags)
        self.assertIn("results", tags)
        self.assertIn("dividend", tags)

        aliases = news_m.load_aliases(None)
        matched = news_m.match_tickers("Tata Consultancy announces major order win", ["TCS", "RELIANCE"], aliases)
        self.assertEqual(matched, ["TCS"])

        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "nse-ann-RELIANCE.json"), "w") as f:
                json.dump([{"desc": "Board Meeting Intimation", "attchmntText": "Consider Q2 financial results",
                            "an_dt": "18-Nov-2026 09:05:00", "attchmntFile": "https://nsearchives.nseindia.com/x.pdf"}], f)
            with open(os.path.join(td, "rss-mc.xml"), "w") as f:
                f.write("<rss><channel><title>Moneycontrol</title><item><title>Reliance Industries bags order win</title>"
                        "<pubDate>Wed, 18 Nov 2026 10:00:00 +0530</pubDate><link>https://mc/1</link>"
                        "<description>Major contract</description></item></channel></rss>")
            a_items, _ = news_m.fetch_nse_announcements(["RELIANCE"], aliases, offline=td)
            n_items, _ = news_m.fetch_india_news(["RELIANCE"], aliases, offline=td)
            self.assertEqual(len(a_items), 1)
            self.assertIn("board_meeting", a_items[0]["tags"])
            self.assertEqual(len(n_items), 1)
            self.assertIn("order_win", n_items[0]["tags"])


if __name__ == "__main__":
    unittest.main()
