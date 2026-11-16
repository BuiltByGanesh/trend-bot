"""5-minute candles: Angel One SmartAPI (long history), Yahoo fallback (~60 days)."""
import logging
import time as _time
from datetime import datetime, time, timedelta

import pandas as pd

log = logging.getLogger(__name__)
ANGEL_INTERVAL = {5: "FIVE_MINUTE", 15: "FIFTEEN_MINUTE"}


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
    df = df[~df.index.duplicated(keep="last")].sort_index().astype(float)
    t = df.index.time
    return df[(t >= time(9, 15)) & (t < time(15, 30))]


def yahoo(symbol: str, days: int = 59, minutes: int = 5) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(f"{symbol}.NS", period=f"{min(days, 59)}d", interval=f"{minutes}m",
                     progress=False, auto_adjust=False, multi_level_index=False)
    if df.empty:
        raise RuntimeError(f"Yahoo returned no data for {symbol}")
    idx = df.index.tz_convert("Asia/Kolkata") if df.index.tz is not None else df.index
    df.index = idx.tz_localize(None)
    return _clean(df)


class AngelData:
    def __init__(self, smart, token_of):
        self.smart, self.token_of = smart, token_of

    def candles(self, symbol: str, start: datetime, end: datetime, minutes: int = 5) -> pd.DataFrame:
        """Angel allows ~100 days of 5-min data per request, so fetch in chunks."""
        frames, cur = [], start
        while cur < end:
            nxt = min(cur + timedelta(days=90), end)
            for attempt in range(3):
                try:
                    res = self.smart.getCandleData({
                        "exchange": "NSE", "symboltoken": self.token_of(symbol),
                        "interval": ANGEL_INTERVAL[minutes],
                        "fromdate": cur.strftime("%Y-%m-%d %H:%M"), "todate": nxt.strftime("%Y-%m-%d %H:%M")})
                    break
                except Exception as e:
                    log.warning("Angel candles %s retry %d: %s", symbol, attempt + 1, e)
                    _time.sleep(1.5)
            else:
                raise RuntimeError(f"Angel candles failed for {symbol}")
            rows = (res or {}).get("data") or []
            if rows:
                f = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
                ts = pd.to_datetime(f.pop("time"))
                if ts.dt.tz is not None:
                    ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
                f.index = pd.DatetimeIndex(ts)
                frames.append(f)
            cur = nxt
            _time.sleep(0.4)  # stay under SmartAPI rate limits
        if not frames:
            raise RuntimeError(f"Angel returned no candles for {symbol}")
        return _clean(pd.concat(frames))


def history(symbols, days, source, angel=None, minutes=5) -> dict:
    end = datetime.now()
    start = end - timedelta(days=days)
    out = {}
    for sym in symbols:
        try:
            if source == "angel" and angel:
                out[sym] = angel.candles(sym, start, end, minutes)
            else:
                out[sym] = yahoo(sym, days, minutes)
        except Exception as e:
            log.warning("%s: %s – trying Yahoo", sym, e)
            try:
                out[sym] = yahoo(sym, days, minutes)
            except Exception as e2:
                log.error("%s: no data (%s)", sym, e2)
    return out
