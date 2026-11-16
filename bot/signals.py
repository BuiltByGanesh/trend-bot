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


def prepare(df: pd.DataFrame, s: dict) -> pd.DataFrame:
    """Add indicator columns to one symbol's 5-min OHLCV."""
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
    df["prev_close"] = df["close"].groupby(d).shift(1)
    df["close_time"] = df.index + bar
    return df


def market_filter(proxy: pd.DataFrame) -> pd.Series:
    """True where the Nifty proxy closed above its session VWAP."""
    return proxy["close"] > vwap(proxy)


def signal_mask(df: pd.DataFrame, mkt_ok: pd.Series, s: dict, vol_mult=None) -> pd.Series:
    w0, w1 = _t(s["entry_window"][0]), _t(s["entry_window"][1])
    ct = df["close_time"].dt.time
    fresh_breakout = (df["close"] > df["orh"]) & (df["prev_close"] <= df["orh"])
    trend = (df["close"] > df["vwap"]) & (df["ema_f"] > df["ema_s"])
    volume = df["volume"] > (vol_mult or s["volume_mult"]) * df["vol_avg"]
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


def trail(stop: float, highest: float, atr_now: float, s: dict) -> float:
    """Ratchet only upward."""
    return max(stop, highest - s["trail_stop_atr"] * atr_now)


def is_eod(ts, s: dict) -> bool:
    return ts.time() >= _t(s["eod_exit"])
