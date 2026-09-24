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

import os
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
        # NYESTE Last-Modified er «sannheten»: ruller ESI-cachen midt i hentingen, er det de gamle sidene
        # som skal hentes på nytt (de kommer da i ny versjon) – ikke de nye.
        ref_lm = max((lm for b, lm in results.values() if lm), default=None)
        todo = [p for p in range(1, pages + 1) if p not in results or results[p][1] != ref_lm]
        if todo:
            log(f"runde {round_no + 1}: {len(todo)} sider eldre enn Last-Modified {ref_lm} – henter på nytt")
            time.sleep(2)

    ok_pages = [p for p in range(1, pages + 1) if p in results and results[p][1] == ref_lm]
    runlog.pages_ok = len(ok_pages)
    if len(ok_pages) < 0.95 * pages:
        raise EsiError(f"bare {len(ok_pages)}/{pages} sider konsistente – skriver ingenting")
    orders, npc = [], []
    for p in ok_pages:
        for o in results[p][0]:
            if o["is_buy_order"] or o["location_id"] == JITA_44:
                orders.append(order_row(o))
            elif o["duration"] >= 365:                      # NPC-seedede salgsordrer andre steder i regionen
                npc.append(order_row(o))
    return orders, ref_lm, npc


def diff_fills(prev: dict | None, orders: list, snapshot_at: datetime, jumps: dict, mods: list | None = None):
    """→ (fills, hours_covered). fills = [(order_id, type_id, is_buy, price, qty, kind, weight)]
    mods (valgfri liste) fylles med (type_id, is_buy) for hver ordre som endret PRIS – krig-indeksen."""
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

    # bare ordrer som er relevante for Jita 4-4: salgsordrer i 4-4 (alltid, pga. filteret ved henting)
    # og kjøpsordrer som DEKKER 4-4 – ellers teller vi dumping i stasjoner langt unna som Jita-flyt
    relevant = {o[cm.O_ID] for l in pbuys.values() for o in l} | {o[cm.O_ID] for l in psells.values() for o in l}
    fills = []
    seen = set()
    for o in prev["orders"]:
        oid = o[cm.O_ID]
        if oid not in relevant or oid in seen:
            continue
        seen.add(oid)
        now_o = cur.get(oid)
        if now_o is not None:
            d = o[cm.O_VOL] - now_o[cm.O_VOL]
            if d > 0:
                fills.append((oid, o[cm.O_TYPE], o[cm.O_BUY], o[cm.O_PRICE], d, "partial", 1.0))
            if mods is not None and now_o[cm.O_PRICE] != o[cm.O_PRICE]:
                best = pbest.get((o[cm.O_TYPE], o[cm.O_BUY]))
                # bare endringer nær toppen teller som «krig» (innenfor 1 % av forrige beste pris)
                if best and abs(o[cm.O_PRICE] - best) / best <= 0.01:
                    mods.append((o[cm.O_TYPE], o[cm.O_BUY]))
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


def write_flow(conn, fills, snapshot_at, hours, keep: set, resolution: int, mods: list | None = None):
    """Skriver fills + type_flow_hourly (inkl. prisendringer = krig-indeks) for typer i keep."""
    fills = [f for f in fills if f[1] in keep]
    agg = defaultdict(lambda: [0.0, 0, 0.0, 0, 0, 0])   # bfs_qty, bfs_trades, s2b_qty, s2b_trades, bid_mods, ask_mods
    for oid, t, is_buy, price, qty, kind, w in fills:
        a = agg[t]
        if is_buy:                                    # kjøpsordre fylt = noen solgte til den = S2B (dumpet)
            a[2] += qty * w
            a[3] += 1
        else:                                         # salgsordre fylt = noen kjøpte = BfS (liftet)
            a[0] += qty * w
            a[1] += 1
    for t, is_buy in (mods or []):
        if t in keep:
            agg[t][4 if is_buy else 5] += 1
    hour = snapshot_at.replace(minute=0, second=0, microsecond=0)
    with conn.cursor() as cur:
        with cur.copy("copy jita.fills (observed_at, order_id, type_id, is_buy, price, qty, kind, weight, resolution) from stdin") as cp:
            for oid, t, is_buy, price, qty, kind, w in fills:
                cp.write_row((snapshot_at, oid, t, is_buy, price, qty, kind, w, resolution))
        cur.executemany(
            """insert into jita.type_flow_hourly (type_id, hour, resolution, bfs_qty, bfs_trades, s2b_qty, s2b_trades, hours_covered, bid_mods, ask_mods)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (type_id, hour, resolution) do update set
                 bfs_qty = jita.type_flow_hourly.bfs_qty + excluded.bfs_qty,
                 bfs_trades = jita.type_flow_hourly.bfs_trades + excluded.bfs_trades,
                 s2b_qty = jita.type_flow_hourly.s2b_qty + excluded.s2b_qty,
                 s2b_trades = jita.type_flow_hourly.s2b_trades + excluded.s2b_trades,
                 hours_covered = jita.type_flow_hourly.hours_covered + excluded.hours_covered,
                 bid_mods = coalesce(jita.type_flow_hourly.bid_mods, 0) + excluded.bid_mods,
                 ask_mods = coalesce(jita.type_flow_hourly.ask_mods, 0) + excluded.ask_mods""",
            [(t, hour, resolution, a[0], a[1], a[2], a[3], hours, a[4], a[5]) for t, a in agg.items()])
    return len(fills), len(agg)


def check_positions(conn, buys: dict, sells: dict, profile) -> int:
    """Overbuds-vakt (spec 2.4): for hver åpen kjøpsordre i jita.decisions – ligger noen over?
    Regner mur, gebyr og råd (HOLD / ENDRE / TREKK), lagrer i jita.alerts og varsler Discord ved endring."""
    from common import overbid_advice, isk
    min_margin = float((profile.thresholds or {}).get("min_margin", 0.10))
    with conn.cursor() as cur:
        # én vurdering per vare: din HØYESTE egen pris er referansen (egne ordrer skal ikke telle som overbud)
        cur.execute("""select max(d.id), d.type_id, t.name, max(d.price)::float8,
                              greatest(0, sum(d.qty - coalesce(d.filled_qty, 0)))::int as remaining
                       from jita.decisions d join jita.types t using (type_id)
                       where d.side = 'buy' and d.filled_at is null and d.closed_at is null
                       group by d.type_id, t.name""")
        open_buys = [r for r in cur.fetchall() if r[4] > 0]
        if not open_buys:
            return 0
        cur.execute("""select type_id, coalesce(sum(s2b_qty) / greatest(sum(hours_covered), 1) * 24, 0)::float8
                       from jita.type_flow_hourly where hour > now() - interval '25 hours' and resolution = 60
                         and type_id = any(%s) group by type_id""", ([r[1] for r in open_buys],))
        s2b = dict(cur.fetchall())
        cur.execute("""select distinct on ((payload->>'decision_id')::bigint) (payload->>'decision_id')::bigint, kind, payload,
                              extract(epoch from (now() - created_at)) / 3600 as age_h
                       from jita.alerts where kind in ('overbid', 'overbid_cleared')
                       order by (payload->>'decision_id')::bigint, created_at desc""")
        last = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    n = 0
    for did, tid, name, p1, remaining in open_buys:
        bl, sl = buys.get(tid, []), sells.get(tid, [])
        if not bl or not sl:
            continue
        best_bid = max(o[cm.O_PRICE] for o in bl)
        best_ask = min(o[cm.O_PRICE] for o in sl)
        prev_kind, prev_payload, prev_age = last.get(did, (None, {}, 99))
        if best_bid <= p1 + 1e-9:                       # du ligger på toppen
            if prev_kind == "overbid":
                _alert(conn, "overbid_cleared", tid, dict(decision_id=did, price=p1, best_bid=best_bid,
                       text=f"{name}: du ligger på toppen igjen ({isk(p1)})."))
                notify(f"✅ {name}: du ligger på toppen igjen ({isk(p1)}).")
                n += 1
            continue
        wall = sum(o[cm.O_VOL] for o in bl if o[cm.O_PRICE] > p1)
        adv = overbid_advice(profile, p1, remaining, best_bid, wall, s2b.get(tid, 0.0), best_ask, min_margin)
        payload = dict(decision_id=did, price=p1, remaining=remaining, best_bid=best_bid, best_ask=best_ask, **adv)
        # varsle bare ved endring: annet råd, toppbudet flyttet > 2 %, eller > 2 t siden sist (1-ISK-hakk hvert 20. min er støy)
        if (prev_kind == "overbid" and prev_payload.get("action") == adv["action"]
                and abs(float(prev_payload.get("best_bid", 0)) - best_bid) / best_bid < 0.02 and prev_age < 2):
            continue
        _alert(conn, "overbid", tid, payload)
        notify(f"⚠️ Overbudt: **{name}** – ditt bud {isk(p1)}, toppbud nå {isk(best_bid)} "
               f"(mur {wall} stk ≈ {adv['days_wall']} d).\nRåd: **{adv['action']}** – {adv['text']}")
        n += 1
    n += check_sell_orders(conn, buys, sells, profile, min_margin)
    conn.commit()
    return n


def check_sell_orders(conn, buys, sells, profile, min_margin) -> int:
    """Undercut-vakt for dine salgsordrer i Jita 4-4 (fra jita.my_orders, fase 2)."""
    from common import undercut_advice, isk
    with conn.cursor() as cur:
        cur.execute("""select o.order_id, o.type_id, t.name, o.price::float8, o.volume_remain::int
                       from jita.my_orders o join jita.types t using (type_id)
                       where o.state = 'open' and not o.is_buy and o.location_id = 60003760""")
        my_sells = cur.fetchall()
        if not my_sells:
            return 0
        cur.execute("""select type_id, coalesce(sum(bfs_qty) / greatest(sum(hours_covered), 1) * 24, 0)::float8
                       from jita.type_flow_hourly where hour > now() - interval '25 hours' and resolution = 60
                         and type_id = any(%s) group by type_id""", ([r[1] for r in my_sells],))
        bfs = dict(cur.fetchall())
        cur.execute("""select type_id, sum(unit_price * quantity) / nullif(sum(quantity), 0)
                       from jita.my_transactions where is_buy and date > now() - interval '90 days'
                         and type_id = any(%s) group by type_id""", ([r[1] for r in my_sells],))
        cost = {t: float(c) for t, c in cur.fetchall() if c is not None}
        cur.execute("""select distinct on ((payload->>'order_id')::bigint) (payload->>'order_id')::bigint, kind, payload,
                              extract(epoch from (now() - created_at)) / 3600 as age_h
                       from jita.alerts where kind in ('undercut', 'undercut_cleared')
                       order by (payload->>'order_id')::bigint, created_at desc""")
        last = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    n = 0
    for oid, tid, name, p1, remaining in my_sells:
        sl = sells.get(tid, [])
        if not sl:
            continue
        best_ask = min(o[cm.O_PRICE] for o in sl)
        prev_kind, prev_payload, prev_age = last.get(oid, (None, {}, 99))
        if best_ask >= p1 - 1e-9:                       # du er billigst
            if prev_kind == "undercut":
                _alert(conn, "undercut_cleared", tid, dict(order_id=oid, price=p1, text=f"{name}: du er billigst igjen ({isk(p1)})."))
                notify(f"✅ {name}: salgsordren din er billigst igjen ({isk(p1)}).")
                n += 1
            continue
        wall = sum(o[cm.O_VOL] for o in sl if o[cm.O_PRICE] < p1)
        adv = undercut_advice(profile, p1, remaining, best_ask, wall, bfs.get(tid, 0.0), cost.get(tid), min_margin)
        payload = dict(order_id=oid, type_id=tid, price=p1, remaining=remaining, best_ask=best_ask, **adv)
        if (prev_kind == "undercut" and prev_payload.get("action") == adv["action"]
                and abs(float(prev_payload.get("best_ask", 0)) - best_ask) / best_ask < 0.02 and prev_age < 2):
            continue
        _alert(conn, "undercut", tid, payload)
        notify(f"⚠️ Undercut: **{name}** – din ask {isk(p1)}, laveste nå {isk(best_ask)} (mur {wall} stk ≈ {adv['days_wall']} d).\n"
               f"Råd: **{adv['action']}** – {adv['text']}")
        n += 1
    return n


def _alert(conn, kind: str, type_id: int, payload: dict):
    import json as _json
    with conn.cursor() as cur:
        cur.execute("insert into jita.alerts (kind, type_id, payload) values (%s, %s, %s::jsonb)",
                    (kind, type_id, _json.dumps(payload)))


def flag_npc_seeded(conn, all_orders_region: list) -> int:
    """Salgsordrer med 365 dagers varighet finnes bare fra NPC (spillere maks 90). Varen er da NPC-seedet:
    uendelig tilbud og prislokk. Flagges i jita.types (npc_seeded, npc_seed_price)."""
    seed = {}
    for o in all_orders_region:
        if not o[cm.O_BUY] and o[cm.O_DUR] >= 365:
            seed[o[cm.O_TYPE]] = min(seed.get(o[cm.O_TYPE], float("inf")), o[cm.O_PRICE])
    with conn.cursor() as cur:
        cur.execute("update jita.types set npc_seeded = false, npc_seed_price = null where npc_seeded and not (type_id = any(%s))", (list(seed),))
        cur.executemany("update jita.types set npc_seeded = true, npc_seed_price = %s where type_id = %s and (not npc_seeded or npc_seed_price is distinct from %s)",
                        [(p, t, p) for t, p in seed.items()])
    return len(seed)


def load_watchlist(conn) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("select type_id from jita.watchlist where status = 'follow'")
        return {r[0] for r in cur.fetchall()}


def run_judge(conn):
    """Kjører dommeren og varsler Discord om endringer (spec del 4, varsler b og c)."""
    with conn.cursor() as cur:
        cur.execute("""select array_agg(type_id order by score desc nulls last)
                       from (select type_id, score from jita.candidates
                             where run_at = (select max(run_at) from jita.candidates) and passed
                             order by score desc nulls last limit 3) x""")
        prev_top3 = cur.fetchone()[0] or []
        cur.execute("""select w.type_id, c.passed from jita.watchlist w
                       left join jita.candidates c on c.type_id = w.type_id
                         and c.run_at = (select max(run_at) from jita.candidates)
                       where w.status = 'follow'""")
        prev_wl = dict(cur.fetchall())
        cur.execute("select jita.judge()")
        n = cur.fetchone()[0]
        cur.execute("""select c.type_id, t.name, c.qty, c.buy_price, c.sell_price, c.expected_profit
                       from jita.candidates c join jita.types t using (type_id)
                       where c.run_at = (select max(run_at) from jita.candidates) and c.passed
                       order by c.score desc nulls last limit 3""")
        for tid, name, qty, buy, sell, profit in cur.fetchall():
            if tid not in prev_top3:
                notify(f"📈 Ny i topp 3: **{name}** – kjøp {qty} à {buy:,.0f}, selg {sell:,.0f} → +{profit:,.0f} ISK")
        cur.execute("""select w.type_id, t.name, c.passed from jita.watchlist w join jita.types t using (type_id)
                       left join jita.candidates c on c.type_id = w.type_id
                         and c.run_at = (select max(run_at) from jita.candidates)
                       where w.status = 'follow'""")
        for tid, name, passed in cur.fetchall():
            if tid in prev_wl and prev_wl[tid] is not None and passed is not None and prev_wl[tid] != passed:
                notify(f"{'✅' if passed else '❌'} Watchlist: **{name}** {'passerer nå' if passed else 'passerer ikke lenger'}")
        return n


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
            orders, snapshot_at, npc_orders = fetch_all_pages(esi, runlog)
        except EsiError as e:
            fail(runlog, str(e), conn)
        # strukturordrer (TTT, Perimeter-citadeller …). AV som standard (STRUCT_ORDERS=1 slår på): det viste seg at
        # regionsboka fra ESI allerede inneholder kjøpsordrene i strukturer (30 000+, alle rekkevidder unntatt
        # «station») – Datacore-feilen 21. sept skyldtes hopptabellen (Perimeter=99), ikke manglende strukturdata.
        try:
            import structures
            tok = structures.get_token() if os.environ.get("STRUCT_ORDERS") == "1" else None
            if tok:
                with conn.cursor() as cur:
                    cur.execute("select coalesce(max(updated_at), 'epoch') < now() - interval '20 hours' from jita.structures")
                    stale = cur.fetchone()[0]
                if stale:
                    structures.discover(conn, tok, jumps)
                orders.extend(structures.fetch_structure_buy_orders(conn, tok))
        except Exception as e:
            log("strukturordrer feilet (fortsetter uten):", e)
        # samme ordre kan dukke opp to ganger når den flytter seg mellom sider under henting (særlig
        # strukturmarkedene, som ikke har felles Last-Modified) → duplikatnøkkel i fills. Behold én per order_id.
        n0 = len(orders)
        orders = list({o[cm.O_ID]: o for o in orders}.values())
        if len(orders) < n0:
            log(f"{n0 - len(orders)} dupliserte ordre-id-er fjernet")
        runlog.snapshot_at = snapshot_at
        runlog.orders_count = len(orders)
        runlog.ratelimit_remaining = esi.ratelimit_remaining
        log(f"{len(orders)} ordrer beholdt, snapshot_at={snapshot_at}, {runlog.pages_ok}/{runlog.pages_total} sider")

        prev = read_snapshot(SNAP_NAME)
        if prev and prev["snapshot_at"] == snapshot_at.isoformat():
            runlog.finish(conn, ok=True, message="samme Last-Modified som forrige – ingen ny data")
            return

        rows, keep, buys, sells = cm.compute(orders, jumps, min_spread, excluded, min_orders)
        wl = load_watchlist(conn)
        for t in wl - keep:                         # fulgte varer dømmes selv om de er utenfor forfilteret
            if buys.get(t) and sells.get(t):
                rows.append(cm.metrics_for(t, buys[t], sells[t]))
        keep = keep | wl
        cm.write_type_hourly(conn, rows, snapshot_at)
        log(f"type_hourly: {len(rows)} varer i forfilter-settet")

        mods = []
        fills, hours = diff_fills(prev, orders, snapshot_at, jumps, mods)
        if prev:
            n_f, n_t = write_flow(conn, fills, snapshot_at, hours, keep, 60, mods)
            log(f"prisendringer nær toppen: {len(mods)}")
            log(f"fills: {n_f} hendelser på {n_t} varer, {round(hours, 2)} t siden forrige")
            runlog.message = f"{n_f} fills/{n_t} varer, {round(hours, 2)}t"
        else:
            runlog.message = "første kjøring – ingen diff"
        conn.commit()

        n_npc = flag_npc_seeded(conn, orders + npc_orders)
        conn.commit()
        n_pass = run_judge(conn)
        conn.commit()
        log(f"judge: {n_pass} kandidater passerte ({n_npc} NPC-seedede varer flagget)")
        runlog.message += f", judge {n_pass} passed"
        n_alerts = check_positions(conn, buys, sells, profile)
        if n_alerts:
            log(f"posisjonsvakt: {n_alerts} varsler")

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
