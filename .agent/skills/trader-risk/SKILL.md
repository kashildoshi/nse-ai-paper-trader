---
name: trader-risk
description: >-
  NSE Risk Manager. Checks proposed NSE MIS orders in ₹ (INR) against your
  limits before reaching the broker: max loss per trade (incl. Indian intraday
  charges), daily loss limit, reward:risk net of STT/brokerage/GST, NSE tick
  validity, 09:30-15:00 IST entry window, 15:20 IST safety cutoff, NSE 2026
  holidays, and optional circuit-limit protection. No US PDT rules. Prints
  PASS or BLOCK and records order fingerprints in trader/state/risk.json.
argument-hint: "[--orders trader/orders.json] [--no-write]"
---

# trader-risk (NSE India)

The Risk Manager is the mandatory gate between `trader-budget` and
`trader-broker`. No order is ready for approval without a same-day `PASS`
matching its exact SHA-256 fingerprint.

```powershell
py .agent/skills/trader-risk/check.py
py .agent/skills/trader-risk/check.py --now "2026-11-18 10:15" --json
```

## Checks enforced

- **Format & Product**: valid NSE symbol, `exchange == "NSE"`, `product == "MIS"`, whole integer shares.
- **Tick Size**: `entry`, `stop`, and `target` must be exact NSE price-band ticks (`N.tick_size`).
- **Loss & Daily Loss**: `qty * (entry - stop) + intraday_charges <= max_loss_per_trade_inr` and cumulative daily risk `<= max_daily_loss_inr`.
- **Net Reward:Risk**: net of charges `>= min_reward_risk` (uses 2R reference if `target` is null).
- **Window & Holidays**: valid NSE trading day (`N.NSE_HOLIDAYS`), inside `no_entries_before` (`09:30`) to `no_entries_after` (`15:00`), and strictly before `15:20 IST` safety cutoff.
- **No US PDT**: US Pattern Day Trader rules are removed (a `pdt_guard` key in old configs is ignored with a note).
- **Circuit Guard**: blocks entries within 0.5% of upper/lower circuit when circuit data is in `trader/prices.json`.

## Rules & Safety

- An exit (`sell` reducing an open holding) is never blocked by daily/window limits.
- `"paper": true` by default; warns loudly if `"paper": false`.
- Never loosens limits or edits an order to make it pass.
