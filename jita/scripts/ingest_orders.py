"""
ingest_orders.py – timesjobben (spec blokk 1.1 steg 4 + 5).

1. /status/ – avbryt stille hvis Tranquility er nede.
2. Hent alle sider av /markets/10000002/orders/?order_type=all (8 parallelle).
   Alle sider må ha samme Last-Modified; avvikende hentes på nytt (maks 2 runder).
   Under 95 % av sidene → skriv INGENTING, varsle Discord.
3. Behold salgsordrer i Jita 4-4 og alle kjøpsordrer i regionen.
4. Diff mot forrige snapshot (fra SNAPSHOT_DIR, restaurert av Actions cache):
   partial (vekt 1) / gone (vekt fra gone_weight) → fills + type_flow_hourly (resolution 60),
   bare for forfilter-settet + watchlist.
5. type_hourly for forfilter-settet (compute_metrics, samme kjøring).
6. select jita.judge()
7. Lagre nytt snapshot til SNAPSHOT_DIR.
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import compute_metrics as cm
from common import (JITA_44, REGION_FORGE, Esi, EsiError, RunLog, db, fail, gone_weight,
                    load_profile, log, notify, now_utc, parse_http_date, read_snapshot,
                    write_snapshot)

SNAP_NAME = "prev_snapshot.json.gz"
PATH = f"/markets/{REGION_FORGE}/orders/"


def order_row(o: dict) -> list:
    return [o["order_id"], o["type_id"], bool(o["is_buy_order"]), float(o["price"]), int(o["volume_remain"]),
            o["issued"], int(o["duration"]), int(o["location_id"]), int(o.get("system_id") or 0), str(o.get("range"))]


def fetch_all_pages(esi: Esi, runlog: RunLog):
    """→ (orders, snapshot_at) eller kaster EsiError. Sidekonsistens per spec."""
    st, body, h = esi.get(PATH, {"order_type": "all", "page": 1}, use_etag=False)
    if st != 200 or body is None:
        raise EsiError(f"side 1 ga {st}")
    pages = int(h.get("X-Pages", "1"))
    runlog.pages_total = pages
    results: dict[int, tuple[list, datetime | None]] = {1: (body, parse_http_date(h.get("Last-Modified")))}

    def fetch(p):
        try:
            st, b, hh = esi.get(PATH, {"order_type": "all", "page": p}, use_etag=False)
            return p, (b if st == 200 else None), parse_http_date(hh.get("Last-Modified"))
        except EsiError as e:
            log(f"side {p} feilet: {e}")
            return p, None, None

    todo = list(range(2, pages + 1))
    for round_no in range(3):                       # 1 hovedrunde + maks 2 omhentinger
        if not todo:
            break
        with ThreadPoolExecutor(max_workers=8) as ex:
            for p, b, lm in ex.map(fetch, todo):
                if b is not None:
                    results[p] = (b, lm)
        # flertallets Last-Modified er «sannheten»
        counts = defaultdict(int)
        for b, lm in results.values():
            counts[lm] += 1
        ref_lm = max(counts, key=counts.get)
        todo = [p for p in range(1, pages + 1) if p not in results or results[p][1] != ref_lm]
        if todo:
            log(f"runde {round_no + 1}: {len(todo)} sider avviker fra Last-Modified {ref_lm} – henter på nytt")
            time.sleep(2)

    ok_pages = [p for p in range(1, pages + 1) if p in results and results[p][1] == ref_lm]
    runlog.pages_ok = len(ok_pages)
    if len(ok_pages) < 0.95 * pages:
        raise EsiError(f"bare {len(ok_pages)}/{pages} sider konsistente – skriver ingenting")
    orders = []
    for p in ok_pages:
        for o in results[p][0]:
            if o["is_buy_order"] or o["location_id"] == JITA_44:
                orders.append(order_row(o))
    return orders, ref_lm


def diff_fills(prev: dict | None, orders: list, snapshot_at: datetime, jumps: dict):
    """→ (fills, hours_covered). fills = [(order_id, type_id, is_buy, price, qty, kind, weight)]"""
    if not prev:
        return [], None
    prev_at = datetime.fromisoformat(prev["snapshot_at"])
    hours = max((snapshot_at - prev_at).total_seconds() / 3600, 0.05)
    cur = {o[cm.O_ID]: o for o in orders}
    # beste pris per (type, side) i FORRIGE snapshot – til gone_weight
    pbuys, psells = cm.split_book(prev["orders"], jumps)
    pbest = {}
    for t, l in pbuys.items():
        pbest[(t, True)] = max(o[cm.O_PRICE] for o in l)
    for t, l in psells.items():
        pbest[(t, False)] = min(o[cm.O_PRICE] for o in l)

    fills = []
    for o in prev["orders"]:
        oid = o[cm.O_ID]
        now_o = cur.get(oid)
        if now_o is not None:
            d = o[cm.O_VOL] - now_o[cm.O_VOL]
            if d > 0:
                fills.append((oid, o[cm.O_TYPE], o[cm.O_BUY], o[cm.O_PRICE], d, "partial", 1.0))
            continue
        # borte: utløpt?
        try:
            issued = datetime.fromisoformat(o[cm.O_ISSUED].replace("Z", "+00:00"))
        except Exception:
            issued = prev_at
        if issued + timedelta(days=o[cm.O_DUR]) <= snapshot_at:
            continue
        best = pbest.get((o[cm.O_TYPE], o[cm.O_BUY]))
        if best is None:
            continue                                # (kjøpsordre som ikke dekket Jita – ikke interessant)
        w = gone_weight(o[cm.O_PRICE], best, o[cm.O_BUY])
        fills.append((oid, o[cm.O_TYPE], o[cm.O_BUY], o[cm.O_PRICE], o[cm.O_VOL], "gone", w))
    return fills, hours


def write_flow(conn, fills, snapshot_at, hours, keep: set, resolution: int):
    """Skriver fills + type_flow_hourly for typer i keep."""
    fills = [f for f in fills if f[1] in keep]
    agg = defaultdict(lambda: [0.0, 0, 0.0, 0])      # bfs_qty, bfs_trades, s2b_qty, s2b_trades
    for oid, t, is_buy, price, qty, kind, w in fills:
        a = agg[t]
        if is_buy:                                    # kjøpsordre fylt = noen solgte til den = S2B (dumpet)
            a[2] += qty * w
            a[3] += 1
        else:                                         # salgsordre fylt = noen kjøpte = BfS (liftet)
            a[0] += qty * w
            a[1] += 1
    hour = snapshot_at.replace(minute=0, second=0, microsecond=0)
    with conn.cursor() as cur:
        with cur.copy("copy jita.fills (observed_at, order_id, type_id, is_buy, price, qty, kind, weight, resolution) from stdin") as cp:
            for oid, t, is_buy, price, qty, kind, w in fills:
                cp.write_row((snapshot_at, oid, t, is_buy, price, qty, kind, w, resolution))
        cur.executemany(
            """insert into jita.type_flow_hourly (type_id, hour, resolution, bfs_qty, bfs_trades, s2b_qty, s2b_trades, hours_covered)
               values (%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (type_id, hour, resolution) do update set
                 bfs_qty = jita.type_flow_hourly.bfs_qty + excluded.bfs_qty,
                 bfs_trades = jita.type_flow_hourly.bfs_trades + excluded.bfs_trades,
                 s2b_qty = jita.type_flow_hourly.s2b_qty + excluded.s2b_qty,
                 s2b_trades = jita.type_flow_hourly.s2b_trades + excluded.s2b_trades,
                 hours_covered = jita.type_flow_hourly.hours_covered + excluded.hours_covered""",
            [(t, hour, resolution, a[0], a[1], a[2], a[3], hours) for t, a in agg.items()])
    return len(fills), len(agg)


def load_watchlist(conn) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("select type_id from jita.watchlist where status = 'follow'")
        return {r[0] for r in cur.fetchall()}


def run_judge(conn):
    with conn.cursor() as cur:
        cur.execute("select jita.judge()")
        return cur.fetchone()[0]


def main():
    runlog = RunLog("hourly")
    esi = Esi()
    if not esi.status_ok():
        log("Tranquility nede – avbryter stille")
        return
    conn = db()
    try:
        profile = load_profile(conn)
        jumps = cm.load_jumps(conn)
        if not jumps:
            fail(runlog, "jita.systems er tom – kjør seed_types.py først", conn)
        th = profile.thresholds or {}
        min_spread = float(th.get("prefilter_spread", 0.05))
        min_orders = int(th.get("prefilter_min_orders", 3))
        excluded = cm.load_excluded(conn)

        try:
            orders, snapshot_at = fetch_all_pages(esi, runlog)
        except EsiError as e:
            fail(runlog, str(e), conn)
        runlog.snapshot_at = snapshot_at
        runlog.orders_count = len(orders)
        runlog.ratelimit_remaining = esi.ratelimit_remaining
        log(f"{len(orders)} ordrer beholdt, snapshot_at={snapshot_at}, {runlog.pages_ok}/{runlog.pages_total} sider")

        prev = read_snapshot(SNAP_NAME)
        if prev and prev["snapshot_at"] == snapshot_at.isoformat():
            runlog.finish(conn, ok=True, message="samme Last-Modified som forrige – ingen ny data")
            return

        rows, keep, _, _ = cm.compute(orders, jumps, min_spread, excluded, min_orders)
        keep = keep | load_watchlist(conn)
        cm.write_type_hourly(conn, rows, snapshot_at)
        log(f"type_hourly: {len(rows)} varer i forfilter-settet")

        fills, hours = diff_fills(prev, orders, snapshot_at, jumps)
        if prev:
            n_f, n_t = write_flow(conn, fills, snapshot_at, hours, keep, 60)
            log(f"fills: {n_f} hendelser på {n_t} varer, {round(hours, 2)} t siden forrige")
            runlog.message = f"{n_f} fills/{n_t} varer, {round(hours, 2)}t"
        else:
            runlog.message = "første kjøring – ingen diff"
        conn.commit()

        n_pass = run_judge(conn)
        conn.commit()
        log(f"judge: {n_pass} kandidater passerte")
        runlog.message += f", judge {n_pass} passed"

        write_snapshot(SNAP_NAME, {"snapshot_at": snapshot_at.isoformat(), "orders": orders})
        esi.save_etags()
        runlog.finish(conn, ok=True)
    except SystemExit:
        raise
    except Exception as e:
        conn.rollback()
        fail(runlog, f"{type(e).__name__}: {e}", conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
