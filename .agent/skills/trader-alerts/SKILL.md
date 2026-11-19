---
name: trader-alerts
description: >-
  NSE Price Alerts & IST Clock. Watches prices, ratcheting trailing stops
  (+0.5% arm, 0.6% trail), optional targets, 15:20 IST MIS safety cutoff, and
  the 09:15 IST NSE opening bell. Uses NSE 2026 holiday calendar, Yahoo (.NS),
  Upstox, or Angel One feeds, and writes trader/prices.json. Use when the user
  says "alert me when", "watch my trailing stop", "is NSE open", "square off
  reminder", or after /trader-exit writes trader/plan.json.
argument-hint: "[clock|quote|add|check|list|reset]"
---

# trader-alerts (NSE India)

Monitors NSE IST market hours (`09:15–15:30 IST`), fetches quotes (`.NS` /
Upstox / Angel One), ratchets `trail` rules with `N.trail_update`, and rings
once per trigger or at the `15:20 IST` safety cutoff. Never places orders.

```powershell
py .agent/skills/trader-alerts/alerts.py clock
py .agent/skills/trader-alerts/alerts.py add --from-plan
py .agent/skills/trader-alerts/alerts.py check
py .agent/skills/trader-alerts/alerts.py quote RELIANCE TCS --write
```

## Rules & Safety

- Quotes from Yahoo `.NS` are delayed and unofficial; check your broker terminal when precision matters.
- Paper trading by default (`"paper": true`). Only `/trader-broker` prepares orders after `/trader-risk` passes and you type `yes`.
