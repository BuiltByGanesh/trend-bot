"""Live executor against a fake broker with injected failures (no network)."""
import copy
from datetime import datetime

import pandas as pd
import pytest
import yaml

from bot import signals as sg
from bot.broker import PaperBroker
from bot.live import Trader
from tests.synth import make

CFG = yaml.safe_load(open("config.yaml"))
CFG["live"]["order_wait_seconds"] = 0.05
CLOCK = lambda: datetime(2026, 3, 2, 11, 0)


class FakeBroker(PaperBroker):
    def __init__(self, prices, **kw):
        self.prices = prices
        super().__init__(lambda s: self.prices[s], slippage_pct=0)
        self.partial = kw.get("partial")          # fill only this many on BUY, leave rest open
        self.reject_sl = kw.get("reject_sl", 0)   # reject this many SL placements
        self.fail_sells = kw.get("fail_sells", 0) # market sells that get rejected
        self.calls = []

    def place(self, sym, side, qty, kind="MARKET", trigger=None, limit=None, tag=""):
        self.calls.append((sym, side, qty, kind))
        if kind == "SL" and self.reject_sl:
            self.reject_sl -= 1
            self._n += 1
            oid = f"P{self._n}"
            self._orders[oid] = dict(symbol=sym, side=side, qty=qty, kind=kind, trigger=trigger, limit=limit,
                                     tag=tag, status="rejected", filled=0, avg=0, text="RMS: price band")
            return oid
        if kind == "MARKET" and side == "SELL" and self.fail_sells:
            self.fail_sells -= 1
            self._n += 1
            oid = f"P{self._n}"
            self._orders[oid] = dict(symbol=sym, side=side, qty=qty, kind=kind, trigger=None, limit=None,
                                     tag=tag, status="rejected", filled=0, avg=0, text="rejected")
            return oid
        if kind == "MARKET" and side == "BUY" and self.partial:
            self._n += 1
            oid = f"P{self._n}"
            self._orders[oid] = dict(symbol=sym, side=side, qty=qty, kind="STUCK", trigger=None, limit=None, tag=tag,
                                     status="open", filled=self.partial, avg=self.prices[sym], text="")
            self._pos[sym] = self._pos.get(sym, 0) + self.partial
            return oid
        return super().place(sym, side, qty, kind, trigger, limit, tag)

    def cancel(self, oid, kind="SL"):
        o = self._orders[oid]
        if o["status"] in ("open", "trigger_pending"):
            o["status"] = "cancelled"


def trader(tmp_path, broker, feed=None):
    alerts, rows = [], []
    t = Trader(copy.deepcopy(CFG), broker, feed or (lambda s: pd.DataFrame()), alerts.append,
               lambda tab, r: rows.append((tab, r)), clock=CLOCK, sleep=lambda s: None,
               state_path=tmp_path / "s.json")
    return t, alerts, rows


def test_entry_places_stop_at_broker(tmp_path):
    b = FakeBroker({"SBIN": 500.0})
    t, alerts, rows = trader(tmp_path, b)
    t.enter("SBIN", 100, atr=1.0, sig_ts=datetime(2026, 3, 2, 10, 0))
    p = t.st["positions"]["SBIN"]
    assert p["qty"] == 100 and p["stop"] == pytest.approx(498.5)
    assert b.orders()[p["sl_id"]]["status"] == "trigger_pending"
    assert {"Signals", "Trade_Log"} <= {tab for tab, _ in rows}


def test_duplicate_signal_never_sends_twice(tmp_path):
    b = FakeBroker({"SBIN": 500.0})
    t, *_ = trader(tmp_path, b)
    ts = datetime(2026, 3, 2, 10, 0)
    t.enter("SBIN", 100, 1.0, ts)
    t.st["positions"].clear()
    t.enter("SBIN", 100, 1.0, ts)
    assert sum(1 for c in b.calls if c[1] == "BUY") == 1
    # restart with state lost: broker tag check still blocks it
    (tmp_path / "s.json").unlink()
    t2, alerts, _ = trader(tmp_path, b)
    t2.enter("SBIN", 100, 1.0, ts)
    assert sum(1 for c in b.calls if c[1] == "BUY") == 1
    assert any("already exists" in a for a in alerts)


def test_partial_fill_cancels_rest_and_sizes_stop(tmp_path):
    b = FakeBroker({"SBIN": 500.0}, partial=40)
    t, alerts, _ = trader(tmp_path, b)
    t.enter("SBIN", 100, 1.0, datetime(2026, 3, 2, 10, 0))
    assert t.st["positions"]["SBIN"]["qty"] == 40
    assert any("partial fill 40/100" in a for a in alerts)
    sl = [c for c in b.calls if c[3] == "SL"][0]
    assert sl[2] == 40


def test_rejected_stop_exits_immediately(tmp_path):
    b = FakeBroker({"SBIN": 500.0}, reject_sl=2)
    t, alerts, _ = trader(tmp_path, b)
    t.enter("SBIN", 100, 1.0, datetime(2026, 3, 2, 10, 0))
    assert "SBIN" not in t.st["positions"]
    assert t.st["closed"][0][7] == "SL_REJECTED"
    assert b.net_qty().get("SBIN", 0) == 0
    assert any("stop-loss rejected" in a for a in alerts)


def test_stop_hit_between_candles_is_recorded(tmp_path):
    b = FakeBroker({"SBIN": 500.0})
    t, *_ = trader(tmp_path, b)
    t.enter("SBIN", 100, 1.0, datetime(2026, 3, 2, 10, 0))
    b.prices["SBIN"] = 498.0          # trigger 498.5, limit ~496.0 -> fills
    t.poll()
    assert "SBIN" not in t.st["positions"] and t.st["closed"][0][7] == "STOP"
    assert t.risk.consec_losses == 1


def test_gap_through_limit_exits_at_market(tmp_path):
    b = FakeBroker({"SBIN": 500.0})
    t, alerts, _ = trader(tmp_path, b)
    t.enter("SBIN", 100, 1.0, datetime(2026, 3, 2, 10, 0))
    b.prices["SBIN"] = 490.0          # below the SL limit -> SL-limit can't fill
    t.poll()
    assert t.st["closed"][0][7] == "STOP_GAP"
    assert b.net_qty()["SBIN"] == 0


def test_daily_loss_limit_flattens_and_halts(tmp_path):
    b = FakeBroker({"SBIN": 500.0, "TCS": 3000.0})
    t, alerts, _ = trader(tmp_path, b)
    t.risk.realised = -900
    t.enter("TCS", 30, 20.0, datetime(2026, 3, 2, 10, 0))   # wide stop so the SL itself isn't hit
    b.prices["TCS"] = 2995.0
    t.poll()
    assert t.risk.flatten and t.risk.halted == "daily loss limit"
    assert not t.st["positions"] and b.net_qty()["TCS"] == 0
    assert t.risk.can_enter("SBIN", CFG["strategy"]) == "daily loss limit"


def test_eod_flatten_retries_and_confirms(tmp_path):
    b = FakeBroker({"SBIN": 500.0}, fail_sells=1)
    t, alerts, _ = trader(tmp_path, b)
    t.enter("SBIN", 100, 1.0, datetime(2026, 3, 2, 10, 0))
    assert t.flatten_all("EOD")
    assert b.net_qty()["SBIN"] == 0
    assert any("EXIT FAILED" in a for a in alerts) and any("all positions are flat" in a for a in alerts)


def test_eod_alerts_when_cannot_flatten(tmp_path):
    b = FakeBroker({"SBIN": 500.0}, fail_sells=99)
    t, alerts, _ = trader(tmp_path, b)
    t.enter("SBIN", 100, 1.0, datetime(2026, 3, 2, 10, 0))
    assert not t.flatten_all("EOD")
    assert any("NOT FLAT" in a for a in alerts)


def test_on_candle_matches_backtest_signal(tmp_path):
    data = {f"S{i}": make(10 + i, days=40, base=300 + 100 * i, trend=0.0012) for i in range(6)}
    proxy = make(99, days=40, base=280, trend=0.0008)
    mkt = sg.market_filter(proxy)
    hit = None
    for sym, df in data.items():
        p = sg.prepare(df, CFG["strategy"])
        m = sg.signal_mask(p, mkt, CFG["strategy"])
        if m.any():
            hit = (sym, m[m].index[5] if m.sum() > 5 else m[m].index[0])
            break
    assert hit
    sym, ts = hit
    feeds = {**data, "NIFTYBEES": proxy}
    b = FakeBroker({k: float(v.loc[:ts].close.iloc[-1]) for k, v in data.items()})
    cfg = copy.deepcopy(CFG)
    cfg["watchlist"] = list(data)
    alerts, rows = [], []
    t = Trader(cfg, b, lambda s: feeds[s].loc[:ts], alerts.append, lambda *a: None,
               clock=lambda: ts, sleep=lambda s: None, state_path=tmp_path / "s.json")
    t.on_candle(ts)
    assert sym in t.st["positions"], alerts


def test_alert_mode_messages(tmp_path):
    b = FakeBroker({"SBIN": 500.0})
    b.name = "alerts"
    t, alerts, _ = trader(tmp_path, b)
    t.enter("SBIN", 100, 1.0, datetime(2026, 3, 2, 10, 0))
    b.prices["SBIN"] = 498.0
    t.poll()
    assert "BUY SBIN" in alerts[0] and "Stop-loss: ₹498.50" in alerts[0]
    assert "SELL / EXIT SBIN" in alerts[-1] and "stop-loss hit" in alerts[-1]


def test_trade_log_row_lifecycle(tmp_path):
    b = FakeBroker({"SBIN": 500.0})
    b.name = "alerts"
    t, alerts, rows = trader(tmp_path, b)
    t.enter("SBIN", 100, 1.0, datetime(2026, 3, 2, 10, 0))
    b.prices["SBIN"] = 498.0
    t.poll()
    log = [r for tab, r in rows if tab == "Trade_Log"]
    opened, closed = log[0], log[-1]
    assert opened["Status"] == "OPEN" and opened["Stock"] == "SBIN" and opened["Target (2R guide)"] == pytest.approx(503.0)
    assert opened["Sell by"].endswith("15:10") and "stop-loss" in opened["When to sell"]
    assert closed["ID"] == opened["ID"] and closed["Status"] == "CLOSED" and closed["Exit reason"] == "stop-loss hit"
    assert closed["Net P&L ₹"] < 0 and closed["Sell price"] > 0


def test_live_target_exit(tmp_path):
    b = FakeBroker({"SBIN": 500.0})
    b.name = "alerts"
    t, alerts, rows = trader(tmp_path, b)
    t.s.update(target_r=2, trail_stop_atr=0)
    t.enter("SBIN", 100, 1.0, datetime(2026, 3, 2, 10, 0))
    assert "Target: ₹503.00" in alerts[0]
    b.prices["SBIN"] = 503.2
    t.poll()
    assert t.st["closed"][0][7] == "TARGET" and "target reached" in alerts[-1]
