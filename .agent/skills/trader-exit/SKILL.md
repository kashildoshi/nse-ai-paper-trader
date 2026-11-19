---
name: trader-exit
description: >-
  NSE Exit Planner. Sets the initial pattern stop, optional target, ratcheting
  trailing stop-loss (activates at +0.5% profit, trails 0.6% below peak
  high-water, moves up only), and 15:20 IST safety cutoff before entry. Rounds
  stops DOWN to NSE ticks. Writes trader/plan.json for Time Machine, Budget
  Manager, and Price Alerts. Long side only. Use when the user says "where's my
  stop", "set my stop and target", "update trailing stop", "plan the exit", or
  right after /trader-entry triggers.
argument-hint: "[plan|trail|show] [--stop or-mid|or-low|pattern-low|price:X|pct:X|atr:K] [--target 2R|none]"
---

# trader-exit (NSE India)

The Exit Planner decides how an NSE intraday trade ends before it starts:

1. **Initial Hard Stop**: from the pattern (`or-mid`, `or-low`, `pattern-low`, `price:X`, `pct:X`, `atr:K`), rounded **down** to the NSE tick (`₹0.01` under `₹250`, `₹0.05` under `₹1,000`, `₹0.10` under `₹5,000`, etc.).
2. **Ratcheting Trailing Stop-Loss**: once the trade reaches **+0.5%** above entry, the stop trails **0.6% below the highest price since entry (`high_water`)** and **only ever moves up**.
3. **15:20 IST Safety Cutoff**: square off anything still open before broker MIS auto-square-off (~15:20–15:25 IST). No overnight holds.

```powershell
py .agent/skills/trader-exit/exit.py plan --write
py .agent/skills/trader-exit/exit.py plan --symbol RELIANCE --entry 2462.50 --setup bull_flag --pattern-low 2445.00 --target 2R --write
py .agent/skills/trader-exit/exit.py trail --high 2485.00
py .agent/skills/trader-exit/exit.py show
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | triggered setup, entry price, `levels.pattern_low`, `opening_range` |
| `trader/config.json` | `trail_activate_pct` (0.5), `trail_pct` (0.6), `safety_cutoff` (15:20) |
| `trader/plan.json` | `exit_mode: "trailing"`, `trailing{activate_pct, trail_pct, activate_at, high_water, current_stop, active}`, `charges_estimate`, `breakeven_move`, `safety_cutoff: "15:20"` |

## Rules & Safety

- Refuses stops at/above entry, risks smaller than `max(0.05%, 2 ticks)`, or entries at/after `15:20 IST`.
- Never lowers a stop once raised.
- A stop is a plan, not a guarantee: gaps or NSE circuit limits can fill past it.
- Long side only. Paper trading by default (`"paper": true`).
