# NSE conversion spec (shared contract for all 11 trader-* skills)

Workspace root: `C:\Users\VISHEK\.gemini\antigravity\scratch\day-trader-workspace`
Skills live in `.agent/skills/trader-*/`. The untouched US original is in `day-trader-skill/` (reference only; never edit it).
Python on this machine: `py` (3.11). Scripts must stay **standard library only**, Python 3.8+.

## 1. The shared layer — `nse_common.py`
- Master: `shared/nse_common.py`. A byte-identical copy sits in every skill folder (`py tools/sync_common.py`).
- **Never edit a copy or the master yourself.** If you need a change/addition, message the coordinator with the exact diff. Put skill-specific helpers in your own script.
- Import it like this at the top of every script (works when a skill folder is copied alone):
  ```python
  sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
  import nse_common as N
  ```
- Use it for: IST clock (`N.now_ist`, `N.parse_now`, `N.IST`), session/holidays (`N.is_trading_day`, `N.session_bounds`, `N.closed_reason`, `N.market_state`, `N.calendar_note`), config (`N.load_config` → defaults + `trader/config.json`), symbols (`N.norm_symbol`, `N.yahoo_symbol`, `N.parse_symbols`, `N.LIQUID_UNIVERSE`, `N.NSE_INDEX_CSV`, `N.parse_index_csv`), ticks (`N.tick_size`, `N.round_tick`), money (`N.inr`, `N.crore`), bars (`N.get_intraday_bars`, `N.bars_from_yahoo`, `N.bars_from_upstox`, `N.bars_from_angel`, `N.fetch_yahoo_chart`, `N.get_ltp`), NSE site (`N.NSESession().get_json`, `N.NSE_RSS`, `N.INDIA_RSS`, `N.fetch_rss`, `N.parse_rss`, `N.parse_nse_date`, `N.nse_date`), charges (`N.intraday_charges`, `N.breakeven_price`, `N.TAX_NOTE`), trailing stop (`N.trail_init`, `N.trail_update`, `N.trail_stop_for`, `N.describe_trail`), I/O (`N.write_json`, `N.read_json`). Errors: raise/catch `N.NSEError` and print its message as one plain line.
- Bar format everywhere: `{"t": aware IST datetime of bar START, "o","h","l","c": float, "v": int}`.

## 2. Market facts (use exactly these)
| item | value |
| --- | --- |
| Timezone | IST, UTC+05:30, no DST. Print times as `10:15 IST` (24h). |
| Session | 09:15–15:30 (pre-open 09:00–09:08). Holidays: `N.NSE_HOLIDAYS` (2026) + config `extra_holidays`. No early closes. |
| Opening range | 09:15–09:30 (first 15 min = three 5-minute bars 09:15, 09:20, 09:25). Earliest ORB hit at 09:35 (close of 09:30 bar). |
| Entry window | `no_entries_before` 09:30, `no_entries_after` 15:00 |
| Safety cutoff | `safety_cutoff` / `flat_by` = **15:20 IST**: everything flat, no exceptions (brokers auto-square-off MIS ~15:20–15:25). |
| Tick size | `N.tick_size(price)` (₹0.01 <250, 0.05 <1000, 0.10 <5000, 0.50 <10000, 1.00 <20000, 5.00 above). Stops round DOWN (away from price for a long), targets round to nearest tick. Never "to the cent". |
| Symbols | Plain NSE symbol in all files (`RELIANCE`, `M&M`, `BAJAJ-AUTO`). `.NS` appended only when calling Yahoo. Accept `RELIANCE.NS` / `NSE:RELIANCE` as input. |
| Currency | INR. Print with `N.inr()` (Indian grouping `₹1,23,456.78`). Large turnover with `N.crore()`. |
| Price bands | NSE circuit limits (2/5/10/20%). A stock at its upper/lower circuit cannot be exited reliably — flag it where you see it. |
| Short selling | Long only, as in the original (`allow_short` false). |
| No PDT | Remove every FINRA / pattern-day-trader / `pdt_guard` reference. |

## 3. Config (`trader/config.json`) — canonical keys
See `N.CONFIG_DEFAULTS`. Money keys are `*_inr` (old `*_usd` keys are read as INR with a warning by `N.load_config`). Key additions:
`market:"NSE"`, `currency:"INR"`, `product:"MIS"` (intraday) , `broker:"upstox"|"angelone"`, `data_feed:"yahoo"|"upstox"|"angelone"`, `paper:true` (**default, must stay true in every example file**), `trail_activate_pct:0.5`, `trail_pct:0.6`, `safety_cutoff:"15:20"`, `min_avg_turnover_cr:25` (avg daily traded value in ₹ crore), `min_price_inr:50`, `charges:{...}` overrides of `N.CHARGES_DEFAULT`, `tax_slab_pct:null`, `contact_email` (replaces `sec_user_agent`).
Data-feed credentials only from env vars: `UPSTOX_ACCESS_TOKEN`; `ANGEL_API_KEY` + `ANGEL_JWT_TOKEN`. Never read them from config, never print them.

## 4. File contracts in `trader/` (keep names; field changes below are mandatory)
### `trader/watchlist.json` (scanner → news, entry)
`mode`, `as_of` (ISO IST), `market:"NSE"`, `universe`, `scanned`, `failed`, `hits[]`. Each hit: `symbol`, `setup` (one of the ids below), `label`, `price`, `time` (IST), `volume_ratio`, `avg_turnover_cr`, plus setup-specific `levels` (e.g. `or_high`, `or_low`, `flag_high`, `triangle_upper`, `vwap`).

### Setup ids (scanner detects, entry triggers, exit/timemachine understand)
`orb` (opening-range breakout), `bull_flag`, `triangle` (ascending/symmetrical breakout of the upper trendline), `vwap_reclaim`, plus daily ones `breakout_20d`, `reclaim_ma20`, `oversold`, `oversold_bounce`. Every intraday trigger requires a **volume filter** (trigger bar volume ≥ `--min-vol-ratio`, default 1.5× the average volume of the same bars/prior bars) and a **liquidity filter** (avg daily turnover ≥ `min_avg_turnover_cr`, price ≥ `min_price_inr`).

### `trader/entry.json` (entry → exit)
As original (`symbol, status, setup, entry, time, bar_index, interval, date, opening_range{high,low,minutes}`) plus `levels{}` for the pattern (the level that broke and the pattern's invalidation low, `pattern_low`), `volume_ratio`, `vwap`.

### `trader/plan.json` (exit → timemachine, budget, alerts, broker)
Original fields stay (`symbol, date, setup, entry, entry_time, stop, stop_rule, stop_label, target, target_r, target_label, risk_per_share, reward_risk, flat_by`) with these changes/additions:
```json
"exit_mode": "trailing",
"trailing": {"activate_pct": 0.5, "trail_pct": 0.6, "activate_at": 1005.0,
             "high_water": 1000.0, "current_stop": 990.0, "active": false, "updated_at": null},
"flat_by": "15:20",
"safety_cutoff": "15:20",
"target": 1020.0,            // optional take-profit; null when --target none
"charges_estimate": {...},   // N.intraday_charges for 1 share at entry->stop and entry->target (per-share view)
"breakeven_move": 0.6        // per share, ₹, from N.breakeven_price for the planned qty if known
```
`stop` stays the INITIAL hard stop (pattern invalidation). `trailing.current_stop` is the live ratcheted stop; it is updated by `exit.py trail` (and by alerts) and **never moves down**. `stop`/`current_stop` round DOWN to tick. Default target: `2R` reference take-profit, `--target none` allowed (then reward_risk is computed against the 2R reference and labelled as such, so trader-risk still has a number).

### Trailing-stop rule (identical everywhere — use `N.trail_update`)
Initial stop from the pattern. Once price reaches entry × (1 + 0.5%) the stop trails **0.6% below the high-water mark** (highest high since entry), ratchets up only. Within a bar: test the low against the stop in force first, then raise high water (conservative). Exit = stop price. At **15:20 IST** anything still open exits at that bar's close ("safety cutoff"). There is no other end-of-day exit.

### `trader/orders.json` (budget → risk → broker)
`paper:true` default. Per order: `symbol`, `exchange:"NSE"`, `product:"MIS"`, `side:"buy"`, `qty` (whole shares), `entry`, `stop`, `target` (nullable), `trailing{activate_pct, trail_pct}`, `order_type`, `cost_inr`, `max_loss_inr` (**includes round-trip charges at the stop**), `charges_at_stop`, `charges_at_target`, `breakeven_price`, `flat_by:"15:20"`, `max_loss_per_trade_inr`. Rename every `*_usd` field to `*_inr`.

## 5. Brokers (trader-broker)
Replace Alpaca with **Upstox** (default) and **Angel One SmartAPI**. Paper mode default: never sends anything; writes a simulated fill log `trader/paper_fills.json`. Live orders still only via the user's own MCP connector set to "Needs approval" + explicit yes, exactly as the original's three-gate rule.

## 6. Writing style & SKILL.md
- Keep each SKILL.md's structure, safety sections and tone. Update frontmatter `description` (trigger phrases incl. Indian ones: "NSE", "Nifty", "intraday", "MIS"), commands, examples (use realistic NSE examples, e.g. RELIANCE / TATAMOTORS / HDFCBANK, clearly marked as illustrative when not reproduced from real data — **never invent "real data" numbers**).
- Replace `${CLAUDE_SKILL_DIR}` with `.agent/skills/<skill-name>` and `python3` with `python` (note: on Windows `py`). Replace "Claude" with "the agent" where it refers to the assistant; keep the safety rule about the connector's "Needs approval"/"Allow once".
- Times: IST, 24-hour. Money: ₹. Never print P&L/returns where the original refused to.

## 7. Tests
- New offline tests go in `tests/test_<skill>.py` at the workspace root (synthetic fixtures under `tests/fixtures/<skill>/`, small). Load scripts with:
  ```python
  import importlib.util, os
  ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  def load(skill, name):
      p = os.path.join(ROOT, ".agent", "skills", skill, name + ".py")
      spec = importlib.util.spec_from_file_location("t_" + name, p); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
  ```
- Run: `py -m unittest discover -s tests -v` from the workspace root. No network in tests.
- Every script must also pass `py <script> --help`.
