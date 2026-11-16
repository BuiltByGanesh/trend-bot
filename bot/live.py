"""
Live / paper trader for the 5-min opening-range breakout.

- Signals and trailing-stop updates use COMPLETED 5-min candles only.
- The protective stop lives at the broker as an SL-limit order, so it works between candle closes.
  Every poll checks it: filled -> record exit; rejected/cancelled -> exit at market and alert;
  price gapped below its limit -> cancel and exit at market.
- Duplicate orders: every signal gets a key (symbol + candle time) saved BEFORE the order goes out,
  and the order carries a tag that is checked in the broker's order book after a restart.
- Partial entry fills: the unfilled rest is cancelled and the stop covers only filled shares.
- 15:10: flatten everything, then confirm with the broker that every position is flat;
  retry failed exits and alert on anything still open.
"""
import hashlib
import json
import logging
import time as _time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import signals as sg
from .broker import round_down
from .costs import order_cost
from .risk import DayRisk

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    return datetime.now(IST).replace(tzinfo=None)


class Trader:
    def __init__(self, cfg, broker, feed, alert, sheet, clock=now_ist, sleep=_time.sleep,
                 state_path="state/live_state.json"):
        self.cfg, self.b, self.feed, self.alert_fn, self.sheet = cfg, broker, feed, alert, sheet
        self.s, self.r, self.c, self.L = cfg["strategy"], cfg["risk"], cfg["costs"], cfg["live"]
        self.clock, self.sleep, self.path = clock, sleep, Path(state_path)
        self.load()

    # ---------- state ----------
    def load(self):
        today = str(self.clock().date())
        st = json.loads(self.path.read_text()) if self.path.exists() else {}
        if st.get("date") != today:
            st = {"date": today, "positions": {}, "signals_done": [], "last_candle": None, "eod_done": False,
                  "stale_alerted": False, "risk": {}, "closed": []}
        self.st = st
        rk = st["risk"]
        self.risk = DayRisk(self.r["capital"], self.r, **{k: rk[k] for k in
                            ("realised", "entries", "consec_losses", "halted", "flatten", "per_stock") if k in rk})

    def save(self):
        self.st["risk"] = {k: v for k, v in asdict(self.risk).items() if k not in ("capital", "r")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, indent=2, default=str))
        tmp.replace(self.path)

    def alert(self, msg):
        log.info(msg)
        self.alert_fn(msg)

    # ---------- helpers ----------
    def _wait_order(self, oid, want=("complete", "rejected", "cancelled")):
        end = _time.monotonic() + self.L["order_wait_seconds"]
        o = {}
        while True:
            o = self.b.orders().get(oid, {})
            if o.get("status") in want or _time.monotonic() >= end:
                return o
            self.sleep(1)

    def _sl_prices(self, sym, stop):
        tick = self.b.tick(sym)
        trig = round_down(stop, tick)
        return trig, round_down(trig * (1 - self.L["sl_limit_buffer_pct"] / 100), tick)

    def open_risk(self):
        return sum(max(0.0, (p["entry"] - p["stop"]) * p["qty"]) for p in self.st["positions"].values())

    def open_value(self):
        return sum(p["entry"] * p["qty"] for p in self.st["positions"].values())

    # ---------- entries ----------
    def enter(self, sym, qty, atr, sig_ts):
        key = f"{sym}|{sig_ts:%Y%m%d%H%M}"
        if key in self.st["signals_done"]:
            log.info("duplicate signal %s ignored", key)
            return
        tag = "ORB" + hashlib.md5(key.encode()).hexdigest()[:12]
        self.st["signals_done"].append(key)
        self.save()  # recorded BEFORE the order: a crash/restart can never send it twice
        if self.b.find_tag(tag):
            self.alert(f"⚠️ {sym}: order for this signal already exists at broker – not sending again")
            return
        try:
            oid = self.b.place(sym, "BUY", qty, "MARKET", tag=tag)
        except Exception as e:
            self.alert(f"❌ {sym} BUY not placed: {e}")
            return
        o = self._wait_order(oid)
        filled = int(o.get("filled") or 0)
        if o.get("status") != "complete":
            if o.get("status") in ("open", "trigger_pending"):
                try:
                    self.b.cancel(oid, "MARKET")
                except Exception as e:
                    log.warning("cancel rest of %s: %s", oid, e)
                o = self.b.orders().get(oid, o)
                filled = int(o.get("filled") or 0)
            if filled == 0:
                self.alert(f"❌ {sym} BUY {o.get('status')} – {o.get('text', '')}")
                return
            self.alert(f"⚠️ {sym} partial fill {filled}/{qty} – rest cancelled, stop covers {filled}")
        avg = float(o["avg"])
        self.risk.record_entry(sym)
        stop = avg - self.s["initial_stop_atr"] * atr
        pos = dict(qty=filled, entry=avg, stop=stop, init_stop=stop, highest=avg, t=str(self.clock())[11:16],
                   sl_id=None, tag=tag, nth=self.risk.per_stock[sym],
                   entry_cost=order_cost(avg * filled, "BUY", self.c))
        self.st["positions"][sym] = pos
        self.save()
        if not self.place_stop(sym):
            return
        tgt = round(avg + 2 * (avg - pos["stop"]), 2)
        pos["target"] = tgt
        self.save()
        self.sheet("Trade_Log", {
            "ID": tag, "Buy date": self.st["date"], "Buy time": pos["t"], "Stock": sym, "Qty": filled,
            "Buy price": round(avg, 2), "Stop-loss (initial)": round(pos["stop"], 2),
            "Stop-loss (current)": round(pos["stop"], 2), "Target (2R guide)": tgt,
            "Risk ₹": round((avg - pos["stop"]) * filled, 2), "Sell by": f"{self.st['date']} {self.s['eod_exit']}",
            "When to sell": "If price falls to the CURRENT stop-loss (it moves up as price rises), "
                            f"or at {self.s['eod_exit']} today – whichever comes first",
            "Status": "OPEN", "Mode": self.b.name})
        if self.b.name == "alerts":
            self.alert(f"📢 <b>BUY {sym}</b> now (market) ≈ ₹{avg:.2f}\nQty: {filled}  (risk ₹{(avg - pos['stop']) * filled:,.0f})"
                       f"\nStop-loss: ₹{pos['stop']:.2f} – place it in Angel One right after buying"
                       f"\nTarget (guide, 2× risk): ₹{avg + 2 * (avg - pos['stop']):.2f}"
                       f"\nI'll tell you when to move the stop up or exit. Exit by 3:10 PM at the latest.")
        else:
            self.alert(f"🟢 <b>BUY {sym}</b> x{filled} @ ₹{avg:.2f}\nSL ₹{pos['stop']:.2f} (at broker, trails 1.5×ATR)"
                       f"\nRisk ₹{(avg - pos['stop']) * filled:,.0f} | entry #{pos['nth']} | {self.b.name}")
        self.sheet("Signals", [[self.st["date"], pos["t"], sym, "BUY", filled, round(avg, 2), round(pos["stop"], 2),
                                round((avg - pos["stop"]) * filled, 2), self.b.name]])

    def place_stop(self, sym):
        """Place the protective SL. If the broker rejects it twice, get out at market."""
        p = self.st["positions"][sym]
        for attempt in (1, 2):
            trig, lim = self._sl_prices(sym, p["stop"] if attempt == 1 else p["stop"] * 0.999)
            try:
                oid = self.b.place(sym, "SELL", p["qty"], "SL", trigger=trig, limit=lim, tag=p["tag"] + "S")
                o = self._wait_order(oid, want=("trigger_pending", "complete", "rejected", "cancelled"))
                if o.get("status") in ("trigger_pending", "open", "complete"):
                    p["sl_id"], p["stop"] = oid, trig
                    self.save()
                    return True
                err = o.get("text") or o.get("status")
            except Exception as e:
                err = str(e)
            log.warning("SL for %s failed (attempt %d): %s", sym, attempt, err)
        self.alert(f"🚨 {sym}: stop-loss rejected ({err}) – exiting at market now")
        self.exit_position(sym, "SL_REJECTED")
        return False

    # ---------- exits ----------
    def record_exit(self, sym, px, reason):
        p = self.st["positions"].pop(sym)
        exit_cost = order_cost(px * p["qty"], "SELL", self.c)
        net = (px - p["entry"]) * p["qty"] - p["entry_cost"] - exit_cost
        self.risk.record_exit(net)
        row = [self.st["date"], sym, p["t"], round(p["entry"], 2), str(self.clock())[11:16], round(px, 2), p["qty"],
               reason, round(p["init_stop"], 2), round(p["entry_cost"] + exit_cost, 2), round(net, 2), self.b.name]
        self.st["closed"].append(row)
        self.save()
        why = {"STOP": "stop-loss hit", "TRAIL_STOP": "trailing stop-loss hit – profit locked",
               "STOP_GAP": "price fell through the stop-loss", "EOD": "end of day (3:10 PM) – don't hold overnight",
               "DAILY_LOSS_LIMIT": "daily loss limit reached – no more trades today"}.get(reason, reason)
        head = f"📢 <b>SELL / EXIT {sym}</b> now ≈ ₹{px:.2f}\nReason: {why}" if self.b.name == "alerts" \
            else f"🔴 <b>SELL {sym}</b> x{p['qty']} @ ₹{px:.2f} – {reason}"
        self.alert(head + f"\nNet P&L ₹{net:,.0f} (after costs)"
                   f" | Day ₹{self.risk.realised:,.0f}" + (f"\n⛔ New entries stopped: {self.risk.halted}" if self.risk.halted else ""))
        self.sheet("Trades", [row])
        self.sheet("Trade_Log", {
            "ID": p["tag"], "Status": "CLOSED", "Sell date": self.st["date"], "Sell time": str(self.clock())[11:16],
            "Sell price": round(px, 2), "Exit reason": why, "Costs ₹": round(p["entry_cost"] + exit_cost, 2),
            "Net P&L ₹": round(net, 2), "P&L %": round(net / (p["entry"] * p["qty"]) * 100, 2),
            "Stop-loss (current)": round(p["stop"], 2)})

    def exit_position(self, sym, reason):
        p = self.st["positions"].get(sym)
        if not p:
            return True
        if p.get("sl_id"):
            o = self.b.orders().get(p["sl_id"], {})
            if o.get("status") == "complete":
                self.record_exit(sym, float(o["avg"]), "STOP")
                return True
            try:
                self.b.cancel(p["sl_id"], "SL")
            except Exception as e:
                log.warning("cancel SL %s: %s", sym, e)
            o = self.b.orders().get(p["sl_id"], {})
            if o.get("status") == "complete":  # filled while we were cancelling
                self.record_exit(sym, float(o["avg"]), "STOP")
                return True
        try:
            oid = self.b.place(sym, "SELL", p["qty"], "MARKET", tag=p["tag"] + "X")
            o = self._wait_order(oid)
        except Exception as e:
            o = {"status": "error", "text": str(e)}
        if o.get("status") == "complete":
            self.record_exit(sym, float(o["avg"]), reason)
            return True
        self.alert(f"🚨 EXIT FAILED {sym} ({reason}): {o.get('status')} {o.get('text', '')} – will retry")
        return False

    def flatten_all(self, reason):
        for sym in list(self.st["positions"]):
            self.exit_position(sym, reason)
        return self.verify_flat(reason)

    def verify_flat(self, reason):
        """Confirm with the broker; retry residual quantities; alert on anything left."""
        traded = {k.split("|")[0] for k in self.st["signals_done"]}
        for attempt in range(1, self.L["exit_retries"] + 1):
            net = {k: v for k, v in self.b.net_qty().items() if v and k in traded}
            if not net:
                if reason == "EOD":
                    self.alert("✅ EOD: broker confirms all positions are flat")
                return True
            for sym, q in net.items():
                if sym in self.st["positions"]:
                    self.exit_position(sym, reason)
                elif q > 0:
                    try:
                        self.b.place(sym, "SELL", q, "MARKET", tag=f"FLAT{sym[:8]}{attempt}")
                    except Exception as e:
                        log.error("flat retry %s: %s", sym, e)
            self.sleep(2)
        left = {k: v for k, v in self.b.net_qty().items() if v and k in traded}
        if left:
            self.alert(f"🚨🚨 NOT FLAT after {self.L['exit_retries']} tries: {left} – CLOSE MANUALLY in Angel One")
            return False
        return True

    # ---------- between candles ----------
    def poll(self):
        orders = self.b.orders()
        for sym in list(self.st["positions"]):
            p = self.st["positions"][sym]
            o = orders.get(p.get("sl_id"), {})
            stt = o.get("status")
            if stt == "complete":
                self.record_exit(sym, float(o["avg"]), "TRAIL_STOP" if p["stop"] > p["entry"] else "STOP")
            elif stt in ("rejected", "cancelled"):
                self.alert(f"🚨 {sym}: stop-loss order {stt} at broker ({o.get('text', '')}) – exiting at market")
                self.exit_position(sym, "SL_LOST")
            else:
                try:
                    px = self.b.ltp(sym)
                except Exception:
                    continue
                p["last"] = px
                _, lim = self._sl_prices(sym, p["stop"])
                if px < lim:  # gapped through the SL-limit: it won't fill, so exit now
                    self.alert(f"🚨 {sym} ₹{px:.2f} gapped below SL limit ₹{lim:.2f} – exiting at market")
                    self.exit_position(sym, "STOP_GAP")
        unreal = sum((p.get("last", p["entry"]) - p["entry"]) * p["qty"] - p["entry_cost"]
                     - order_cost(p.get("last", p["entry"]) * p["qty"], "SELL", self.c)
                     for p in self.st["positions"].values())
        if not self.risk.flatten and self.risk.check_loss(unreal):
            self.alert(f"⛔ Daily loss limit hit (₹{self.risk.realised + unreal:,.0f}) – closing all, no more trades today")
            self.flatten_all("DAILY_LOSS_LIMIT")
        self.save()

    # ---------- on each completed candle ----------
    def on_candle(self, bar_start: datetime):
        s = self.s
        frames = {}
        for sym in list(self.cfg["watchlist"]) + [self.cfg["market_proxy"]]:
            try:
                frames[sym] = self.feed(sym)
            except Exception as e:
                log.warning("feed %s: %s", sym, e)
        proxy = frames.pop(self.cfg["market_proxy"], None)
        fresh = {k: v for k, v in frames.items() if len(v) and v.index[-1] >= bar_start}
        stale = proxy is None or not len(proxy) or proxy.index[-1] < bar_start
        if stale or len(fresh) < len(frames) / 2:
            if not self.st["stale_alerted"]:
                self.alert(f"⚠️ Data delayed for {bar_start:%H:%M} candle – no new entries until it catches up")
                self.st["stale_alerted"] = True
        else:
            self.st["stale_alerted"] = False

        # trail stops on completed candles (move up only)
        for sym, p in self.st["positions"].items():
            if sym not in fresh:
                continue
            d = sg.prepare(fresh[sym].loc[:bar_start], s)
            row = d.iloc[-1]
            p["highest"] = max(p["highest"], float(row.high))
            new = sg.trail(p["stop"], p["highest"], float(row.atr), s)
            trig, lim = self._sl_prices(sym, new)
            if trig > p["stop"] + 1e-9 and p.get("sl_id"):
                try:
                    self.b.modify_sl(p["sl_id"], sym, p["qty"], trig, lim)
                    self.alert(f"⬆️ {sym}: move your stop-loss UP to ₹{trig:.2f} (was ₹{p['stop']:.2f})"
                               if self.b.name == "alerts" else f"⬆️ {sym} SL ₹{p['stop']:.2f} → ₹{trig:.2f}")
                    p["stop"] = trig
                    self.sheet("Trade_Log", {"ID": p["tag"], "Stop-loss (current)": trig})
                except Exception as e:
                    self.alert(f"⚠️ {sym} SL modify failed ({e}) – old stop ₹{p['stop']:.2f} still active")
        self.save()
        if stale or self.risk.flatten:
            return

        mkt = sg.market_filter(proxy.loc[:bar_start])
        for sym, df in fresh.items():
            if sym in self.st["positions"] or self.risk.can_enter(sym, s):
                continue
            d = sg.prepare(df.loc[:bar_start], s)
            if d.index[-1] != bar_start or not bool(sg.signal_mask(d.iloc[-60:], mkt, s).iloc[-1]):
                continue
            row = d.iloc[-1]
            try:
                spread = self.b.spread_pct(sym)
            except Exception:
                spread = 99
            if spread > self.L["max_spread_pct"]:
                log.info("%s signal skipped: spread %.3f%%", sym, spread)
                continue
            est_stop = row.close - s["initial_stop_atr"] * row.atr
            qty = sg.size_position(row.close, est_stop, self.r["capital"], self.open_risk(), self.open_value(), self.r)
            if qty >= 1:
                self.enter(sym, qty, float(row.atr), bar_start)

    # ---------- main loop ----------
    def run(self, stop_at: str = None):
        """stop_at='HH:MM' hands over to another session without closing positions (broker SLs stay live)."""
        s = self.s
        step = timedelta(minutes=s["bar_minutes"])
        if self.st["eod_done"]:
            return
        self.alert(f"🔔 Intraday bot started ({self.b.name}) – {len(self.cfg['watchlist'])} stocks, "
                   f"risk {self.r['risk_per_trade_pct']}%/trade, daily stop {self.r['daily_loss_limit_pct']}%")
        self.reconcile()
        while True:
            now = self.clock()
            if now.time() >= sg._t("15:30"):
                break
            if stop_at and now.time() >= sg._t(stop_at) and not sg.is_eod(now, s):
                self.save()
                self.alert(f"🔁 Session handover at {stop_at} – {len(self.st['positions'])} open, stops live at broker")
                return
            if sg.is_eod(now, s):
                if not self.st["eod_done"]:
                    self.flatten_all("EOD")
                    self.st["eod_done"] = True
                    self.save()
                    self.day_summary()
                break
            # last completed candle start
            mins = (now.hour * 60 + now.minute) // s["bar_minutes"] * s["bar_minutes"]
            cur_start = now.replace(hour=mins // 60, minute=mins % 60, second=0, microsecond=0)
            done = cur_start - step
            settled = (now - cur_start).total_seconds() >= self.L["data_settle_seconds"]
            if settled and done.time() >= sg._t("09:15") and str(done) != self.st["last_candle"]:
                self.st["last_candle"] = str(done)
                try:
                    self.on_candle(done)
                except Exception as e:
                    log.exception("candle failed")
                    self.alert(f"⚠️ candle {done:%H:%M} error: {e}")
            try:
                self.poll()
            except Exception as e:
                log.exception("poll failed")
                self.alert(f"⚠️ poll error: {e}")
            self.sleep(self.L["poll_seconds"])

    def reconcile(self):
        """After a restart: broker positions vs saved state."""
        try:
            net = self.b.net_qty()
        except Exception as e:
            self.alert(f"⚠️ Could not read broker positions: {e}")
            return
        for sym in list(self.st["positions"]):
            if net.get(sym, 0) <= 0:
                o = self.b.orders().get(self.st["positions"][sym].get("sl_id"), {})
                self.record_exit(sym, float(o.get("avg") or self.st["positions"][sym]["entry"]), "CLOSED_WHILE_OFFLINE")
        unknown = {k: v for k, v in net.items() if v and k not in self.st["positions"]}
        if unknown:
            self.alert(f"⚠️ Positions at broker not opened by this bot today: {unknown} – bot won't touch them")

    def day_summary(self):
        rows = self.st["closed"]
        net = sum(r[10] for r in rows)
        wins = sum(1 for r in rows if r[10] > 0)
        lines = "\n".join(f"• {r[1]} {r[7]} ₹{r[10]:,.0f}" for r in rows) or "No trades"
        self.alert(f"📊 <b>Day summary {self.st['date']}</b>\n{lines}\nNet ₹{net:,.0f} | "
                   f"{wins}/{len(rows)} winners" + (f"\nHalted: {self.risk.halted}" if self.risk.halted else ""))
        self.sheet("Daily", [[self.st["date"], len(rows), wins, round(net, 2),
                              round(net / self.r["capital"] * 100, 3), self.risk.halted, self.b.name]])
