# Day Trader skills — NSE (India) edition

11 agent skills for intraday trading on the **National Stock Exchange of India**,
converted from [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT). Plain standard-library Python 3.8+, no pip installs. **Paper trading is the default.**

> Not financial advice. Most people who day trade lose money. Nothing here can
> place an order on its own: only `trader-broker`, only after `trader-risk`
> passes that exact order, and only after you type yes.

## Layout

```
.agent/skills/trader-*/     the 11 skills (SKILL.md + script + nse_common.py copy)
shared/nse_common.py        master copy of the shared NSE layer (edit here only)
tools/sync_common.py        copies the master into every skill (--check to verify)
tests/                      offline unittest suite (no network)
NSE_SPEC.md                 the cross-skill contract (files, fields, rules)
day-trader-skill/           the untouched US original, for reference
```

Open this folder as your workspace so `.agent/skills` is picked up.

## What changed from the US version

| area | US original | NSE edition |
| --- | --- | --- |
| Universe | ~5,900 Nasdaq Trader symbols | NIFTY 200 by default (NSE index CSVs: nifty50/100/200/500, midcap150), built-in liquid fallback list |
| Data | Yahoo `TSLA` | Yahoo `RELIANCE.NS` (free, delayed) or your **Upstox** / **Angel One SmartAPI** feed (`data_feed`, token in env vars) |
| Clock | New York, 9:30–16:00, NYSE holidays | IST, 09:15–15:30, NSE 2026 holidays (NSE/CMTR/71775) |
| Setups | ORB, flags, oversold | ORB, bull flag, triangle, VWAP reclaim (+ daily setups) with volume-ratio and turnover filters |
| Exit | fixed stop/target, flat by 15:55 | initial pattern stop → **ratcheting trailing stop** (arms at +0.5 %, trails 0.6 % below high-water, only moves up) + **15:20 IST safety cutoff** |
| Prices | to the cent | to the NSE tick (₹0.01 / 0.05 / 0.10 / 0.50 / 1 / 5 by price band) |
| Money | USD, PDT guard | ₹ INR, no PDT; sizing and risk include STT, exchange, SEBI, stamp duty, brokerage, GST |
| News | SEC EDGAR 8-K + Yahoo | NSE corporate announcements + Moneycontrol / ET Markets / LiveMint / Business Standard RSS |
| Analyst | SEC XBRL facts | NSE quarterly financial results |
| "Pelosi" | Congress PTR filings | NSE bulk / block deals + SEBI PIT insider & promoter buying |
| Broker | Alpaca MCP | Upstox (default) / Angel One, paper fills by default |

## The flow (one trade)

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker
alerts / exit.py trail : ratchet the stop while the trade is open, square off by 15:20 IST
news / analyst / pelosi : on a schedule
```

```powershell
py .agent/skills/trader-scanner/scan.py universe            # NIFTY 200 -> trader/universe.txt
py .agent/skills/trader-scanner/scan.py patterns            # ORB / flag / triangle / VWAP reclaim
py .agent/skills/trader-news/news.py --tickers RELIANCE
py .agent/skills/trader-entry/entry.py check RELIANCE --setup orb
py .agent/skills/trader-exit/exit.py plan --write           # pattern stop + trailing block -> trader/plan.json
py .agent/skills/trader-timemachine/timemachine.py run --write
py .agent/skills/trader-budget/size.py --write              # INR sizing incl. charges -> trader/orders.json
py .agent/skills/trader-risk/check.py                       # PASS / BLOCK
py .agent/skills/trader-broker/ticket.py show               # paper by default
py .agent/skills/trader-exit/exit.py trail                  # while open: "move your SL to ₹X"
py .agent/skills/trader-pelosi/ptr.py insider --promoters-only --buys-only
```

(Exact flags per skill are in each `SKILL.md`.)

## Config

Copy `.agent/skills/trader-budget/config.example.json` to `trader/config.json` and edit.
`"paper": true` stays on until you change it yourself. Broker / feed credentials are
**environment variables only** (`UPSTOX_ACCESS_TOKEN`, `ANGEL_API_KEY`, `ANGEL_JWT_TOKEN`),
never in the chat or config.

## Fine print (India)

- **Charges.** Intraday equity: STT 0.025 % on the sell side, NSE transaction charge
  ~0.00297 % both sides, SEBI fee ₹10/crore, stamp duty 0.003 % on the buy side, 18 % GST
  on brokerage + exchange + SEBI fees, brokerage per your broker (default ₹20 or 0.03 %,
  whichever is lower, per order). Override in `config.json` → `charges`.
- **Tax.** Intraday equity profit is speculative business income, taxed at your slab rate.
  Not tax advice.
- **Square-off.** Brokers auto-square-off MIS positions around 15:20–15:25 IST (often with
  a fee). The 15:20 safety cutoff gets you out first.
- **Circuits.** A stock locked at its upper/lower circuit may not fill your stop.
- **NSE website data** (announcements, deals, insider filings, results) is public but the
  site may refuse scripted requests. The tools report that plainly and fall back to NSE's
  archive RSS feeds or `--offline` files; they never try to get around a block.
- **Holidays.** The built-in calendar covers 2026; add later years to `extra_holidays`.

## Tests

```powershell
py -m unittest discover -s tests -v
py tools/sync_common.py --check
```

MIT. Original by [Jake Schincariol](https://opusjake.ai); NSE conversion in this workspace.
