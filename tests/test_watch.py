import pandas as pd

from bot.watch import message, scan, snapshot
from tests.synth import make


def _flat(day_closes, start="2026-02-02"):
    frames = []
    for d, c in zip(pd.bdate_range(start, periods=len(day_closes)), day_closes):
        idx = pd.date_range(f"{d.date()} 09:15", f"{d.date()} 10:00", freq="5min")
        c = float(c)
        frames.append(pd.DataFrame(dict(open=c, high=c * 1.001, low=c * 0.999, close=c, volume=1000.0), index=idx))
    return pd.concat(frames)


def test_scan_lists_only_stronger_than_nifty_and_above_vwap():
    nifty = _flat([100, 99.5])                       # Nifty -0.5% today
    strong = _flat([50, 50])
    day2 = strong.index.date == strong.index[-1].date()
    strong.loc[day2, "close"] = [50.0] * 5 + [50.2, 50.3, 50.4, 50.5, 50.6]   # +1.2%, rising above VWAP
    strong.loc[day2, "high"] = strong.loc[day2, "close"] * 1.001
    weak = _flat([80, 79.6])                         # -0.5%, same as Nifty
    n, rows = scan({"PNB": strong, "WEAK": weak}, nifty, min_rs_pct=1.0)
    assert [r["sym"] for r in rows] == ["PNB"]
    assert round(rows[0]["rs"], 2) == 1.7 and rows[0]["above_vwap"]
    msg = message(n, rows, 1.0)
    assert "PNB" in msg and "not buy calls" in msg and "WEAK" not in msg


def test_snapshot_needs_two_days():
    assert snapshot(_flat([100])) == {}
