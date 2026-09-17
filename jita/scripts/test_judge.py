"""
test_judge.py – sikrer at SQL-dommeren og Python-formlene (common.py) gir samme tall.

Setter inn en oppdiktet vare i en transaksjon, kjører jita.judge_rows(), regner det samme i Python,
krever likhet på 4 desimaler, og ruller tilbake. Kjør:  python test_judge.py
"""
from __future__ import annotations

import sys

from common import (Profile, days_to_fill, db, economics, fees, load_profile, log,
                    qty_recommendation, score, break_even, max_buy_price, min_net_per_unit)

TID = 999999901
BID, ASK = 10000.0, 13000.0
S2B, BFS, BFS_TRADES = 43.0, 480.0, 30


def python_side(p: Profile):
    broker, tax = fees(p)
    buy, sell, net, margin = economics(BID, ASK, broker, tax)
    qty = qty_recommendation(p, buy, S2B)
    dfb = days_to_fill(40, qty, S2B)      # 40 = bid_qty_1pct i testraden
    dfs = days_to_fill(120, qty, BFS)     # 120 = ask_qty_1pct
    sc = score(net, S2B, BFS, dfb + dfs, 0.7, qty)  # ingen historikk → nøytral hist_pos; posisjonsbasert score
    return dict(buy_price=buy, sell_price=sell, qty=qty, net_per_unit=net, margin=margin,
                expected_profit=qty * net, days_to_fill_buy=dfb, days_to_fill_sell=dfs,
                flow_ratio=BFS / max(S2B, 0.1), score=sc,
                broker=broker, tax=tax, break_even=break_even(broker, tax),
                max_buy_price=max_buy_price(p), min_net_per_unit=min_net_per_unit(p))


def main():
    conn = db()
    p = load_profile(conn)
    want = python_side(p)
    with conn.cursor() as cur:
        cur.execute("insert into jita.types (type_id, name, is_t1, is_meta, is_t2, is_faction, is_ship, is_excluded) "
                    "values (%s, 'Testvare I', true, false, false, false, false, false)", (TID,))
        cur.execute("insert into jita.type_hourly (type_id, snapshot_at, best_bid, best_ask, bid_top_qty, bid_orders_1pct, "
                    "bid_qty_1pct, ask_orders_1pct, ask_qty_1pct, ask_qty_3pct) values (%s, now() + interval '1 second', %s, %s, 27, 2, 40, 5, 120, 200)",
                    (TID, BID, ASK))
        cur.execute("insert into jita.type_flow_hourly (type_id, hour, resolution, bfs_qty, bfs_trades, s2b_qty, s2b_trades, hours_covered) "
                    "values (%s, date_trunc('hour', now()), 60, %s, %s, %s, 12, 24)", (TID, BFS, BFS_TRADES, S2B))
        cur.execute("select buy_price, sell_price, qty, net_per_unit, margin, expected_profit, days_to_fill_buy, "
                    "days_to_fill_sell, flow_ratio, score from jita.judge_rows((select to_jsonb(pr) from jita.profile pr where id = 1)) "
                    "where type_id = %s", (TID,))
        cols = [d.name for d in cur.description]
        got = dict(zip(cols, cur.fetchone()))
        cur.execute("select broker, tax, break_even, max_buy_price, min_net_per_unit from jita.profile_calc((select to_jsonb(pr) from jita.profile pr where id = 1))")
        got.update(dict(zip([d.name for d in cur.description], cur.fetchone())))
    conn.rollback()
    conn.close()

    bad = 0
    for k, v in want.items():
        g = float(got[k])
        ok = abs(g - float(v)) < 1e-4 or abs(g - float(v)) / max(abs(float(v)), 1e-9) < 1e-9
        log(f"{'OK ' if ok else 'FEIL'} {k:18s} python={float(v):.6f} sql={g:.6f}")
        bad += not ok
    if bad:
        log(f"{bad} avvik – SQL og Python er IKKE enige")
        sys.exit(1)
    log("alle tall like på 4 desimaler")


if __name__ == "__main__":
    main()
