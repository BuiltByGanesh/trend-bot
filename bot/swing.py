"""
Swing trading (2-15 day holds) on DAILY candles, delivery (CNC) costs.

Three pre-defined setups (small grids, chosen BEFORE looking at results):
  A pullback : close > SMA200, SMA50 > SMA200, RSI(2) < x          -> exit close > SMA5 / max hold / 2.5 ATR stop
  B breakout : close > prior N-day high close, > SMA200, vol > 1.5x, beats Nifty 20d -> trailing ATR stop / max 15d
  C ema      : 9 EMA crosses above 21, close > 50 EMA, beats Nifty 20d -> 9 < 21 cross / 2 ATR stop / optional target
Signals on day t's close -> buy at day t+1 open. Stops are live during the day (low <= stop).
"""
import copy

import numpy as np
import pandas as pd

from .indicators import atr, ema


def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def delivery_cost(value: float, side: str, c: dict) -> float:
    brok = max(c["brokerage_min"], min(c["brokerage_flat"], value * c["brokerage_pct"] / 100))
    txn, sebi = value * c["exchange_txn_pct"] / 100, value * c["sebi_pct"] / 100
    gst = (brok + txn + sebi) * c["gst_pct"] / 100
    stt = value * c["stt_pct"] / 100
    stamp = value * c["stamp_buy_pct"] / 100 if side == "BUY" else 0.0
    dp = c["dp_charge"] * (1 + c["gst_pct"] / 100) if side == "SELL" else 0.0
    return brok + txn + sebi + gst + stt + stamp + dp


def prepare(df: pd.DataFrame, nifty_ret20: pd.Series) -> pd.DataFrame:
    d = df.copy()
    c = d["close"]
    d["sma5"], d["sma50"], d["sma200"] = c.rolling(5).mean(), c.rolling(50).mean(), c.rolling(200).mean()
    d["ema9"], d["ema21"], d["ema50"] = ema(c, 9), ema(c, 21), ema(c, 50)
    d["rsi2"] = rsi(c, 2)
    d["atr"] = atr(d, 14)
    d["vol20"] = d["volume"].shift(1).rolling(20).mean()
    d["hi20"], d["hi55"] = c.shift(1).rolling(20).max(), c.shift(1).rolling(55).max()
    d["ret20"] = c.pct_change(20) * 100
    d["rs20"] = d["ret20"] - nifty_ret20.reindex(d.index).ffill()
    return d


def entry_mask(d: pd.DataFrame, p: dict) -> pd.Series:
    if p["setup"] == "pullback":
        m = (d.close > d.sma200) & (d.sma50 > d.sma200) & (d.rsi2 < p["rsi_max"])
    elif p["setup"] == "breakout":
        hi = d[f"hi{p['lookback']}"]
        m = (d.close > hi) & (d.close > d.sma200) & (d.volume > 1.5 * d.vol20) & (d.rs20 > 0)
    else:  # ema cross
        cross = (d.ema9 > d.ema21) & (d.ema9.shift(1) <= d.ema21.shift(1))
        m = cross & (d.close > d.ema50) & (d.rs20 > 0)
    return m.fillna(False)


def exit_on_close(row, pos, p) -> str:
    """Rule exits decided on the close (executed at next open)."""
    if p["setup"] == "pullback" and row.close > row.sma5:
        return "CLOSE_ABOVE_SMA5"
    if p["setup"] == "ema" and row.ema9 < row.ema21:
        return "EMA_CROSS_DOWN"
    if pos["held"] >= p["max_hold"]:
        return "MAX_HOLD"
    return ""


VARIANTS = (
    [dict(setup="pullback", rsi_max=r, max_hold=h, stop_atr=2.5, trail_atr=0, target_r=0)
     for r in (5, 10) for h in (7, 10)]
    + [dict(setup="breakout", lookback=lb, max_hold=15, stop_atr=2.0, trail_atr=t, target_r=0)
       for lb in (20, 55) for t in (2.5, 3.5)]
    + [dict(setup="ema", max_hold=20, stop_atr=2.0, trail_atr=0, target_r=t) for t in (0, 2)]
)


def name(p):
    if p["setup"] == "pullback":
        return f"A pullback RSI2<{p['rsi_max']} hold≤{p['max_hold']}d"
    if p["setup"] == "breakout":
        return f"B breakout {p['lookback']}d trail{p['trail_atr']}ATR"
    return f"C ema9/21" + (f" target{p['target_r']}R" if p["target_r"] else "")


def run(data: dict, nifty: pd.DataFrame, p: dict, cfg: dict, slip_mult=1.0, cost_mult=1.0):
    r, c = cfg["swing"]["risk"], cfg["swing"]["costs"]
    slip = c["slippage_pct"] / 100 * slip_mult
    stop_slip = (c["slippage_pct"] + c["stop_slippage_pct"]) / 100 * slip_mult
    cost = lambda v, side: delivery_cost(v, side, c) * cost_mult
    n20 = nifty["close"].pct_change(20) * 100
    prep = {s: prepare(df, n20) for s, df in data.items() if len(df) > 220}
    masks = {s: entry_mask(d, p).to_numpy() for s, d in prep.items()}
    idx = {s: {t: i for i, t in enumerate(d.index)} for s, d in prep.items()}
    dates = sorted(set().union(*[set(d.index) for d in prep.values()]))

    cash, pos, pending_in, pending_out, trades, equity = float(r["capital"]), {}, [], {}, [], []
    for t in dates:
        # 1. exits decided yesterday, filled at today's open
        for s, why in list(pending_out.items()):
            if s in pos and t in idx[s]:
                row = prep[s].iloc[idx[s][t]]
                _close(s, pos, row.open * (1 - slip), why, t, trades, cost)
                cash += trades[-1]["proceeds"]
            pending_out.pop(s, None)
        # 2. entries signalled yesterday, filled at today's open
        for s, sig_atr, rank in pending_in:
            if s in pos or t not in idx[s] or len(pos) >= r["max_positions"]:
                continue
            row = prep[s].iloc[idx[s][t]]
            fill = row.open * (1 + slip)
            stop = fill - p["stop_atr"] * sig_atr
            eq = cash + sum(q["qty"] * q["last"] for q in pos.values())
            qty = int(min(eq * r["risk_per_trade_pct"] / 100 / (fill - stop),
                          eq * r["max_position_pct"] / 100 / fill,
                          (cash - 50) / fill)) if fill > stop else 0
            if qty < 1:
                continue
            ec = cost(fill * qty, "BUY")
            cash -= fill * qty + ec
            pos[s] = dict(qty=qty, entry=fill, stop=stop, init_stop=stop, high=fill, last=fill, t=t, held=0,
                          entry_cost=ec, target=fill + p["target_r"] * (fill - stop) if p["target_r"] else 0)
        pending_in = []
        # 3. during the day: stop / target
        for s in list(pos):
            if t not in idx[s]:
                continue
            row, q = prep[s].iloc[idx[s][t]], pos[s]
            if row.low <= q["stop"]:
                _close(s, pos, min(q["stop"], row.open) * (1 - stop_slip),
                       "TRAIL_STOP" if q["stop"] > q["entry"] else "STOP", t, trades, cost)
                cash += trades[-1]["proceeds"]
            elif q["target"] and row.high >= q["target"]:
                _close(s, pos, max(q["target"], row.open), "TARGET", t, trades, cost)
                cash += trades[-1]["proceeds"]
        # 4. at the close: trail, rule exits, new signals
        for s, q in pos.items():
            if t not in idx[s]:
                continue
            row = prep[s].iloc[idx[s][t]]
            q["held"] += 1
            q["last"], q["high"] = row.close, max(q["high"], row.high)
            if p["trail_atr"]:
                q["stop"] = max(q["stop"], q["high"] - p["trail_atr"] * row.atr)
            why = exit_on_close(row, q, p)
            if why:
                pending_out[s] = why
        cands = []
        for s, d in prep.items():
            if s in pos or t not in idx[s]:
                continue
            i = idx[s][t]
            if masks[s][i] and i + 1 < len(d):
                row = d.iloc[i]
                cands.append((s, float(row.atr), float(row.rs20) if row.rs20 == row.rs20 else -99))
        cands.sort(key=lambda x: x[2], reverse=True)          # strongest vs Nifty first
        pending_in = cands[: max(0, r["max_positions"] - len(pos))]
        equity.append((t, cash + sum(q["qty"] * q["last"] for q in pos.values())))
    for s in list(pos):  # mark open positions at last close
        q = pos[s]
        _close(s, pos, q["last"] * (1 - slip), "OPEN_AT_END", dates[-1], trades, cost)
        cash += trades[-1]["proceeds"]
    eq = pd.Series(dict(equity))
    return pd.DataFrame(trades), eq


def _close(s, pos, px, why, t, trades, cost):
    q = pos.pop(s)
    xc = cost(px * q["qty"], "SELL")
    net = (px - q["entry"]) * q["qty"] - q["entry_cost"] - xc
    trades.append(dict(symbol=s, entry_date=str(q["t"].date()), entry=round(q["entry"], 2), exit_date=str(t.date()),
                       exit=round(px, 2), qty=q["qty"], days=q["held"], reason=why, costs=round(q["entry_cost"] + xc, 2),
                       pnl=round(net, 2), ret_pct=round((px / q["entry"] - 1) * 100, 2),
                       proceeds=px * q["qty"] - xc))


def stats(tr: pd.DataFrame, eq: pd.Series, start_cap: float) -> dict:
    if eq.empty:
        return {}
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 0.01)
    end = eq.iloc[-1]
    base = eq.iloc[0] if len(eq) else start_cap
    dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    w, l = (tr[tr.pnl > 0], tr[tr.pnl <= 0]) if len(tr) else (tr, tr)
    return dict(trades=len(tr), net=round(end - base, 0), cagr=round(((end / base) ** (1 / yrs) - 1) * 100, 2),
                win_rate=round(len(w) / len(tr) * 100, 1) if len(tr) else 0,
                pf=round(w.pnl.sum() / abs(l.pnl.sum()), 2) if len(l) and l.pnl.sum() else 0,
                avg_days=round(tr.days.mean(), 1) if len(tr) else 0, max_dd=round(dd, 2),
                costs=round(tr.costs.sum(), 0) if len(tr) else 0)


def bench(nifty: pd.DataFrame, a, b) -> dict:
    n = nifty.loc[a:b, "close"]
    yrs = max((n.index[-1] - n.index[0]).days / 365.25, 0.01)
    dd = ((n - n.cummax()) / n.cummax()).min() * 100
    return dict(cagr=round(((n.iloc[-1] / n.iloc[0]) ** (1 / yrs) - 1) * 100, 2), max_dd=round(dd, 2))


def search(data, nifty, cfg, log=print):
    cap = cfg["swing"]["risk"]["capital"]
    dates = sorted(set().union(*[set(d.index) for d in data.values()]))
    dates = [d for d in dates if d >= dates[0] + pd.Timedelta(days=300)]   # after SMA200 warm-up
    cut = dates[int(len(dates) * (1 - cfg["swing"]["out_of_sample_pct"] / 100))]
    rows, runs = [], {}
    for p in VARIANTS:
        tr, eq = run(data, nifty, p, cfg)
        eq = eq[eq.index >= dates[0]]
        tr = tr[pd.to_datetime(tr.entry_date) >= dates[0]] if len(tr) else tr
        ins = stats(tr[pd.to_datetime(tr.entry_date) < cut] if len(tr) else tr, eq[eq.index < cut], cap)
        oos = stats(tr[pd.to_datetime(tr.entry_date) >= cut] if len(tr) else tr, eq[eq.index >= cut], cap)
        full = stats(tr, eq, cap)
        runs[name(p)] = (p, tr, eq)
        rows.append({"setup": name(p), **{f"is_{k}": v for k, v in ins.items()},
                     **{f"oos_{k}": v for k, v in oos.items()}, "full_cagr": full.get("cagr"), "full_dd": full.get("max_dd")})
        log(f"{name(p)}: IS {ins.get('cagr')}%/yr OOS {oos.get('cagr')}%/yr")
    table = pd.DataFrame(rows).sort_values("is_cagr", ascending=False)
    pick = table.iloc[0].setup
    p = runs[pick][0]
    stress = {}
    for nm, kw in [("slippage_x2", dict(slip_mult=2)), ("costs_x1.5", dict(cost_mult=1.5))]:
        tr, eq = run(data, nifty, p, cfg, **kw)
        eq = eq[eq.index >= cut]
        stress[nm] = stats(tr[pd.to_datetime(tr.entry_date) >= cut] if len(tr) else tr, eq, cap)
    b_is, b_oos = bench(nifty, dates[0], cut), bench(nifty, cut, dates[-1])
    r = table.set_index("setup").loc[pick]
    passed = (r.is_cagr > 0 and r.oos_cagr > 0 and r.oos_pf >= 1.2 and r.oos_trades >= 20
              and stress["slippage_x2"].get("cagr", -1) > 0)
    yearly = None
    tr, eq = runs[pick][1], runs[pick][2]
    if len(eq):
        yearly = eq.groupby(eq.index.year).agg(["first", "last"])
        yearly = ((yearly["last"] / yearly["first"] - 1) * 100).round(2)
    return table, pick, "PASS" if passed else "FAIL", stress, cut, (b_is, b_oos), yearly, runs[pick][1]
