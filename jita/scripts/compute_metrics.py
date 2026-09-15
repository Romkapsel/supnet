"""
compute_metrics.py – ordrebok-tall per vare fra et snapshot (spec del 4/5).

Kjøpsordrer telles hvis de DEKKER Jita 4-4 (stasjon / system / region / N hopp via jita.systems).
Salgsordrer bare fra Jita 4-4 (stasjonsbundne).

Brukes i minnet av ingest_orders.py; kan også kjøres alene på et lagret snapshot:
    python compute_metrics.py prev_snapshot.json.gz
"""
from __future__ import annotations

from collections import defaultdict

from common import JITA_44, JITA_SYSTEM

# Indekser i snapshot-radene
O_ID, O_TYPE, O_BUY, O_PRICE, O_VOL, O_ISSUED, O_DUR, O_LOC, O_SYS, O_RANGE = range(10)


def covers_jita(order, jumps: dict[int, int]) -> bool:
    """Dekker denne kjøpsordren Jita 4-4?"""
    rng = order[O_RANGE]
    if rng == "region":
        return True
    if rng == "station":
        return order[O_LOC] == JITA_44
    if rng == "solarsystem":
        return order[O_SYS] == JITA_SYSTEM
    try:
        n = int(rng)
    except (TypeError, ValueError):
        return False
    return jumps.get(order[O_SYS], 99) <= n


def split_book(orders, jumps) -> tuple[dict, dict]:
    """→ (buys_by_type, sells_by_type) med bare relevante ordrer."""
    buys, sells = defaultdict(list), defaultdict(list)
    for o in orders:
        if o[O_BUY]:
            if covers_jita(o, jumps):
                buys[o[O_TYPE]].append(o)
        elif o[O_LOC] == JITA_44:
            sells[o[O_TYPE]].append(o)
    return buys, sells


def best_prices(buys, sells) -> dict[int, tuple[float, float]]:
    """→ {type_id: (best_bid, best_ask)} for typer med ordrer på begge sider."""
    out = {}
    for t, bl in buys.items():
        sl = sells.get(t)
        if not sl:
            continue
        out[t] = (max(o[O_PRICE] for o in bl), min(o[O_PRICE] for o in sl))
    return out


def prefilter(best: dict, min_spread: float) -> set[int]:
    return {t for t, (bid, ask) in best.items() if bid > 0 and ask / bid - 1 >= min_spread}


def metrics_for(type_id: int, buys: list, sells: list) -> dict:
    bid = max(o[O_PRICE] for o in buys)
    ask = min(o[O_PRICE] for o in sells)
    bid_top_qty = sum(o[O_VOL] for o in buys if o[O_PRICE] == bid)
    b1 = [o for o in buys if o[O_PRICE] >= bid * 0.99]
    a1 = [o for o in sells if o[O_PRICE] <= ask * 1.01]
    a3 = [o for o in sells if o[O_PRICE] <= ask * 1.03]
    # «mur»: prisnivået med mest volum blant kjøpsordrer innenfor 5 % under toppen
    walls = defaultdict(int)
    for o in buys:
        if o[O_PRICE] >= bid * 0.95:
            walls[o[O_PRICE]] += o[O_VOL]
    wall_price, wall_qty = max(walls.items(), key=lambda kv: kv[1]) if walls else (None, None)
    return dict(
        type_id=type_id, best_bid=bid, best_ask=ask,
        bid_top_qty=bid_top_qty, bid_orders_1pct=len(b1), bid_qty_1pct=sum(o[O_VOL] for o in b1),
        ask_orders_1pct=len(a1), ask_qty_1pct=sum(o[O_VOL] for o in a1), ask_qty_3pct=sum(o[O_VOL] for o in a3),
        bid_floor_price=wall_price, bid_floor_qty=wall_qty,
    )


def compute(orders, jumps, min_spread: float):
    """→ (rows for type_hourly, prefilter-sett, buys, sells)"""
    buys, sells = split_book(orders, jumps)
    best = best_prices(buys, sells)
    keep = prefilter(best, min_spread)
    rows = [metrics_for(t, buys[t], sells[t]) for t in keep]
    return rows, keep, buys, sells


def write_type_hourly(conn, rows, snapshot_at):
    with conn.cursor() as cur:
        cur.execute("delete from jita.type_hourly where snapshot_at = %s", (snapshot_at,))
        with cur.copy("copy jita.type_hourly (type_id, snapshot_at, best_bid, best_ask, bid_top_qty, bid_orders_1pct, "
                      "bid_qty_1pct, ask_orders_1pct, ask_qty_1pct, ask_qty_3pct, bid_floor_price, bid_floor_qty) "
                      "from stdin") as cp:
            for r in rows:
                cp.write_row((r["type_id"], snapshot_at, r["best_bid"], r["best_ask"], r["bid_top_qty"],
                              r["bid_orders_1pct"], r["bid_qty_1pct"], r["ask_orders_1pct"], r["ask_qty_1pct"],
                              r["ask_qty_3pct"], r["bid_floor_price"], r["bid_floor_qty"]))


def load_jumps(conn) -> dict[int, int]:
    with conn.cursor() as cur:
        cur.execute("select system_id, jumps_from_jita from jita.systems")
        return dict(cur.fetchall())


if __name__ == "__main__":
    import sys
    from common import db, read_snapshot, load_profile, log
    snap = read_snapshot(sys.argv[1] if len(sys.argv) > 1 else "prev_snapshot.json.gz")
    if not snap:
        sys.exit("fant ikke snapshot")
    conn = db()
    prof = load_profile(conn)
    rows, keep, _, _ = compute(snap["orders"], load_jumps(conn), (prof.thresholds or {}).get("prefilter_spread", 0.05))
    log(f"{len(rows)} varer i forfilter-settet av {len(snap['orders'])} ordrer")
    write_type_hourly(conn, rows, snap["snapshot_at"])
    conn.commit()
    conn.close()
