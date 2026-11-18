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
    def __init__(self, smart, token_of, pace=0.4):
        self.smart, self.token_of, self.pace = smart, token_of, pace

    def candles(self, symbol: str, start: datetime, end: datetime, minutes: int = 5) -> pd.DataFrame:
        """Angel allows ~100 days of 5-min data per request, so fetch in chunks."""
        frames, cur = [], start
        while cur < end:
            nxt = min(cur + timedelta(days=90), end)
            for attempt in range(8):
                try:
                    res = self.smart.getCandleData({
                        "exchange": "NSE", "symboltoken": self.token_of(symbol),
                        "interval": ANGEL_INTERVAL[minutes],
                        "fromdate": cur.strftime("%Y-%m-%d %H:%M"), "todate": nxt.strftime("%Y-%m-%d %H:%M")})
                    if isinstance(res, dict) and not res.get("status", True) and "rate" in str(res.get("message", "")).lower():
                        raise RuntimeError(res.get("message"))
                    break
                except Exception as e:
                    wait = 5 * (attempt + 1) if "rate" in str(e).lower() else 1.5
                    log.warning("Angel candles %s retry %d (wait %ss): %s", symbol, attempt + 1, wait, str(e)[:80])
                    _time.sleep(wait)
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
            _time.sleep(self.pace)  # stay under SmartAPI rate limits
        if not frames:
            raise RuntimeError(f"Angel returned no candles for {symbol}")
        return _clean(pd.concat(frames))


def history(symbols, days, source, angel=None, minutes=5, allow_fallback=True) -> dict:
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
            if not allow_fallback:
                log.error("%s: Angel failed (%s) – left out (no mixing with 59-day Yahoo data)", sym, e)
                continue
            log.warning("%s: %s – trying Yahoo", sym, e)
            try:
                out[sym] = yahoo(sym, days, minutes)
            except Exception as e2:
                log.error("%s: no data (%s)", sym, e2)
    return out


def _clean_daily(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Kolkata").tz_localize(None)
    df.index = idx.normalize()
    return df[~df.index.duplicated(keep="last")].sort_index().astype(float)


def yahoo_daily(symbol: str, years: int = 5) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(f"{symbol}.NS", period=f"{years}y", interval="1d", progress=False,
                     auto_adjust=True, multi_level_index=False)
    if df.empty:
        raise RuntimeError(f"Yahoo returned no daily data for {symbol}")
    return _clean_daily(df)


def daily_history(symbols, years, angel=None) -> dict:
    """Daily candles. Angel ONE_DAY first (2000 days per request); Yahoo (split-adjusted) as fallback."""
    end = datetime.now()
    start = end - timedelta(days=int(years * 365.25))
    out = {}
    for sym in symbols:
        try:
            if not angel:
                raise RuntimeError("no Angel login")
            frames, cur = [], start
            while cur < end:
                nxt = min(cur + timedelta(days=1900), end)
                for attempt in range(8):
                    try:
                        res = angel.smart.getCandleData({
                            "exchange": "NSE", "symboltoken": angel.token_of(sym), "interval": "ONE_DAY",
                            "fromdate": cur.strftime("%Y-%m-%d 09:15"), "todate": nxt.strftime("%Y-%m-%d 15:30")})
                        if isinstance(res, dict) and not res.get("status", True) and "rate" in str(res.get("message", "")).lower():
                            raise RuntimeError(res.get("message"))
                        break
                    except Exception as e:
                        _time.sleep(5 * (attempt + 1) if "rate" in str(e).lower() else 1.5)
                else:
                    raise RuntimeError("Angel daily failed")
                rows = (res or {}).get("data") or []
                if rows:
                    f = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
                    f.index = pd.to_datetime(f.pop("time"))
                    frames.append(f)
                cur = nxt
                _time.sleep(angel.pace)
            out[sym] = _clean_daily(pd.concat(frames))
        except Exception as e:
            log.warning("%s daily via Angel failed (%s) – Yahoo", sym, str(e)[:60])
            try:
                out[sym] = yahoo_daily(sym, years)
            except Exception as e2:
                log.error("%s: no daily data (%s)", sym, e2)
    return out
