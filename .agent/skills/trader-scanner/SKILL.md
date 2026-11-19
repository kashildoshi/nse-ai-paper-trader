---
name: trader-scanner
description: >-
  NSE Chart Scanner. Scans liquid NSE equities for intraday and daily setups:
  opening-range breakouts (09:15-09:30 IST), bull flags, triangles, VWAP
  reclaims, and daily breakouts/RSI bounces, with volume-ratio and turnover
  filters. Builds the NSE universe from NSE index CSVs (NIFTY 200 default) with
  Yahoo (.NS), Upstox, or Angel One SmartAPI feeds, and writes
  trader/watchlist.json. Use when the user says "scan NSE", "scan Nifty",
  "find me setups", "what is breaking out", "opening range breakouts",
  "scan for bull flags", "VWAP reclaim", "triangle breakout", "build my
  watchlist", or at the start of the NSE trading day before /trader-news.
argument-hint: "[universe | daily | orb | flag | triangle | vwap | patterns] [--at HH:MM]"
---

# trader-scanner (NSE India)

The Chart Scanner reads NSE equity charts and lists the ones matching a rule.
A scan is a list of charts that match a rule, not a list of stocks to buy. Most
people who day trade lose money; scanning only saves flipping through charts by
hand.

```powershell
py .agent/skills/trader-scanner/scan.py universe --index nifty200     # -> trader/universe.txt
py .agent/skills/trader-scanner/scan.py daily                         # before 09:15 IST -> trader/watchlist.json
py .agent/skills/trader-scanner/scan.py patterns --at 10:30           # ORB, bull flag, triangle, VWAP reclaim
py .agent/skills/trader-scanner/scan.py orb --symbols RELIANCE,TCS    # opening-range breakouts only
py .agent/skills/trader-scanner/scan.py patterns --feed upstox        # use Upstox live feed (UPSTOX_ACCESS_TOKEN)
```

Flags:
- `--min-price` (default `₹50`), `--min-turnover-cr` (default `₹25 crore` daily turnover), `--min-vol-ratio` (default `1.5x` prior bars).
- `--or-minutes 15` (opening range `09:15–09:30 IST`), `--feed yahoo|upstox|angelone`.
- `--now "2026-11-18 10:30"` (IST), `--offline PATH`, `--no-write`, `--show N`, `--json`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | `SYMBOL\|Company Name\|ISIN`, written by `universe` |
| `trader/watchlist.json` | written by `daily` and intraday modes (`mode`, `as_of`, `market: "NSE"`, `hits[]` with `levels`, `volume_ratio`, `avg_turnover_cr`) |
| `trader/config.json` | `data_feed`, `min_price_inr`, `min_avg_turnover_cr`, `no_entries_after`, `safety_cutoff` (15:20 IST) |

## Setups detected

| setup | intraday 5-minute rule (IST, 09:15–15:30) |
| --- | --- |
| `orb` | First completed bar after 09:30 closing above the 09:15–09:30 opening-range high with volume >= 1.5x prior avg |
| `bull_flag` | Impulse pole >= 1.2% followed by tight 3–8 bar lower-volume pullback (<= 55% retrace) and high-volume close above flag high |
| `triangle` | 6–15 bars of higher lows + flat/descending highs with narrowing range, broken upward on >= 1.5x volume |
| `vwap_reclaim` | At least 2 consecutive closes below session VWAP followed by a >= 1.5x volume close back above VWAP |

Daily setups (`scan.py daily`): `breakout_20d`, `bull_flag`, `reclaim_ma20`, `oversold` (RSI-14 < 30), `oversold_bounce`, `triangle_daily`.

## Data and limits

- Yahoo Finance (`.NS`) is free, unofficial, and delayed. Upstox and Angel One require your own API token in environment variables (`UPSTOX_ACCESS_TOKEN`, or `ANGEL_API_KEY` + `ANGEL_JWT_TOKEN`).
- NSE holidays for 2026 (circular NSE/CMTR/71775) are built in; weekend and holiday scans fail plainly.
- Partial results are reported honestly as scanned/failed counts.

## Illustrative example

Illustrative (not live market data): on a 10:30 IST `patterns` scan of NIFTY 200 names, `RELIANCE` triggers `orb` at `₹2,462.50` (`+0.51%` above the 09:15–09:30 range high `₹2,450.00`, `1.9x` volume, turnover `₹1,840.2 cr`). The agent hands off to `/trader-news RELIANCE` and `/trader-entry RELIANCE` without picking for the user.

## Rules & Safety

- Say how many symbols were in the universe, how many came back, how many were filtered for liquidity, and the IST timestamp.
- Say "matched a rule", never "a buy" or "a winner". Never print P&L or promises.
- Paper trading by default (`"paper": true`). Only `/trader-broker` can prepare an order, after `/trader-risk` passes it and the user types an explicit `yes`.
