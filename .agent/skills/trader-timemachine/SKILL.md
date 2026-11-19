---
name: trader-timemachine
description: >-
  NSE Time Machine. Replays an NSE trade plan 10,000 times from the stock's own
  5-minute bars (09:15-15:30 IST) using the ratcheting trailing stop (+0.5%
  activation, 0.6% trail below high_water) and 15:20 IST safety cutoff, plus
  historical setup replay. Reports win odds net of Indian intraday charges,
  gross/net R, and outcome breakdown (target, trailed, stop, cutoff). Use when
  the user says "run the time machine", "what are the odds", "simulate this
  trade", "monte carlo", or after /trader-exit writes trader/plan.json.
argument-hint: "[run|chart] [--replays 10000] [--seed 19]"
---

# trader-timemachine (NSE India)

Before risking a rupee, the Time Machine bootstraps the rest of the NSE session
(`09:15–15:20 IST`) 10,000 times from the stock's own past 5-minute sessions and
applies the exact same trailing stop-loss and `15:20 IST` safety cutoff.

```powershell
py .agent/skills/trader-timemachine/timemachine.py run --write
py .agent/skills/trader-timemachine/timemachine.py run --replays 10000 --seed 19 --json
py .agent/skills/trader-timemachine/timemachine.py chart
```

## Outcomes & Metrics

- `target`: optional take-profit hit before stop/cutoff.
- `trailed`: stopped out by ratcheted trailing stop after reaching `+0.5%`.
- `stop`: stopped out at initial pattern stop before activation.
- `cutoff`: closed at `15:20 IST` safety cutoff.
- `win_prob_pct`: percentage of replays net profitable **after** Indian intraday charges (STT, NSE txn, SEBI, stamp duty, brokerage, GST).

## Rules & Safety

- Always report the seed, replay count, and both gross and net average R.
- A simulation built from past bars is a reality check, not a prediction. News, earnings, and NSE circuit limits can cause outcomes not seen in the sample.
- Never places an order or picks stocks.
