"""
Signal logic shared by the backtester and the live bot (one source of truth).

All timestamps are naive IST candle START times; a 5-min candle stamped 09:30 closes at 09:35.
A signal needs ALL filters true on the SAME completed candle, and the breakout must be fresh:
this candle closes above the first-15-min high AND the previous candle closed at/below it.
So if price broke out earlier while another filter was off, the bot waits for a new breakout.
"""
from datetime import time, timedelta

import numpy as np
import pandas as pd

from .indicators import atr, ema, vwap


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """5-min candles -> N-min candles (aligned to 09:15)."""
    if minutes == 5 or df.empty:
        return df
    out = df.resample(f"{minutes}min", origin="start_day", offset="15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    return out[(out.index.time >= time(9, 15)) & (out.index.time < time(15, 30))]


def prepare(df: pd.DataFrame, s: dict) -> pd.DataFrame:
    """Add indicator columns to one symbol's OHLCV (already at s['bar_minutes'])."""
    df = df.sort_index().copy()
    bar = timedelta(minutes=s["bar_minutes"])
    d = df.index.date
    df["vwap"] = vwap(df)
    df["ema_f"] = ema(df["close"], s["ema_fast"])
    df["ema_s"] = ema(df["close"], s["ema_slow"])
    df["atr"] = atr(df, s["atr_period"])
    df["vol_avg"] = df["volume"].shift(1).rolling(s["volume_lookback"]).mean()
    or_end = _t(s["opening_range_end"])
    in_or = (df.index.time >= time(9, 15)) & ((df.index + bar).time <= or_end)
    orh = df["high"].where(in_or).groupby(d).transform("max")
    after_or = (df.index + bar).time > or_end
    df["orh"] = orh.where(after_or)
    df["orl"] = df["low"].where(in_or).groupby(d).transform("min").where(after_or)
    df["prev_close"] = df["close"].groupby(d).shift(1)
    df["close_time"] = df.index + bar
    return df


def market_filter(proxy: pd.DataFrame, min_move_pct=None) -> pd.Series:
    """True where the Nifty proxy is above its session VWAP; if min_move_pct is set, also up at least that much on the day."""
    ok = proxy["close"] > vwap(proxy)
    if min_move_pct is None:
        return ok
    day_open = proxy["open"].groupby(proxy.index.date).transform("first")
    return ok & ((proxy["close"] / day_open - 1) * 100 >= min_move_pct)


def day_change_pct(df: pd.DataFrame) -> pd.Series:
    """% change vs the previous day's last close (NaN on the first day)."""
    d = pd.Series(df.index.date, index=df.index)
    last = df["close"].groupby(df.index.date).last()
    prev = pd.Series(last.shift(1).to_numpy(), index=last.index)
    return (df["close"] / d.map(prev) - 1) * 100


def signal_mask(df: pd.DataFrame, mkt_ok: pd.Series, s: dict, vol_mult=None, proxy_chg: pd.Series = None) -> pd.Series:
    w0, w1 = _t(s["entry_window"][0]), _t(s["entry_window"][1])
    ct = df["close_time"].dt.time
    fresh_breakout = (df["close"] > df["orh"]) & (df["prev_close"] <= df["orh"])
    trend = (df["close"] > df["vwap"]) & (df["ema_f"] > df["ema_s"])
    volume = df["volume"] > (vol_mult or s["volume_mult"]) * df["vol_avg"]
    if s.get("market_mode", "vwap") == "rs":
        # relative strength: stock's day % change beats Nifty's by rs_min_pct (Nifty itself may be weak)
        nifty = proxy_chg.reindex(df.index).ffill()
        market = (day_change_pct(df) - nifty >= s.get("rs_min_pct", 0.5)).fillna(False)
    else:
        market = mkt_ok.reindex(df.index).fillna(False).astype(bool)
    window = (ct >= w0) & (ct <= w1)
    return (fresh_breakout & trend & volume & market & window).fillna(False)


def size_position(entry: float, stop: float, capital: float, open_risk: float, open_value: float, r: dict) -> int:
    """Shares to buy: limited by per-trade risk, remaining open-risk budget, position value and margin."""
    per_share = entry - stop
    if per_share <= 0 or entry <= 0:
        return 0
    risk_budget = capital * r["risk_per_trade_pct"] / 100
    open_budget = capital * r["max_open_risk_pct"] / 100 - open_risk
    budget = min(risk_budget, open_budget)
    if budget <= 0:
        return 0
    qty = int(budget // per_share)
    qty = min(qty, int(capital * r["max_position_value_pct"] / 100 // entry))
    qty = min(qty, int(max(0.0, capital * r["leverage"] - open_value) // entry))
    return max(qty, 0)


def initial_stop(entry: float, atr: float, orl: float, s: dict) -> float:
    """'atr': entry - k*ATR.  'or_low': just below the opening-range low (never tighter than k*ATR)."""
    atr_stop = entry - s["initial_stop_atr"] * atr
    if s.get("stop_mode", "atr") == "or_low" and orl == orl and orl:
        return min(orl - 0.1 * atr, atr_stop)
    return atr_stop


def target_price(entry: float, stop: float, s: dict) -> float:
    r = s.get("target_r", 0) or 0
    return entry + r * (entry - stop) if r > 0 else 0.0


def trail(stop: float, highest: float, atr_now: float, s: dict) -> float:
    """Ratchet only upward. trail_stop_atr 0 = no trailing (stop stays put)."""
    k = s.get("trail_stop_atr", 0) or 0
    return max(stop, highest - k * atr_now) if k > 0 else stop


def is_eod(ts, s: dict) -> bool:
    return ts.time() >= _t(s["eod_exit"])
