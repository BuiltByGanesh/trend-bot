import numpy as np
import pandas as pd
import pytest
import yaml

from bot import signals as sg
from bot.backtest import Scenario, full_report, run
from bot.costs import order_cost
from bot.risk import DayRisk
from tests.synth import day_index, make

CFG = yaml.safe_load(open("config.yaml"))
S, R = CFG["strategy"], CFG["risk"]


def test_sizing_matches_spec_example():
    # Rs 1,00,000 capital, 0.25% risk = Rs 250, entry 500, stop 498 -> 125 shares
    assert sg.size_position(500, 498, 100000, 0, 0, R) == 125


def test_open_risk_budget_shrinks_size():
    # 0.75% total = Rs 750; Rs 600 already at risk -> only Rs 150 left -> 75 shares
    assert sg.size_position(500, 498, 100000, 600, 0, R) == 75
    assert sg.size_position(500, 498, 100000, 750, 0, R) == 0


def test_margin_caps_size():
    # 5x leverage on 1L = 5L exposure; 4.9L already used -> only 20 shares of 500
    assert sg.size_position(500, 498, 100000, 0, 490000, R) == 20


def test_trail_never_moves_down():
    assert sg.trail(100, 101, 5, S) == 100
    assert sg.trail(100, 110, 2, S) == 107


def _one_day(closes, vols, highs_or=None):
    idx = day_index("2026-02-02")[: len(closes)]
    c = np.array(closes, float)
    df = pd.DataFrame(dict(open=c, high=c + 0.2, low=c - 0.2, close=c, volume=np.array(vols, float)), index=idx)
    if highs_or is not None:
        df.iloc[:3, df.columns.get_loc("high")] = highs_or
    return df


def test_opening_range_high_and_fresh_breakout_rule():
    # OR candles 09:15/09:20/09:25 with high 101. 09:30 breaks out on LOW volume (no signal),
    # 09:35 stays above (not fresh -> no signal), 09:40 dips below, 09:45 re-breaks on high volume -> signal.
    closes = [100, 100.5, 100.8, 101.5, 101.8, 100.9, 101.6, 101.9]
    vols = [1000] * 3 + [900, 5000, 1000, 6000, 1000]
    pre = make(1, days=1, start="2026-01-30")  # previous day for EMA / volume history
    pre["volume"] = 1000.0
    pre["close"] = pre["open"] = pre["high"] = pre["low"] = 99.0
    df = pd.concat([pre, _one_day(closes, vols, highs_or=[100.2, 100.7, 101.0])])
    p = sg.prepare(df, S)
    day = p[p.index.date == pd.Timestamp("2026-02-02").date()]
    assert day["orh"].dropna().iloc[0] == pytest.approx(101.0)
    mkt = pd.Series(True, index=p.index)
    m = sg.signal_mask(p, mkt, S)[day.index]
    fired = list(day.index[m.to_numpy()].strftime("%H:%M"))
    assert fired == ["09:45"], fired


def test_no_signal_when_market_below_vwap():
    df = make(2, days=5)
    p = sg.prepare(df, S)
    assert not sg.signal_mask(p, pd.Series(False, index=p.index), S).any()


def test_risk_gates():
    k = DayRisk(100000, R)
    for _ in range(3):
        k.record_exit(-100)
    assert k.can_enter("X", S) == "3 consecutive losses"
    k = DayRisk(100000, R)
    for sym in "ABCD":
        k.record_entry(sym)
    assert k.can_enter("E", S) == "max entries per day"
    k = DayRisk(100000, R)
    k.record_entry("A"); k.record_entry("A")
    assert k.can_enter("A", S) == "max re-entries for stock"
    k = DayRisk(100000, R)
    k.record_exit(-600)
    assert not k.check_loss(-300)
    assert k.check_loss(-450) and k.halted == "daily loss limit"


def test_costs_reasonable():
    buy, sell = order_cost(50000, "BUY", CFG["costs"]), order_cost(50000, "SELL", CFG["costs"])
    assert 20 < buy < 30 and 30 < sell < 40   # ~Rs 20 brokerage + taxes; STT only on sell


@pytest.fixture(scope="module")
def bt():
    data = {f"S{i}": make(10 + i, days=40, base=300 + 100 * i, trend=0.0012) for i in range(6)}
    proxy = make(99, days=40, base=280, trend=0.0008)
    return data, proxy


def test_backtest_respects_all_limits(bt):
    data, proxy = bt
    trades, daily = run(data, proxy, CFG)
    assert len(trades) > 0
    per_day = trades.groupby("date").size()
    assert per_day.max() <= R["max_entries_per_day"]
    assert trades.groupby(["date", "symbol"]).size().max() <= S["max_entries_per_stock"]
    assert (trades.entry_time >= "09:35").all() and (trades.entry_time <= "14:35").all()
    assert (trades.exit_time <= "15:10").all()
    start_cap = (R["capital"] + daily.pnl.cumsum().shift(fill_value=0)).set_axis(daily.date)
    risk_cap = trades.date.map(start_cap) * R["risk_per_trade_pct"] / 100
    planned = (trades.entry - trades.init_stop) * trades.qty
    assert (planned <= risk_cap * 1.10).all()          # entry gap vs signal close can shift risk slightly
    for d, g in trades.groupby("date"):                 # no new entries after daily-loss / 3-loss halt
        dl = g[g.reason == "DAILY_LOSS_LIMIT"]
        if len(dl):
            assert (g.entry_time <= dl.exit_time.min()).all()


def test_stress_scenarios_cost_more(bt):
    data, proxy = bt
    base, _ = run(data, proxy, CFG)
    worse, _ = run(data, proxy, CFG, Scenario(slip_mult=3))
    assert worse.pnl.sum() < base.pnl.sum()


def test_full_report_sections(bt):
    out, *_ = full_report(*bt, CFG)
    for k in ["base", "in_sample", "out_of_sample", "sideways_days", "trending_days",
              "slippage_x2", "slippage_x3", "costs_x1.5", "entry_delayed_1_bar"]:
        assert k in out


def test_resample_15min_aligned():
    df = make(3, days=2)
    r = sg.resample(df, 15)
    assert r.index[0].strftime("%H:%M") == "09:15" and r.index[1].strftime("%H:%M") == "09:30"
    first = df.loc[df.index[0]: df.index[2]]
    assert r.iloc[0].high == first.high.max() and r.iloc[0].volume == pytest.approx(first.volume.sum())


def test_or_low_stop_and_target_and_no_trail():
    s = dict(S, stop_mode="or_low", target_r=2, trail_stop_atr=0)
    assert sg.initial_stop(100, 1.0, 98.0, s) == pytest.approx(97.9)       # below OR low
    assert sg.initial_stop(100, 1.0, 99.5, s) == pytest.approx(98.5)       # never tighter than 1.5 ATR
    assert sg.target_price(100, 98, s) == pytest.approx(104)
    assert sg.trail(98, 120, 1, s) == 98


def test_market_strength_filter():
    p = make(5, days=3)
    base, flat, strict = sg.market_filter(p), sg.market_filter(p, 0.0), sg.market_filter(p, 0.5)
    assert strict.sum() <= flat.sum() <= base.sum()


def test_variant_backtest_has_target_exits(bt):
    data, proxy = bt
    cfg = yaml.safe_load(open("config.yaml"))
    cfg["strategy"].update(stop_mode="or_low", initial_stop_atr=3, target_r=1, trail_stop_atr=0, bar_minutes=15)
    trades, daily = run(data, proxy, cfg)
    assert len(trades) and (trades.reason == "TARGET").any()
    tg = trades[trades.reason == "TARGET"]
    assert (tg.exit >= tg.entry).all()
