import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP, resets every day."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    d = df.index.date
    pv = (tp * df["volume"]).groupby(d).cumsum()
    v = df["volume"].groupby(d).cumsum().replace(0, np.nan)
    return pv / v
