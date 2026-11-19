from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# Official NSE Trading Holidays (YYYY-MM-DD)
NSE_HOLIDAYS = {
    # 2026
    "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
    "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14",
    "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
    # 2027
    "2027-01-26", "2027-03-08", "2027-03-22", "2027-03-26", "2027-04-14",
    "2027-04-15", "2027-05-01", "2027-08-15", "2027-10-02", "2027-10-29",
    "2027-11-12", "2027-12-25"
}

def is_nse_trading_day(dt: datetime | None = None) -> tuple[bool, str]:
    now = dt.astimezone(IST) if dt else datetime.now(IST)
    if now.weekday() >= 5:
        return False, f"Weekend ({now.strftime('%A')})"
    date_str = now.strftime("%Y-%m-%d")
    if date_str in NSE_HOLIDAYS:
        return False, f"NSE Market Holiday ({date_str})"
    return True, "Open Trading Day"

def is_nse_market_hours(dt: datetime | None = None) -> bool:
    now = dt.astimezone(IST) if dt else datetime.now(IST)
    ok, _ = is_nse_trading_day(now)
    if not ok:
        return False
    return (9, 0) <= (now.hour, now.minute) <= (15, 30)

if __name__ == "__main__":
    ok, msg = is_nse_trading_day()
    print(f"NSE Status Today: {ok} -> {msg} | Active Window (9:00-15:30 IST): {is_nse_market_hours()}")