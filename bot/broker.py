"""
Broker adapters with one interface, used by the live trader:
  place(symbol, side, qty, kind, trigger, limit, tag) -> order_id
  modify_sl(order_id, symbol, qty, trigger, limit)
  cancel(order_id, kind)
  orders() -> {order_id: {status, filled, avg, tag, text, kind}}
     status: open | trigger_pending | complete | rejected | cancelled
  find_tag(tag) -> order_id | None      (duplicate-order protection after restarts)
  net_qty() -> {symbol: qty}            (intraday positions)
  ltp(symbol), spread_pct(symbol), tick(symbol)
"""
import json
import logging
import math
import os
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)
SCRIP_MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
STATUS = {"complete": "complete", "rejected": "rejected", "cancelled": "cancelled", "trigger pending": "trigger_pending"}


def round_down(px, tick):
    return round(math.floor(px / tick + 1e-9) * tick, 2)


class AngelOneBroker:
    """Env: ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN, ANGEL_TOTP_SECRET"""
    name = "angel"

    def __init__(self, cache_dir="state"):
        from SmartApi import SmartConnect
        import pyotp
        self.smart = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
        totp = pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now()
        s = self.smart.generateSession(os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_MPIN"], totp)
        if not s or not s.get("status"):
            raise RuntimeError(f"Angel login failed: {(s or {}).get('message')}")
        self.scrips = self._load_scrips(Path(cache_dir) / "scrip_master_nse.json")
        log.info("Angel One logged in")

    @staticmethod
    def _load_scrips(path: Path) -> dict:
        fresh = path.exists() and time.time() - path.stat().st_mtime < 86400
        if not fresh:
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = requests.get(SCRIP_MASTER, timeout=90).json()
            nse = {r["symbol"][:-3]: {"token": r["token"], "tick": float(r.get("tick_size") or 5) / 100}
                   for r in rows if r.get("exch_seg") == "NSE" and r["symbol"].endswith("-EQ")}
            path.write_text(json.dumps(nse))
        return json.loads(path.read_text())

    def token(self, sym):
        return self.scrips[sym]["token"]

    def tick(self, sym):
        return self.scrips[sym]["tick"] or 0.05

    def _base(self, sym, side, qty):
        return {"tradingsymbol": f"{sym}-EQ", "symboltoken": self.token(sym), "transactiontype": side,
                "exchange": "NSE", "producttype": "INTRADAY", "duration": "DAY", "quantity": str(int(qty))}

    def place(self, sym, side, qty, kind="MARKET", trigger=None, limit=None, tag=""):
        p = self._base(sym, side, qty)
        if kind == "MARKET":
            p.update(variety="NORMAL", ordertype="MARKET", price="0")
        else:
            p.update(variety="STOPLOSS", ordertype="STOPLOSS_LIMIT",
                     triggerprice=str(trigger), price=str(limit))
        if tag:
            p["ordertag"] = tag[:20]
        oid = self.smart.placeOrder(p)
        if not oid:
            raise RuntimeError(f"order not accepted: {p}")
        return str(oid)

    def modify_sl(self, oid, sym, qty, trigger, limit):
        p = self._base(sym, "SELL", qty)
        p.update(variety="STOPLOSS", orderid=oid, ordertype="STOPLOSS_LIMIT",
                 triggerprice=str(trigger), price=str(limit))
        res = self.smart.modifyOrder(p)
        if not res or not res.get("status", True):
            raise RuntimeError(f"modify failed: {res}")

    def cancel(self, oid, kind="SL"):
        self.smart.cancelOrder(oid, "STOPLOSS" if kind == "SL" else "NORMAL")

    def orders(self):
        book = (self.smart.orderBook() or {}).get("data") or []
        out = {}
        for o in book:
            st = STATUS.get(str(o.get("status", "")).lower(), "open")
            out[str(o["orderid"])] = dict(status=st, filled=int(float(o.get("filledshares") or 0)),
                                          avg=float(o.get("averageprice") or 0), tag=o.get("ordertag") or "",
                                          text=o.get("text") or "", symbol=o.get("tradingsymbol", "")[:-3])
        return out

    def find_tag(self, tag):
        for oid, o in self.orders().items():
            if o["tag"] == tag[:20] and o["status"] != "rejected":
                return oid
        return None

    def net_qty(self):
        data = (self.smart.position() or {}).get("data") or []
        return {p["tradingsymbol"][:-3]: int(float(p.get("netqty") or 0))
                for p in data if p.get("producttype") == "INTRADAY" and p["tradingsymbol"].endswith("-EQ")}

    def ltp(self, sym):
        return float(self.smart.ltpData("NSE", f"{sym}-EQ", self.token(sym))["data"]["ltp"])

    def spread_pct(self, sym):
        d = self.smart.getMarketData("FULL", {"NSE": [self.token(sym)]})["data"]["fetched"][0]["depth"]
        bid, ask = float(d["buy"][0]["price"]), float(d["sell"][0]["price"])
        return (ask - bid) / ((ask + bid) / 2) * 100 if bid and ask else 99.0


class PaperBroker:
    """Simulated broker. Fills from a live price function; SL-limit orders can gap through like real ones."""
    name = "paper"

    def __init__(self, price_fn, slippage_pct=0.03, tick_size=0.05, spread=0.02):
        self.price_fn, self.slip, self._tick, self._spread = price_fn, slippage_pct / 100, tick_size, spread
        self._orders, self._pos, self._n = {}, {}, 0
        self.smart = None

    def tick(self, sym):
        return self._tick

    def ltp(self, sym):
        return float(self.price_fn(sym))

    def spread_pct(self, sym):
        return self._spread

    def place(self, sym, side, qty, kind="MARKET", trigger=None, limit=None, tag=""):
        self._n += 1
        oid = f"P{self._n}"
        self._orders[oid] = dict(symbol=sym, side=side, qty=qty, kind=kind, trigger=trigger, limit=limit,
                                 tag=tag[:20], status="open" if kind == "MARKET" else "trigger_pending",
                                 filled=0, avg=0.0, text="")
        self._process()
        return oid

    def modify_sl(self, oid, sym, qty, trigger, limit):
        o = self._orders[oid]
        if o["status"] != "trigger_pending":
            raise RuntimeError(f"cannot modify {o['status']} order")
        o.update(trigger=trigger, limit=limit, qty=qty)

    def cancel(self, oid, kind="SL"):
        o = self._orders[oid]
        if o["status"] in ("open", "trigger_pending"):
            o["status"] = "cancelled"

    def _fill(self, o, px):
        o.update(status="complete", filled=o["qty"], avg=round(px, 2))
        self._pos[o["symbol"]] = self._pos.get(o["symbol"], 0) + (o["qty"] if o["side"] == "BUY" else -o["qty"])

    def _process(self):
        for o in self._orders.values():
            if o["status"] not in ("open", "trigger_pending"):
                continue
            px = self.ltp(o["symbol"])
            if o["kind"] == "STUCK":
                continue
            if o["kind"] == "MARKET":
                self._fill(o, px * (1 + self.slip if o["side"] == "BUY" else 1 - self.slip))
            elif px <= o["trigger"] and px >= o["limit"]:
                self._fill(o, min(o["trigger"], px) * (1 - self.slip))

    def orders(self):
        self._process()
        return {k: dict(status=v["status"], filled=v["filled"], avg=v["avg"], tag=v["tag"], text=v["text"],
                        symbol=v["symbol"]) for k, v in self._orders.items()}

    def find_tag(self, tag):
        return next((k for k, v in self._orders.items() if v["tag"] == tag[:20] and v["status"] != "rejected"), None)

    def net_qty(self):
        self._process()
        return dict(self._pos)
