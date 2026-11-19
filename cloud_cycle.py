import os, sqlite3, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import nse_holidays, nse_scanner

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = "paper_trades.db"

def send_tg(msg):
    t, c = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if t and c:
        try:
            requests.post(f"https://api.telegram.org/bot{t}/sendMessage", json={"chat_id": c, "text": msg, "parse_mode": "HTML"}, timeout=6)
        except Exception:
            pass

def get_ltp(sym):
    try:
        s = sym.replace(".NS", "").strip().upper()
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{s}.NS?interval=5m&range=1d", headers={"User-Agent": "Mozilla/5.0"}, timeout=4)
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return 0.0

def run_cycle():
    now = datetime.now(IST)
    ok, reason = nse_holidays.is_nse_trading_day(now)
    if not ok or not ((9, 0) <= (now.hour, now.minute) <= (15, 30)):
        print("Skipped:", reason, now.strftime("%H:%M IST"))
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, pattern TEXT, qty INTEGER, signal_entry_price REAL, entry_price REAL, exit_price REAL, high_water REAL, initial_stop REAL, final_stop REAL, target_price REAL, exit_reason TEXT, gross_pnl REAL, charges REAL, net_pnl REAL, peak_capture_pct REAL, status TEXT, entry_time TEXT, exit_time TEXT, entry_reason TEXT, tech_params TEXT, news_catalyst TEXT, timemachine_prob REAL)")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
    for col, typ in [("side", "TEXT"), ("target_price", "REAL"), ("entry_reason", "TEXT"), ("tech_params", "TEXT"), ("timemachine_prob", "REAL")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
    cur = conn.cursor()
    open_rows = cur.execute("SELECT id, symbol, COALESCE(side, ?), qty, entry_price, high_water, final_stop, COALESCE(target_price, 0) FROM trades WHERE status=?", ("LONG", "OPEN")).fetchall()
    sq_off = (now.hour, now.minute) >= (15, 20)
    for tid, sym, side, qty, ep, hw, sl, tgt in open_rows:
        ltp = get_ltp(sym)
        if ltp <= 0:
            continue
        side = side.upper()
        tgt = tgt if tgt > 0 else round(ep * (1.015 if side == "LONG" else 0.985), 2)
        sl = sl if sl > 0 else round(ep * (0.992 if side == "LONG" else 1.008), 2)
        ex_reason = None
        if side == "LONG":
            nhw = max(hw or ep, ltp)
            if nhw >= ep * 1.005:
                sl = max(sl, round(nhw * 0.994, 2))
            cur.execute("UPDATE trades SET high_water=?, final_stop=? WHERE id=?", (nhw, sl, tid))
            if ltp >= tgt:
                ex_reason = "TARGET"
            elif ltp <= sl:
                ex_reason = "TRAILING_SL"
        else:
            nhw = min(hw or ep, ltp)
            if nhw <= ep * 0.995:
                sl = min(sl, round(nhw * 1.006, 2))
            cur.execute("UPDATE trades SET high_water=?, final_stop=? WHERE id=?", (nhw, sl, tid))
            if ltp <= tgt:
                ex_reason = "TARGET"
            elif ltp >= sl:
                ex_reason = "TRAILING_SL"
        if sq_off and not ex_reason:
            ex_reason = "EOD_1520_SQUAREOFF"
        if ex_reason:
            gross = round((ltp - ep) * qty if side == "LONG" else (ep - ltp) * qty, 2)
            charges = round((ep + ltp) * qty * 0.0007, 2)
            net = round(gross - charges, 2)
            cur.execute("UPDATE trades SET status=?, exit_price=?, exit_reason=?, gross_pnl=?, charges=?, net_pnl=?, exit_time=? WHERE id=?", ("CLOSED", ltp, ex_reason, gross, charges, net, now.isoformat(timespec="seconds"), tid))
            send_tg(f"<b>NSE {side} EXIT: {sym}</b>\nReason: <code>{ex_reason}</code> | Net P&L: <b>Rs {net:+,.2f}</b>")
    active_cnt = cur.execute("SELECT COUNT(*) FROM trades WHERE status=?", ("OPEN",)).fetchone()[0]
    active_syms = {r[0] for r in cur.execute("SELECT symbol FROM trades WHERE status=?", ("OPEN",)).fetchall()}
    if (9, 15) <= (now.hour, now.minute) <= (15, 5) and active_cnt < 3:
        for sig in nse_scanner.scan_full_nse_universe(max_symbols=250, min_prob=65.0):
            if active_cnt >= 3:
                break
            if sig["symbol"] in active_syms:
                continue
            qty = max(1, int(30000.0 // sig["entry_price"]))
            cur.execute("INSERT INTO trades (symbol, side, pattern, qty, signal_entry_price, entry_price, exit_price, high_water, initial_stop, final_stop, target_price, status, entry_time, entry_reason, tech_params, timemachine_prob) VALUES (?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?)", (sig["symbol"], sig["side"], sig["pattern"], qty, sig["entry_price"], sig["entry_price"], sig["entry_price"], sig["stop_loss"], sig["stop_loss"], sig["target_price"], "OPEN", now.isoformat(timespec="seconds"), sig["entry_reason"], sig["tech_params"], sig["timemachine_prob"]))
            active_cnt += 1
            active_syms.add(sig["symbol"])
            s_side, s_sym, s_tm, s_ep, s_tgt, s_sl = sig["side"], sig["symbol"], sig["timemachine_prob"], sig["entry_price"], sig["target_price"], sig["stop_loss"]
            d_txt = "BUY to SELL" if s_side == "LONG" else "SELL to BUY"
            send_tg(f"<b>NSE {s_side} ({d_txt}): {s_sym}</b>\nTM: <b>{s_tm}%</b> | Qty: {qty} @ Rs {s_ep:,.2f}\nTGT: Rs {s_tgt:,.2f} | SL: Rs {s_sl:,.2f}")
    conn.commit()
    conn.close()
    print("Cycle complete at", now.strftime("%H:%M:%S IST"))

if __name__ == "__main__":
    run_cycle()