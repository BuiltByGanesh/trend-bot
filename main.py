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

from bot.notify import sheet, sheet_upsert, telegram  # noqa: E402


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
    syms = list(cfg["watchlist"]) + (list(cfg.get("watchlist_extra", [])) if all_syms or cfg.get("universe") == "all" else [])
    data = history(syms, days, src, angel)
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


def load_history(cfg, all_syms=False):
    from bot.data import AngelData, history
    ab = angel_login() if cfg["data_source"] == "angel" else None
    angel = AngelData(ab.smart, ab.token, pace=1.0) if ab else None
    src = "angel" if angel else "yahoo"
    days = cfg["backtest"]["days"] if angel else 59
    strict = angel is not None   # never mix 1-year Angel data with 59-day Yahoo data
    proxy = history([cfg["market_proxy"]], days, src, angel, allow_fallback=not strict).get(cfg["market_proxy"])
    syms = list(cfg["watchlist"]) + (list(cfg.get("watchlist_extra", [])) if all_syms or cfg.get("universe") == "all" else [])
    data = history(syms, days, src, angel, allow_fallback=not strict)
    return data, proxy, src


def do_variants(cfg, grid="default"):
    import yaml as _y
    from bot.variants import search
    data, proxy, src = load_history(cfg, all_syms=(grid == "rs"))
    log.info("loaded %d symbols: %s", len(data), ", ".join(sorted(data)))
    if proxy is None or not data:
        telegram("❌ Variant test: no data", cfg["telegram"]["enabled"])
        sys.exit(1)
    table, pick, verdict, stress, cut, cfgs = search(data, proxy, cfg, log=log.info, grid=grid)
    os.makedirs("reports", exist_ok=True)
    table.to_csv("reports/variants.csv", index=False)
    top = table.head(15)
    md = [f"# Variant search ({src}, {len(table)} variants)\n",
          f"Ranked on in-sample days only; out-of-sample starts {cut}.\n",
          top.to_markdown(index=False), f"\n**Pick (best in-sample):** `{pick}` → **{verdict}**\n"]
    for k, v in stress.items():
        md.append(f"- {k}: {v['trades']} trades, ₹{v['net_pnl']:,.0f}, PF {v['profit_factor']}")
    cur = table[table.variant == "current_rules"].iloc[0]
    md.append(f"\nCurrent rules for comparison: IS ₹{cur.is_net:,.0f}, OOS ₹{cur.oos_net:,.0f}")
    open("reports/variants.md", "w").write("\n".join(md))
    print("\n".join(md))
    if pick:
        _y.safe_dump({"universe": cfgs[pick].get("universe", "core"), "strategy": cfgs[pick]["strategy"]},
                     open("reports/best_strategy.yaml", "w"), sort_keys=False)
        r = table.set_index("variant").loc[pick]
        msg = (f"🧪 <b>Variant test – {len(table)} versions, 1 year</b>\nBest on first 70% of days: <code>{pick}</code>\n"
               f"First 70%: {int(r.is_trades)} trades ₹{r.is_net:,.0f} (PF {r.is_pf})\n"
               f"Unseen last 30%: {int(r.oos_trades)} trades ₹{r.oos_net:,.0f} (PF {r.oos_pf}, WR {r.oos_wr}%)\n"
               f"Slippage ×2: ₹{stress['slippage_x2']['net_pnl']:,.0f}\nVerdict: <b>{verdict}</b>\n"
               f"Current rules: ₹{cur.full_net:,.0f} over the year")
        telegram(msg, cfg["telegram"]["enabled"])
        run_id = datetime.now().strftime("%Y-%m-%d %H:%M")
        sheet("Backtest", [[run_id, f"VARIANT {row.variant}", f"IS/OOS cut {cut}", row.is_trades + row.oos_trades,
                            row.full_net, round(row.full_net / cfg['risk']['capital'] * 100, 2), row.oos_wr,
                            row.oos_pf, "", row.full_dd, row.costs, f"IS ₹{row.is_net:,.0f} | OOS ₹{row.oos_net:,.0f}"]
                           for row in top.itertuples()], cfg["sheet"]["enabled"])


def do_watch(cfg):
    from bot.data import AngelData, yahoo
    from bot.live import now_ist
    from bot.watch import message, scan
    now = now_ist()
    if not os.getenv("WATCH_FORCE") and (now.weekday() >= 5 or not ("09:30" <= now.strftime("%H:%M") <= "15:30")):
        print("Market closed – no watchlist.")
        return
    ab = angel_login()
    angel = AngelData(ab.smart, ab.token, pace=0.5) if ab else None
    syms = list(cfg["watchlist"]) + [x for x in cfg.get("watchlist_extra", []) if x not in cfg["watchlist"]]
    def fetch(sym):
        try:
            return angel.candles(sym, now - timedelta(days=9), now) if angel else yahoo(sym, 7)
        except Exception as e:
            log.warning("%s: %s", sym, e)
            return None
    proxy = fetch(cfg["market_proxy"])
    frames = {s: f for s in syms if (f := fetch(s)) is not None}
    w = cfg.get("watch", {})
    n, rows = scan(frames, proxy, w.get("min_rs_pct", 1.0), w.get("top", 8))
    if n is None:
        telegram("⚠️ Watchlist: no Nifty data", cfg["telegram"]["enabled"])
        return
    telegram(message(n, rows, w.get("min_rs_pct", 1.0)), cfg["telegram"]["enabled"])
    sheet("Watchlist", [[f"{n['as_of']:%Y-%m-%d}", f"{n['as_of']:%H:%M}", r["sym"], round(r["price"], 2),
                         round(r["chg"], 2), round(n["chg"], 2), round(r["rs"], 2), round(r["vwap"], 2),
                         round(r["vol_x"], 2), round(r["day_high"], 2), round(r["from_high"], 2)] for r in rows],
          cfg["sheet"]["enabled"])
    print(message(n, rows, w.get("min_rs_pct", 1.0)))


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

    if cfg.get("universe") == "all":
        cfg["watchlist"] = list(cfg["watchlist"]) + [x for x in cfg.get("watchlist_extra", []) if x not in cfg["watchlist"]]
    Trader(cfg, broker, feed, lambda m: telegram(m, cfg["telegram"]["enabled"]),
           lambda tab, rows: (sheet_upsert if isinstance(rows, dict) else sheet)(tab, rows, cfg["sheet"]["enabled"])
           ).run(stop_at=until)


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
    ap.add_argument("cmd", choices=["backtest", "variants", "variants-rs", "watch", "swing", "live", "daemon", "test-telegram", "test-sheet"])
    ap.add_argument("--until", help="HH:MM – end this session early (handover, positions stay protected)")
    ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    if not os.getenv("SHEET_WEBHOOK_URL") and cfg["sheet"].get("webhook_url"):
        os.environ["SHEET_WEBHOOK_URL"] = cfg["sheet"]["webhook_url"]
    if a.cmd == "backtest":
        do_backtest(cfg)
    elif a.cmd == "variants":
        do_variants(cfg)
    elif a.cmd == "watch":
        do_watch(cfg)
    elif a.cmd == "variants-rs":
        do_variants(cfg, grid="rs")
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
