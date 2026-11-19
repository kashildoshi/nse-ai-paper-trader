---
name: trader-broker
description: >-
  NSE Order Executor. Prepares Upstox (default) or Angel One SmartAPI MIS
  tickets in ₹ (INR), simulates fills in paper mode (paper_fills.json, enabled
  by default), and enforces the three-gate live safety rule: trader-risk PASS
  today (fingerprint match), your explicit yes, and MCP connector Needs
  approval / Allow once. Use when the user says "place the order", "paper fill",
  "show ticket", "flatten", or after /trader-risk passes.
argument-hint: "[show|approve|paper-fill|flatten|log]"
---

# trader-broker (NSE India)

Supports **Upstox** (`product: "I"`, `NSE_EQ|ISIN`, `SL-M` initial stop) and
**Angel One SmartAPI** (`producttype: "INTRADAY"`, `SYMBOL-EQ`). Paper mode
(`"paper": true`) is enabled by default.

```powershell
py .agent/skills/trader-broker/ticket.py show
py .agent/skills/trader-broker/ticket.py paper-fill --index 1
py .agent/skills/trader-broker/ticket.py flatten --symbol RELIANCE --qty 10
```

## Three gates for any real order

1. `trader-risk` passed this exact order today (SHA-256 fingerprint match).
2. You typed an explicit `yes` for that single order (`approve --index 1 --reply "yes"`).
3. Your Upstox / Angel One MCP order tool is set to **Needs approval** so you click **Allow once**.

## Trailing stop management

Upstox regular equity orders do not trail automatically on the exchange: keep
the initial `SL-M` order placed, and update its `trigger_price` upward whenever
`exit.py trail` raises `current_stop`.

## Safety

- Never places orders without all three gates.
- API keys, tokens, and TOTP secrets live in environment variables or local MCP config, never in chat.
- `"paper": true` stays on until you edit `trader/config.json` yourself.
