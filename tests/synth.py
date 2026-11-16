"""Synthetic 5-min NSE-like candles for tests (no network needed)."""
import numpy as np
import pandas as pd


def day_index(day):
    return pd.date_range(f"{day} 09:15", f"{day} 15:25", freq="5min")


def make(symbol_seed=0, days=30, start="2026-01-05", base=500.0, trend=0.0004):
    rng = np.random.default_rng(symbol_seed)
    frames, px = [], base
    for d in pd.bdate_range(start, periods=days):
        idx = day_index(d.date())
        drift = rng.choice([trend, -trend, 0.0]) + rng.normal(0, 0.0002)
        rets = rng.normal(drift, 0.0018, len(idx))
        close = px * np.exp(np.cumsum(rets))
        open_ = np.r_[px, close[:-1]] * (1 + rng.normal(0, 0.0003, len(idx)))
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0009, len(idx))))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0009, len(idx))))
        vol = rng.lognormal(10, 0.5, len(idx)) * np.linspace(2, 1, len(idx))
        frames.append(pd.DataFrame(dict(open=open_, high=high, low=low, close=close, volume=vol), index=idx))
        px = close[-1]
    return pd.concat(frames)
