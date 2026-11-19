---
name: trader-pelosi
description: >-
  NSE Bulk/Block Deals & Promoter Insider Buying Tracker. Tracks public NSE
  bulk deals (>0.5% equity in a day), block deals (>= ₹10 crore window trades),
  and SEBI PIT Reg 7 promoter/insider buying disclosures using public NSE
  endpoints and archive RSS fallback. Cross-checks against trader/watchlist.json
  and trader/portfolio.json. Use when the user says "bulk deals", "block deals",
  "promoter buying", "insider trading NSE", "who bought big today", or "run the
  pelosi skill".
argument-hint: "[deals|insider|watch] [--promoters-only] [--buys-only] [--symbol SYMBOL]"
---

# trader-pelosi (NSE Bulk/Block Deals & Insider Tracker)

Reads public NSE large-deal and SEBI PIT (Prohibition of Insider Trading Reg 7)
disclosures. Under SEBI PIT, insiders report trades to the company within 2
trading days and companies disclose to NSE within 2 trading days; bulk deals
(`>0.5%` of listed equity) and block deals (window trades `>= ₹10 crore`) are
disclosed after market/same day.

```powershell
py .agent/skills/trader-pelosi/deals.py deals --days 7
py .agent/skills/trader-pelosi/deals.py insider --promoters-only --buys-only --min-value-cr 0.5
py .agent/skills/trader-pelosi/deals.py watch --write
```

(`ptr.py` is kept as an alias to `deals.py`.)

## Data sources & fallbacks

- `/api/snapshot-capital-market-largedeal` and `/api/historical/bulk-deals`, `/api/historical/block-deals`
- `/api/corporates-pit?index=equities` with fallback to NSE's archive RSS `Insider_Trading.xml`
- If `nseindia.com` refuses scripted requests (HTTP 401/403), the tool reports it in one line and falls back to archive RSS or `--offline FILE` saved from your browser.

## Rules & Safety

- Public disclosures only — never inside information, never a buy signal or return promise.
- Never attempts to defeat NSE bot checks.
- Never places orders.
