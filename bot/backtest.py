"""
Portfolio backtest on completed 5-min candles, using the same signal and risk code as the live bot.

Event order inside each candle:
  1. pending entries (signalled on the previous candle's close) fill at this candle's OPEN + slippage
  2. at or after eod_exit, every position exits at the OPEN
  3. protective stop is live inside the candle: low <= stop -> fill at min(stop, open) - stop slippage
  4. when the candle completes: trail the stop (move up only), check the daily loss limit, look for new signals
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import signals as sg
from .costs import order_cost
from .risk import DayRisk


@dataclass
class Scenario:
    name: str = "base"
    slip_mult: float = 1.0
    cost_mult: float = 1.0
    entry_delay_bars: int = 0      # simulates delayed data / late orders


def run(data: dict, proxy: pd.DataFrame, cfg: dict, sc: Scenario = Scenario()):
    s, r, c = cfg["strategy"], cfg["risk"], cfg["costs"]
    slip = c["slippage_pct"] / 100 * sc.slip_mult
    stop_slip = (c["slippage_pct"] + c["stop_slippage_pct"]) / 100 * sc.slip_mult
    cost = lambda v, side: order_cost(v, side, c) * sc.cost_mult

    bm = s["bar_minutes"]
    proxy = sg.resample(proxy, bm)
    mkt_ok = sg.market_filter(proxy, s.get("market_min_move_pct"))
    proxy_chg = sg.day_change_pct(proxy)
    rows, sig = {}, {}
    for sym, df in data.items():
        p = sg.prepare(sg.resample(df, bm), s)
        sig[sym] = sg.signal_mask(p, mkt_ok, s, proxy_chg=proxy_chg)
        rows[sym] = p
    days = sorted(set().union(*[set(p.index.date) for p in rows.values()]))

    trades, daily = [], []
    capital = r["capital"]
    for day in days:
        bars = {}
        for sym, p in rows.items():
            dp = p[p.index.date == day]
            if len(dp):
                bars[sym] = (dp, sig[sym][dp.index].to_numpy())
        if not bars:
            continue
        risk = DayRisk(capital, r)
        pos, pending = {}, {}
        stamps = sorted(set().union(*[set(dp.index) for dp, _ in bars.values()]))
        pos_idx = {sym: {t: i for i, t in enumerate(dp.index)} for sym, (dp, _) in bars.items()}
        day_trades = []

        def close_pos(sym, px, reason, t):
            p = pos.pop(sym)
            exit_cost = cost(px * p["qty"], "SELL")
            net = (px - p["entry"]) * p["qty"] - p["entry_cost"] - exit_cost
            risk.record_exit(net)
            day_trades.append(dict(date=str(day), symbol=sym, entry_time=p["t"].strftime("%H:%M"),
                                   entry=round(p["entry"], 2), exit_time=t.strftime("%H:%M"), exit=round(px, 2),
                                   qty=p["qty"], reason=reason, init_stop=round(p["init_stop"], 2),
                                   costs=round(p["entry_cost"] + exit_cost, 2), pnl=round(net, 2),
                                   r_mult=round(net / p["risk_rs"], 2) if p["risk_rs"] else 0.0, nth_entry=p["nth"]))

        for t in stamps:
            # 1. fill pending entries at the open
            for sym in list(pending):
                if t not in pos_idx[sym] or pos_idx[sym][t] < pending[sym]["fill_idx"]:
                    continue
                pe = pending.pop(sym)
                if risk.flatten or sg.is_eod(t, s):
                    continue
                row = bars[sym][0].loc[t]
                fill = row.open * (1 + slip)
                stop = sg.initial_stop(fill, pe["atr"], pe["orl"], s)
                if stop >= fill:
                    continue
                ec = cost(fill * pe["qty"], "BUY")
                pos[sym] = dict(qty=pe["qty"], entry=fill, stop=stop, init_stop=stop, highest=fill, t=t,
                                entry_cost=ec, risk_rs=(fill - stop) * pe["qty"], nth=pe["nth"], last=fill,
                                target=sg.target_price(fill, stop, s))

            # 2. end-of-day exit
            if sg.is_eod(t, s):
                for sym in list(pos):
                    if t in pos_idx[sym]:
                        close_pos(sym, bars[sym][0].loc[t].open * (1 - slip), "EOD", t)
                pending.clear()
                continue

            # 3. protective stop inside the candle
            for sym in list(pos):
                if t not in pos_idx[sym]:
                    continue
                row = bars[sym][0].loc[t]
                p = pos[sym]
                if row.low <= p["stop"]:   # stop checked first (conservative when both touch)
                    px = min(p["stop"], row.open) * (1 - stop_slip)
                    close_pos(sym, px, "TRAIL_STOP" if p["stop"] > p["entry"] else "STOP", t)
                elif p["target"] and row.high >= p["target"]:   # limit sell at target
                    close_pos(sym, max(p["target"], row.open), "TARGET", t)

            # 4a. candle complete: trail stops, mark to market
            unreal = 0.0
            for sym, p in pos.items():
                if t in pos_idx[sym]:
                    row = bars[sym][0].loc[t]
                    p["highest"] = max(p["highest"], row.high)
                    p["stop"] = sg.trail(p["stop"], p["highest"], row.atr, s)
                    p["last"] = row.close
                unreal += (p["last"] - p["entry"]) * p["qty"] - p["entry_cost"] - cost(p["last"] * p["qty"], "SELL")

            # 4b. daily loss limit (realised + unrealised + costs) -> flatten, stop for the day
            if risk.check_loss(unreal):
                for sym in list(pos):
                    close_pos(sym, pos[sym]["last"] * (1 - slip), "DAILY_LOSS_LIMIT", t)
                pending.clear()

            # 4c. new signals on this completed candle
            for sym, (dp, mask) in bars.items():
                if t not in pos_idx[sym] or sym in pos or sym in pending:
                    continue
                i = pos_idx[sym][t]
                if not mask[i] or risk.can_enter(sym, s):
                    continue
                row = dp.iloc[i]
                est_stop = sg.initial_stop(row.close, row.atr, row.orl, s)
                open_risk = sum(max(0.0, (p["entry"] - p["stop"]) * p["qty"]) for p in pos.values()) + \
                    sum(q["risk"] for q in pending.values())
                open_val = sum(p["entry"] * p["qty"] for p in pos.values()) + sum(q["val"] for q in pending.values())
                qty = sg.size_position(row.close, est_stop, capital, open_risk, open_val, r)
                if qty < 1:
                    continue
                risk.record_entry(sym)
                pending[sym] = dict(qty=qty, atr=row.atr, orl=row.orl, fill_idx=i + 1 + sc.entry_delay_bars,
                                    risk=(row.close - est_stop) * qty, val=row.close * qty, nth=risk.per_stock[sym])

        for sym in list(pos):  # data ended before the EOD candle
            close_pos(sym, pos[sym]["last"] * (1 - slip), "EOD_NO_DATA", stamps[-1])
        day_pnl = sum(x["pnl"] for x in day_trades)
        trades += day_trades
        daily.append(dict(date=str(day), pnl=round(day_pnl, 2), trades=len(day_trades), halted=risk.halted))
        capital += day_pnl
    cols = ["date", "symbol", "entry_time", "entry", "exit_time", "exit", "qty", "reason", "init_stop",
            "costs", "pnl", "r_mult", "nth_entry"]
    return pd.DataFrame(trades, columns=cols), pd.DataFrame(daily, columns=["date", "pnl", "trades", "halted"])


def day_regimes(proxy: pd.DataFrame, threshold: float) -> pd.Series:
    """'sideways' if the market proxy's day had low efficiency |close-open|/(high-low)."""
    g = proxy.groupby(proxy.index.date)
    eff = (g["close"].last() - g["open"].first()).abs() / (g["high"].max() - g["low"].min()).replace(0, np.nan)
    return pd.Series(np.where(eff < threshold, "sideways", "trending"), index=[str(d) for d in eff.index])


def stats(trades: pd.DataFrame, daily: pd.DataFrame, capital: float) -> dict:
    if trades.empty:
        return dict(trades=0, net_pnl=0.0, return_pct=0.0, win_rate=0.0, profit_factor=0.0,
                    avg_r=0.0, max_dd_pct=0.0, costs=0.0, days=len(daily), halted_days=0)
    w, l = trades[trades.pnl > 0], trades[trades.pnl <= 0]
    eq = capital + daily.pnl.cumsum()
    dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    return dict(trades=len(trades), net_pnl=round(trades.pnl.sum(), 0),
                return_pct=round(trades.pnl.sum() / capital * 100, 2),
                win_rate=round(len(w) / len(trades) * 100, 1),
                profit_factor=round(w.pnl.sum() / abs(l.pnl.sum()), 2) if len(l) and l.pnl.sum() else float("inf"),
                avg_r=round(trades.r_mult.mean(), 2), max_dd_pct=round(min(dd, 0.0), 2),
                costs=round(trades.costs.sum(), 0), days=len(daily), halted_days=int((daily.halted != "").sum()))


def full_report(data, proxy, cfg):
    """Base run + in/out-of-sample split + sideways vs trending days + stress scenarios."""
    cap, b = cfg["risk"]["capital"], cfg["backtest"]
    trades, daily = run(data, proxy, cfg)
    out = {"base": stats(trades, daily, cap)}

    days = list(daily.date)
    cut = days[int(len(days) * (1 - b["out_of_sample_pct"] / 100))] if len(days) > 3 else None
    if cut:
        out["in_sample"] = stats(trades[trades.date < cut], daily[daily.date < cut], cap)
        out["out_of_sample"] = stats(trades[trades.date >= cut], daily[daily.date >= cut], cap)

    reg = day_regimes(proxy, b["sideways_efficiency"])
    for name in ("sideways", "trending"):
        dset = set(reg[reg == name].index)
        out[f"{name}_days"] = stats(trades[trades.date.isin(dset)], daily[daily.date.isin(dset)], cap)

    for sc in [Scenario("no_costs_no_slippage", slip_mult=0, cost_mult=0), Scenario("slippage_x2", slip_mult=2), Scenario("slippage_x3", slip_mult=3),
               Scenario("costs_x1.5", cost_mult=1.5), Scenario("entry_delayed_1_bar", entry_delay_bars=1)]:
        t2, d2 = run(data, proxy, cfg, sc)
        out[sc.name] = stats(t2, d2, cap)

    by_sym = trades.groupby("symbol").pnl.agg(["count", "sum"]).round(0).reset_index() if len(trades) else pd.DataFrame()
    reasons = trades.reason.value_counts().to_dict() if len(trades) else {}
    return out, trades, daily, by_sym, reasons, cut
