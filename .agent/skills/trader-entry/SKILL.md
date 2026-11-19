---
name: trader-entry
description: >-
  NSE Entry Timer. Watches an NSE setup bar by bar and triggers on the first
  completed 5-minute or 1-minute bar closing above the pattern level (ORB
  09:15-09:30 IST, bull flag, triangle, VWAP reclaim, or price level) with
  volume >= 1.5x average prior volume. Writes trader/entry.json for /trader-exit.
  Long side only. Use when the user says "tell me when to get in", "watch
  RELIANCE", "has it broken out", "did it break the opening range", "entry
  signal", "time my entry", "check my watchlist", or after /trader-scanner and
  /trader-news, before /trader-exit.
argument-hint: "[check|watch|watchlist] [SYMBOL] [--setup orb|bull_flag|triangle|vwap_reclaim|level]"
---

# trader-entry (NSE India)

The Entry Timer turns a chart setup into one confirmed trigger: the first
completed bar that closes strictly above the pattern level on qualifying volume.
It never places an order.

```powershell
py .agent/skills/trader-entry/entry.py check RELIANCE --write
py .agent/skills/trader-entry/entry.py check TCS --setup bull_flag --min-volume-x 1.5 --write
py .agent/skills/trader-entry/entry.py check HDFCBANK --setup triangle --write
py .agent/skills/trader-entry/entry.py check INFY --setup vwap_reclaim --write
py .agent/skills/trader-entry/entry.py watch RELIANCE --until 15:00
py .agent/skills/trader-entry/entry.py watchlist --write
```

Flags: `--interval 5m|1m`, `--or-minutes 15` (09:15–09:30 IST), `--min-volume-x 1.5`,
`--feed yahoo|upstox|angelone`, `--now "2026-11-18 10:15"`, `--offline PATH`, `--json`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | scanner hits and pattern levels |
| `trader/config.json` | `no_entries_before` (09:30), `no_entries_after` (15:00), `safety_cutoff` (15:20), `data_feed` |
| `trader/entry.json` | trigger symbol, setup, entry price (NSE tick), IST time, `opening_range`, `levels` (incl. `pattern_low`), `volume_ratio`, `vwap` |

## Rules & Safety

- Only completed bars inside `09:30–15:00 IST` count; past `15:00` or `15:20 IST` any waiting setup expires.
- Volume filter defaults to `1.5x` average volume of prior bars.
- Long side only. A trigger is information, not a buy recommendation or an order.
- Paper trading by default (`"paper": true`). Only `/trader-broker` prepares orders after `/trader-risk` passes and you type `yes`.
