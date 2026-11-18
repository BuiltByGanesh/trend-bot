import numpy as np
import pandas as pd
import pytest
import yaml

from bot import swing as sw

CFG = yaml.safe_load(open("config.yaml"))


def daily(seed, n=900, drift=0.0006, base=200.0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    c = base * np.exp(np.cumsum(rng.normal(drift, 0.016, n)))
    o = np.r_[base, c[:-1]] * (1 + rng.normal(0, 0.004, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.008, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.008, n)))
    v = rng.lognormal(13, 0.4, n)
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=v), index=idx)


@pytest.fixture(scope="module")
def uni():
    return {f"S{i}": daily(i, drift=0.0004 + 0.0002 * i) for i in range(8)}, daily(99, drift=0.0004)


def test_delivery_costs():
    buy, sell = sw.delivery_cost(50000, "BUY", CFG["swing"]["costs"]), sw.delivery_cost(50000, "SELL", CFG["swing"]["costs"])
    assert 78 < buy < 88      # STT 50 + brokerage 20 + stamp 7.5 + GST/fees
    assert 95 < sell < 105    # STT 50 + brokerage 20 + DP 23.6 + GST/fees


@pytest.mark.parametrize("p", sw.VARIANTS)
def test_every_setup_respects_limits(uni, p):
    data, nifty = uni
    tr, eq = sw.run(data, nifty, p, CFG)
    assert len(eq) > 500 and (eq > 0).all()
    if len(tr):
        # never more than max_positions open on any day
        opens = []
        for d in eq.index[::25]:
            ds = str(d.date())
            opens.append(((tr.entry_date <= ds) & (tr.exit_date > ds)).sum())
        assert max(opens) <= CFG["swing"]["risk"]["max_positions"]
        assert (tr.days <= p["max_hold"] + 1).all()
        assert set(tr.reason) <= {"STOP", "TRAIL_STOP", "TARGET", "CLOSE_ABOVE_SMA5", "EMA_CROSS_DOWN",
                                  "MAX_HOLD", "OPEN_AT_END"}


def test_search_report(uni):
    data, nifty = uni
    table, pick, verdict, stress, cut, (b_is, b_oos), yearly, trades = sw.search(data, nifty, CFG, log=lambda m: None)
    assert len(table) == len(sw.VARIANTS) and pick in set(table.setup)
    assert verdict in ("PASS", "FAIL") and "slippage_x2" in stress and "cagr" in b_oos
