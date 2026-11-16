"""Angel One equity-intraday charges for one executed order."""


def order_cost(value: float, side: str, c: dict) -> float:
    brokerage = max(c["brokerage_min"], min(c["brokerage_flat"], value * c["brokerage_pct"] / 100))
    brokerage = min(brokerage, value)  # never more than the trade itself
    txn = value * c["exchange_txn_pct"] / 100
    sebi = value * c["sebi_pct"] / 100
    gst = (brokerage + txn + sebi) * c["gst_pct"] / 100
    stt = value * c["stt_sell_pct"] / 100 if side == "SELL" else 0.0
    stamp = value * c["stamp_buy_pct"] / 100 if side == "BUY" else 0.0
    return brokerage + txn + sebi + gst + stt + stamp
