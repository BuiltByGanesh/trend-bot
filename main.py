"""
Intraday 5-min opening-range breakout bot.

  python main.py backtest          # 1y of 5-min data (Angel) -> report, Telegram, Google Sheet
  python main.py live              # paper or live trading for today (run before 09:15 IST)
  python main.py test-telegram     # send a test alert
  python main.py test-sheet        # write a test row to the Google Sheet
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta

import yaml

sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("main")
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from bot.notify import sheet, telegram  # noqa: E402


def angel_login():
    from bot.broker import AngelOneBroker
    try:
        return AngelOneBroker()
    except Exception as e:
        log.warning("Angel login failed: %s", e)
        return None


def do_backtest(cfg):
    from bot.backtest import full_report
    from bot.data import AngelData, history
    b = cfg["backtest"]
    ab = angel_login() if cfg["data_source"] == "angel" else None
    angel = AngelData(ab.smart, ab.token) if ab else None
    src = "angel" if angel else "yahoo"
    days = b["days"] if angel else 59
    data = history(cfg["watchlist"], days, src, angel)
    proxy = history([cfg["market_proxy"]], days, src, angel).get(cfg["market_proxy"])
    if proxy is None or not data:
        telegram("❌ Backtest: no data", cfg["telegram"]["enabled"])
        sys.exit(1)
    out, trades, daily, by_sym, reasons, cut = full_report(data, proxy, cfg)
    os.makedirs("reports", exist_ok=True)
    trades.to_csv("reports/backtest_trades.csv", index=False)
    daily.to_csv("reports/backtest_daily.csv", index=False)
    span = f"{daily.date.min()} → {daily.date.max()} ({len(daily)} days, {src})" if len(daily) else "no days"

    cols = ["trades", "net_pnl", "return_pct", "win_rate", "profit_factor", "avg_r", "max_dd_pct", "costs", "halted_days"]
    md = [f"# Backtest – 5-min ORB\n\n{span}. Out-of-sample starts {cut}. Capital ₹{cfg['risk']['capital']:,}.\n",
          "| Section | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for k, v in out.items():
        md.append(f"| {k} | " + " | ".join(str(v.get(c, "")) for c in cols) + " |")
    md.append(f"\nExit reasons: {reasons}\n")
    if len(by_sym):
        md.append(by_sym.to_markdown(index=False))
    open("reports/backtest_report.md", "w").write("\n".join(md))
    print("\n".join(md))

    def line(k):
        v = out.get(k, {})
        return f"<b>{k}</b>: {v.get('trades', 0)} tr | ₹{v.get('net_pnl', 0):,.0f} ({v.get('return_pct', 0)}%) | " \
               f"WR {v.get('win_rate', 0)}% | PF {v.get('profit_factor', 0)} | DD {v.get('max_dd_pct', 0)}%"
    telegram("🧪 <b>Intraday ORB backtest</b>\n" + span + "\n" + "\n".join(line(k) for k in out), cfg["telegram"]["enabled"])

    run_id = datetime.now().strftime("%Y-%m-%d %H:%M")
    on = cfg["sheet"]["enabled"]
    sheet("Backtest", [[run_id, k, span] + [v.get(c, "") for c in cols] for k, v in out.items()], on)
    tr = trades.tail(2000)
    sheet("Backtest_Trades", [[run_id] + [x if not hasattr(x, "item") else x.item() for x in r]
                              for r in tr.itertuples(index=False)], on)


def do_live(cfg, until=None):
    from bot.broker import PaperBroker
    from bot.data import AngelData, yahoo
    from bot.live import Trader, now_ist
    now = now_ist()
    if now.weekday() >= 5:
        print("Weekend – market closed.")
        return
    ab = angel_login()
    if cfg["mode"] == "live":
        if os.getenv("CONFIRM_LIVE") != "YES" or not ab:
            sys.exit("Live mode needs Angel login and env CONFIRM_LIVE=YES.")
        broker = ab
    else:
        if ab:
            price = lambda s: ab.ltp(s)
        else:
            price = lambda s: float(yahoo(s, 1).close.iloc[-1])
        broker = PaperBroker(price, cfg["costs"]["slippage_pct"])
        if cfg["mode"] == "alerts":
            broker.name = "alerts"
    angel = AngelData(ab.smart, ab.token) if ab else None

    def feed(sym):
        end = now_ist()
        if angel:
            return angel.candles(sym, end - timedelta(days=7), end)
        return yahoo(sym, 5)

    Trader(cfg, broker, feed, lambda m: telegram(m, cfg["telegram"]["enabled"]),
           lambda tab, rows: sheet(tab, rows, cfg["sheet"]["enabled"])).run(stop_at=until)


def do_daemon(cfg):
    """For a VPS/Docker: runs the trading session every weekday, forever."""
    import time
    from bot.live import now_ist
    last = None
    while True:
        now = now_ist()
        if now.weekday() < 5 and "09:05" <= now.strftime("%H:%M") < "15:15" and last != now.date():
            last = now.date()
            try:
                do_live(cfg)
            except Exception as e:
                log.exception("session crashed")
                telegram(f"❌ Session crashed: {e} – restarting in 1 min", cfg["telegram"]["enabled"])
                last = None
        time.sleep(60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backtest", "live", "daemon", "test-telegram", "test-sheet"])
    ap.add_argument("--until", help="HH:MM – end this session early (handover, positions stay protected)")
    ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    if not os.getenv("SHEET_WEBHOOK_URL") and cfg["sheet"].get("webhook_url"):
        os.environ["SHEET_WEBHOOK_URL"] = cfg["sheet"]["webhook_url"]
    if a.cmd == "backtest":
        do_backtest(cfg)
    elif a.cmd == "live":
        do_live(cfg, a.until)
    elif a.cmd == "daemon":
        do_daemon(cfg)
    elif a.cmd == "test-telegram":
        telegram(f"✅ Intraday ORB bot connected | mode <b>{cfg['mode']}</b> | {len(cfg['watchlist'])} stocks")
    elif a.cmd == "test-sheet":
        sheet("Signals", [[datetime.now().strftime("%Y-%m-%d"), datetime.now().strftime("%H:%M"),
                           "TEST", "TEST", 0, 0, 0, 0, "setup check"]])
        print("sent")


if __name__ == "__main__":
    main()
