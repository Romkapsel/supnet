"""
ingest_history.py – hver time (blokk 1.4 pkt 3, 16. sept 2026; var daglig og bare watchlist).

Henter /markets/10000002/history/?type_id=… (The Forge, daglig snitt/høy/lav/volum/antall handler)
for inntil HISTORY_BATCH varer per kjøring, prioritert:
  1. dagens passed-kandidater, watchlist og varer du holder
  2. varer i forfilteret uten historikk
  3. eldste history_fetched_at
Maks 4 parallelle med 1 s pause (300 kall/min-grensen) → ~240/min → ~2 min per kjøring.
Hele forfilteret (~7 000) er dekket på ~18 timer; ESI oppdaterer historikken bare én gang i døgnet.
Skriver siste 30 dager til jita.history_daily og setter types.history_fetched_at.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from common import REGION_FORGE, Esi, EsiError, RunLog, db, fail, log

HISTORY_BATCH = int(os.environ.get("HISTORY_BATCH", "400"))


def target_types(conn) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("""
            with prio1 as (
              select type_id from jita.candidates where run_at = (select max(run_at) from jita.candidates) and passed
              union select type_id from jita.watchlist where status = 'follow'
              union select type_id from jita.decisions where closed_at is null
              union select type_id from jita.my_orders where state = 'open'),
            book as (select type_id from jita.type_hourly where snapshot_at = (select max(snapshot_at) from jita.type_hourly)),
            ranked as (
              select b.type_id,
                     case when b.type_id in (select type_id from prio1) then 0
                          when t.history_fetched_at is null then 1 else 2 end as prio,
                     t.history_fetched_at
              from book b join jita.types t using (type_id)
              where not t.is_excluded and t.history_fetched_at is distinct from (current_date)::timestamptz
                and (t.history_fetched_at is null or t.history_fetched_at < now() - interval '20 hours'))
            select type_id from ranked order by prio, history_fetched_at nulls first, type_id limit %s""", (HISTORY_BATCH,))
        return [r[0] for r in cur.fetchall()]


def main():
    runlog = RunLog("history")
    esi = Esi()
    if not esi.status_ok():
        log("Tranquility nede – avbryter stille")
        return
    conn = db()
    try:
        types = target_types(conn)
        if not types:
            runlog.finish(conn, ok=True, message="alle varer i forfilteret har fersk historikk")
            return
        since = date.today() - timedelta(days=31)
        rows, ok_types = [], []

        def fetch(t):
            time.sleep(1.0)
            try:
                st, b, _ = esi.get(f"/markets/{REGION_FORGE}/history/", {"type_id": t}, use_etag=True)
                return t, (b if st in (200, 304) else None)
            except EsiError as e:
                log(f"historikk {t} feilet: {e}")
                return t, None

        with ThreadPoolExecutor(max_workers=4) as ex:
            for t, b in ex.map(fetch, types):
                if b is None:
                    continue
                ok_types.append(t)
                for d in b:
                    if date.fromisoformat(d["date"]) >= since:
                        rows.append((t, d["date"], d["average"], d["highest"], d["lowest"],
                                     d["volume"], d["order_count"]))
        with conn.cursor() as cur:
            cur.executemany(
                """insert into jita.history_daily (type_id, date, average, highest, lowest, volume, order_count)
                   values (%s,%s,%s,%s,%s,%s,%s)
                   on conflict (type_id, date) do update set average = excluded.average, highest = excluded.highest,
                     lowest = excluded.lowest, volume = excluded.volume, order_count = excluded.order_count""",
                rows)
            cur.execute("update jita.types set history_fetched_at = now() where type_id = any(%s)", (ok_types,))
            cur.execute("""select count(*) from jita.type_hourly h join jita.types t using (type_id)
                           where h.snapshot_at = (select max(snapshot_at) from jita.type_hourly)
                             and not t.is_excluded and t.history_fetched_at is null""")
            remaining = cur.fetchone()[0]
        conn.commit()
        esi.save_etags()
        runlog.pages_total, runlog.pages_ok = len(types), len(ok_types)
        runlog.ratelimit_remaining = esi.ratelimit_remaining
        runlog.finish(conn, ok=(len(ok_types) >= 0.9 * len(types)),
                      message=f"{len(ok_types)}/{len(types)} varer, {len(rows)} rader, {remaining} i forfilteret mangler ennå")
    except SystemExit:
        raise
    except Exception as e:
        conn.rollback()
        fail(runlog, f"{type(e).__name__}: {e}", conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
