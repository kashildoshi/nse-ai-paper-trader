---
name: trader-analyst
description: >-
  NSE Earnings & Results Analyst. Reads quarterly financial results from NSE
  corporate filings (/api/corporates-financial-results and Ind-AS XBRL) in ₹
  crore, computes YoY and QoQ growth, net margins, EPS, and TTM totals, with
  consolidated vs standalone flags and offline/CSV support. Use when the user
  says "analyze quarterly results", "how were RELIANCE earnings", "YoY profit
  growth", "check Ind-AS XBRL", or before trading around Indian earnings.
argument-hint: "[RELIANCE TCS ...] [--quarters 8] [--metrics trader/metrics.json]"
---

# trader-analyst (NSE India)

Reads NSE quarterly financial results in `₹ crore` (consolidated preferred
when both consolidated and standalone are filed), computes YoY/QoQ growth,
margins, and trailing twelve months (TTM).

```powershell
py .agent/skills/trader-analyst/facts.py RELIANCE TCS --quarters 8 --metrics trader/metrics.json
py .agent/skills/trader-analyst/facts.py RELIANCE --offline path\to\dir --json
```

## Sources & fallback

- Public NSE endpoint `/api/corporates-financial-results?index=equities&symbol=SYM&period=Quarterly`
- Ind-AS XBRL XML parser (`parse_indas_xbrl`) and manual CSV fallback (`--csv FILE`) when NSE blocks scripted requests.

## Rules & Safety

- Reports filed numbers only; never says a stock is cheap, expensive, or a buy.
- Never attempts to defeat NSE anti-bot protections.
