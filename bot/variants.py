"""
Variant search with an honest hold-out:
  1. run every variant on the full year
  2. rank ONLY on the first 70% of days (in-sample)
  3. judge the pick on the last 30% (out-of-sample) + stress tests it never saw
A variant passes only if it is still profitable after costs on unseen days and with doubled slippage.
"""
import copy
import itertools

import pandas as pd

from .backtest import Scenario, run, stats

GRID = dict(
    bar_minutes=[5, 15],
    stop=[("atr", 1.5), ("atr", 3.0), ("or_low", 3.0)],
    target_r=[0, 1.5, 2, 3],
    trail_stop_atr=[0, 3.0],
    market_min_move_pct=[0.0, 0.3],
)
MIN_IS_TRADES = 30


def variant_cfgs(cfg):
    yield "current_rules", copy.deepcopy(cfg)
    for bm, (mode, k), tr, trail, mm in itertools.product(*GRID.values()):
        c = copy.deepcopy(cfg)
        c["strategy"].update(bar_minutes=bm, stop_mode=mode, initial_stop_atr=k, target_r=tr,
                             trail_stop_atr=trail, market_min_move_pct=mm)
        name = f"{bm}m|{mode}{k:g}|T{tr:g}R|trail{trail:g}|mkt{mm:g}"
        yield name, c


def split(trades, daily, cut, cap):
    return (stats(trades[trades.date < cut], daily[daily.date < cut], cap),
            stats(trades[trades.date >= cut], daily[daily.date >= cut], cap))


def search(data, proxy, cfg, log=print):
    cap = cfg["risk"]["capital"]
    rows, cfgs, cut = [], {}, None
    for i, (name, c) in enumerate(variant_cfgs(cfg)):
        trades, daily = run(data, proxy, c)
        if cut is None:
            days = list(daily.date)
            cut = days[int(len(days) * (1 - cfg["backtest"]["out_of_sample_pct"] / 100))]
        ins, oos = split(trades, daily, cut, cap)
        full = stats(trades, daily, cap)
        rows.append(dict(variant=name, is_trades=ins["trades"], is_net=ins["net_pnl"], is_pf=ins["profit_factor"],
                         is_wr=ins["win_rate"], oos_trades=oos["trades"], oos_net=oos["net_pnl"],
                         oos_pf=oos["profit_factor"], oos_wr=oos["win_rate"], full_net=full["net_pnl"],
                         full_dd=full["max_dd_pct"], costs=full["costs"]))
        cfgs[name] = c
        if i % 10 == 0:
            log(f"variant {i}: {name} IS ₹{ins['net_pnl']:,.0f} OOS ₹{oos['net_pnl']:,.0f}")
    table = pd.DataFrame(rows)
    eligible = table[table.is_trades >= MIN_IS_TRADES].sort_values("is_net", ascending=False)
    pick = eligible.iloc[0].variant if len(eligible) else None

    verdict, stress = "NO VARIANT", {}
    if pick:
        c = cfgs[pick]
        for sc in [Scenario("slippage_x2", slip_mult=2), Scenario("entry_delayed_1_bar", entry_delay_bars=1)]:
            t2, d2 = run(data, proxy, c, sc)
            stress[sc.name] = stats(t2, d2, cap)
        r = table.set_index("variant").loc[pick]
        ok = (r.is_net > 0 and r.oos_net > 0 and r.oos_pf >= 1.2 and r.oos_trades >= 10
              and stress["slippage_x2"]["net_pnl"] > 0)
        verdict = "PASS" if ok else "FAIL"
    return table.sort_values("is_net", ascending=False), pick, verdict, stress, cut, cfgs
