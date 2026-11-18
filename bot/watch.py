"""
"Stronger than Nifty" watchlist – information only, no buy calls.

For each stock, on completed 5-min candles so far today:
  day %         vs previous day's close
  vs Nifty      day % minus NIFTYBEES day %
  VWAP          above / below today's VWAP
  volume        today's volume so far vs the average of the previous days up to the same time
  from high     how far below today's high it is
A stock is listed if it beats Nifty by at least min_rs_pct and is above VWAP.
"""
from datetime import time

import pandas as pd

from .indicators import vwap


def snapshot(df: pd.DataFrame) -> dict:
    """Stats for the latest day in df (5-min candles, naive IST)."""
    days = sorted(set(df.index.date))
    if len(days) < 2:
        return {}
    today = df[df.index.date == days[-1]]
    prev = df[df.index.date == days[-2]]
    if today.empty or prev.empty:
        return {}
    last = today.iloc[-1]
    now_t = today.index[-1].time()
    past = [df[(df.index.date == d) & (df.index.time <= now_t)]["volume"].sum() for d in days[:-1][-5:]]
    avg_vol = sum(past) / len(past) if past else 0
    vw = vwap(today).iloc[-1]
    return dict(price=float(last.close), chg=(last.close / prev.close.iloc[-1] - 1) * 100,
                above_vwap=bool(last.close > vw), vwap=float(vw), day_high=float(today.high.max()),
                from_high=(today.high.max() / last.close - 1) * 100,
                vol_x=float(today.volume.sum() / avg_vol) if avg_vol else float("nan"),
                as_of=today.index[-1] + pd.Timedelta(minutes=5))


def scan(frames: dict, proxy: pd.DataFrame, min_rs_pct=1.0, top=8):
    n = snapshot(proxy)
    if not n:
        return None, []
    rows = []
    for sym, df in frames.items():
        s = snapshot(df)
        if not s or s["as_of"].date() != n["as_of"].date():
            continue
        s["sym"], s["rs"] = sym, s["chg"] - n["chg"]
        if s["rs"] >= min_rs_pct and s["above_vwap"]:
            rows.append(s)
    rows.sort(key=lambda r: r["rs"], reverse=True)
    return n, rows[:top]


def message(n, rows, min_rs_pct):
    head = (f"👀 <b>Stronger than Nifty – {n['as_of']:%H:%M}</b>\n"
            f"Nifty (NIFTYBEES) {n['chg']:+.2f}% {'above' if n['above_vwap'] else 'below'} VWAP\n"
            f"<i>Watchlist only – not buy calls</i>\n")
    if not rows:
        return head + f"No stock is {min_rs_pct:g}%+ stronger than Nifty and above VWAP right now."
    lines = [f"• <b>{r['sym']}</b> ₹{r['price']:.2f} {r['chg']:+.2f}% (vs Nifty {r['rs']:+.2f}) | "
             f"vol {r['vol_x']:.1f}× | {r['from_high']:.1f}% below high" for r in rows]
    return head + "\n".join(lines)
