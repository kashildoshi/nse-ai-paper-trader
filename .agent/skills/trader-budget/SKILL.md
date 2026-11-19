---
name: trader-budget
description: >-
  NSE Budget Manager. Sizes intraday NSE MIS positions in ₹ (INR) to your
  budget and max loss per trade, factoring in Indian intraday charges
  (brokerage, STT 0.025% sell, NSE transaction charges, SEBI fee, stamp duty
  0.003% buy, 18% GST). Whole shares only, paper trading enabled by default.
  Use when the user says "how many shares", "size this trade", "budget in
  rupees", "calculate intraday charges", or after /trader-exit writes
  trader/plan.json.
argument-hint: "[--budget INR] [--write]"
---

# trader-budget (NSE India)

Sizes every trade in `₹` (INR) so that your loss at the initial stop **including
all Indian intraday taxes and brokerage** fits inside `max_loss_per_trade_inr`.
Whole NSE equity shares only. Writes proposals to `trader/orders.json` with
`"paper": true` by default.

```powershell
py .agent/skills/trader-budget/size.py --write
py .agent/skills/trader-budget/size.py --budget 100000 --limit 2462.50 --write
```

## Indian intraday equity charges factored in

- **Brokerage**: `₹20` per executed order or `0.03%`, whichever is lower (buy + sell).
- **STT**: `0.025%` on sell turnover (rounded to nearest rupee).
- **NSE Transaction Charge**: `0.00297%` on both sides.
- **Stamp Duty**: `0.003%` on buy turnover.
- **SEBI Turnover Fee**: `0.0001%` (`₹10/crore`) on both sides.
- **GST**: `18%` on brokerage + exchange charges + SEBI fee.

Tax note: Intraday equity profit is speculative business income in India, taxed
at your income-tax slab rate (not as STCG). Nothing here is tax advice.

## Rules & Safety

- `"paper": true` by default in `config.example.json`.
- No US PDT rules apply on NSE.
- Never places orders; passes `trader/orders.json` to `/trader-risk`.
