"""
ingest_history.py – daglig (spec blokk 1.1 steg 6).

For varer som passerte regel 1–4 i siste candidates-kjøring (passed, eller failed_rules bare
inneholder 5–9) + watchlist: hent /markets/10000002/history/?type_id=… og skriv siste 30 dager
til jita.history_daily. Maks 4 parallelle med 1 s pause (300/min-grensen), ETag.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from common import REGION_FORGE, Esi, EsiError, RunLog, db, fail, log
from ingest_orders import load_watchlist

EARLY_RULES = {"1", "1b", "1x", "2", "3", "4", "8"}


def target_types(conn) -> set[int]:
    types = load_watchlist(conn)
    with conn.cursor() as cur:
        cur.execute("""select type_id, passed, failed_rules from jita.candidates
                       where run_at = (select max(run_at) from jita.candidates)""")
        for t, passed, failed in cur.fetchall():
            if passed or not (set(failed or []) & EARLY_RULES):
                types.add(t)
    return types


def main():
    runlog = RunLog("history")
    esi = Esi()
    if not esi.status_ok():
        log("Tranquility nede – avbryter stille")
        return
    conn = db()
    try:
        types = sorted(target_types(conn))
        if not types:
            runlog.finish(conn, ok=True, message="ingen varer ennå")
            return
        since = date.today() - timedelta(days=31)
        rows, ok = [], 0

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
                ok += 1
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
        conn.commit()
        esi.save_etags()
        runlog.pages_total, runlog.pages_ok = len(types), ok
        runlog.ratelimit_remaining = esi.ratelimit_remaining
        runlog.finish(conn, ok=(ok >= 0.9 * len(types)), message=f"{ok}/{len(types)} varer, {len(rows)} rader")
    except SystemExit:
        raise
    except Exception as e:
        conn.rollback()
        fail(runlog, f"{type(e).__name__}: {e}", conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
