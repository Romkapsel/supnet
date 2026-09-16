"""
ingest_watchlist.py – den lette 20-minuttersjobben (spec blokk 1.1 steg 5b).

For varer i jita.watchlist (status 'follow') + topp 20 fra siste candidates:
hent /markets/10000002/orders/?type_id=… (én side per vare), diff mot
prev_snapshot_watchlist.json.gz, skriv fills + type_flow_hourly med resolution=20,
kjør jita.judge(). Rører IKKE type_hourly.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import compute_metrics as cm
from common import (JITA_44, REGION_FORGE, Esi, EsiError, RunLog, db, fail, log,
                    parse_http_date, read_snapshot, write_snapshot)
from ingest_orders import diff_fills, order_row, run_judge, write_flow, load_watchlist, check_positions
from common import load_profile

SNAP_NAME = "prev_snapshot_watchlist.json.gz"
PATH = f"/markets/{REGION_FORGE}/orders/"


def target_types(conn) -> set[int]:
    types = load_watchlist(conn)
    with conn.cursor() as cur:
        cur.execute("""select type_id from jita.candidates
                       where run_at = (select max(run_at) from jita.candidates) and passed
                       order by score desc limit 20""")
        types |= {r[0] for r in cur.fetchall()}
        cur.execute("select distinct type_id from jita.decisions where closed_at is null")   # det du holder
        types |= {r[0] for r in cur.fetchall()}
    return types


def main():
    runlog = RunLog("watchlist")
    esi = Esi()
    if not esi.status_ok():
        log("Tranquility nede – avbryter stille")
        return
    conn = db()
    try:
        types = target_types(conn)
        if not types:
            runlog.finish(conn, ok=True, message="ingen varer å følge ennå")
            return
        jumps = cm.load_jumps(conn)

        def fetch(t):
            try:
                st, b, h = esi.get(PATH, {"order_type": "all", "type_id": t}, use_etag=True)
                return t, (b if st in (200, 304) else None), parse_http_date(h.get("Last-Modified"))
            except EsiError as e:
                log(f"type {t} feilet: {e}")
                return t, None, None

        orders, lms, ok = [], [], 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            for t, b, lm in ex.map(fetch, sorted(types)):
                if b is None:
                    continue
                ok += 1
                lms.append(lm)
                for o in b:
                    if o["is_buy_order"] or o["location_id"] == JITA_44:
                        orders.append(order_row(o))
        runlog.pages_total, runlog.pages_ok, runlog.orders_count = len(types), ok, len(orders)
        runlog.ratelimit_remaining = esi.ratelimit_remaining
        if ok < 0.95 * len(types):
            fail(runlog, f"bare {ok}/{len(types)} varer hentet", conn)
        snapshot_at = max(lm for lm in lms if lm) if any(lms) else None
        if snapshot_at is None:
            fail(runlog, "ingen Last-Modified", conn)
        runlog.snapshot_at = snapshot_at

        prev = read_snapshot(SNAP_NAME)
        if prev:
            # bare typer som fantes i forrige snapshot kan diffes
            prev_types = {o[cm.O_TYPE] for o in prev["orders"]}
            mods = []
            fills, hours = diff_fills(prev, orders, snapshot_at, jumps, mods)
            n_f, n_t = write_flow(conn, fills, snapshot_at, hours, types & prev_types, 20, mods)
            runlog.message = f"{len(types)} varer, {n_f} fills/{n_t} varer, {round(hours, 2)}t"
        else:
            runlog.message = f"{len(types)} varer, første kjøring"
        conn.commit()
        n_pass = run_judge(conn)
        conn.commit()
        runlog.message += f", judge {n_pass} passed"
        buys, sells = cm.split_book(orders, jumps)
        n_alerts = check_positions(conn, buys, sells, load_profile(conn))
        if n_alerts:
            runlog.message += f", {n_alerts} posisjonsvarsler"
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
