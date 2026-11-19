---
name: trader-news
description: >-
  NSE News & Corporate Announcements Monitor. Watches NSE corporate
  announcements (/api/corporate-announcements + archive RSS fallback), Indian
  financial RSS feeds (Moneycontrol, Economic Times, LiveMint, Business
  Standard), and Yahoo Finance (.NS) headlines, plus intraday price moves. Tags
  results, board meetings, dividends, order wins, bulk/block deals, pledges,
  and SEBI actions in IST. Use when the user says "news on RELIANCE", "NSE
  announcements", "why is TCS moving", "morning briefing", or after
  /trader-scanner finds setups.
argument-hint: "[--tickers RELIANCE TCS] [--hours 24]"
---

# trader-news (NSE India)

Checks official NSE corporate announcements and Indian financial RSS feeds for
stocks on your watchlist, plan, or portfolio, and prints unseen items in IST.

```powershell
py .agent/skills/trader-news/news.py
py .agent/skills/trader-news/news.py --tickers RELIANCE TCS INFY --hours 24
py .agent/skills/trader-news/news.py --no-nse --json
```

## Sources

- **NSE Corporate Announcements**: `/api/corporate-announcements?index=equities&symbol=SYM` with fallback to NSE archive RSS `Online_announcements.xml`.
- **Indian Financial RSS**: Moneycontrol, Economic Times Markets/Stocks, LiveMint, Business Standard (`N.INDIA_RSS`).
- **Yahoo Finance `.NS` RSS & Chart**: headline RSS and today's % move vs previous close.

## Rules & Safety

- Announcements/filings carry `!`; read the attachment before acting on a headline.
- Flags board meetings and quarterly results (gap risk).
- Never trades on headlines or predicts prices.
