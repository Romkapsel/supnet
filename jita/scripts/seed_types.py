"""
seed_types.py – fyller jita.types (alle varer som handles i The Forge) og jita.systems.

Klassifisering (spec del 5) fra dogma-attributtene 1692 metaGroupID og 633 metaLevel;
navneordlista brukes bare når begge mangler.

Kjøres én gang, og ellers når nye varer dukker opp (f.eks. månedlig). Tar 10–20 min første gang.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import (Esi, JITA_SYSTEM, REGION_FORGE, RunLog, db, fail, log)

META_WORDS = ("Compact", "Enduring", "Ample", "Prototype", "Scoped", "Restrained", "Upgraded",
              "Modulated", "Limited", "Experimental", "'Arbalest'", "'Malkuth'", "'Regulated'",
              "'Allotek'", "'Kindred'")
EXCLUDED_CATEGORIES = {9, 91, 30}          # blueprint, skin, apparel
T2_META = {2, 14}                          # Tech II, Tech III
FACTION_META = {3, 4, 5, 6, 52}            # storyline, faction, officer, deadspace, structure faction
EXCLUDED_META = {15}                       # abyssal (muterte moduler)


def classify(name: str, category_id: int | None, group_name: str | None,
             meta_group_id: int | None, meta_level: int | None, published: bool | None) -> dict:
    gname = (group_name or "").lower()
    excluded = (
        published is False
        or category_id in EXCLUDED_CATEGORIES
        or "mutaplasmid" in gname
        or "filament" in gname
        or "skin" in gname and category_id == 91
        or (meta_group_id in EXCLUDED_META)
    )
    if meta_group_id:                      # 0 fra ESI betyr «ikke satt»
        is_meta = meta_group_id == 1 and (meta_level or 0) > 0
        is_t2 = meta_group_id in T2_META
        is_faction = meta_group_id in FACTION_META
    elif meta_level is not None:
        is_meta = 1 <= meta_level <= 4
        is_t2 = meta_level == 5
        is_faction = meta_level >= 6
    else:
        is_meta = any(w in name for w in META_WORDS)
        is_t2 = name.endswith(" II")
        is_faction = False
    if excluded:
        is_meta = is_t2 = is_faction = False
    is_t1 = not (excluded or is_meta or is_t2 or is_faction)
    return dict(is_excluded=excluded, is_meta=is_meta, is_t2=is_t2, is_faction=is_faction,
                is_t1=is_t1, is_ship=(category_id == 6))


def fetch_type_ids(esi: Esi) -> list[int]:
    st, body, h = esi.get(f"/markets/{REGION_FORGE}/types/", {"page": 1}, use_etag=False)
    pages = int(h.get("X-Pages", "1"))
    ids = list(body)
    for p in range(2, pages + 1):
        _, b, _ = esi.get(f"/markets/{REGION_FORGE}/types/", {"page": p}, use_etag=False)
        ids.extend(b)
    return sorted(set(ids))


def main():
    runlog = RunLog("seed_types")
    esi = Esi()
    conn = db()
    try:
        ids = fetch_type_ids(esi)
        log(f"{len(ids)} varetyper i The Forge")

        groups: dict[int, dict] = {}
        mgroups: dict[int, dict] = {}
        glock = __import__("threading").Lock()

        def get_group(gid):
            with glock:
                if gid in groups:
                    return groups[gid]
            _, b, _ = esi.get(f"/universe/groups/{gid}/", use_etag=False)
            with glock:
                groups[gid] = b or {}
            return groups[gid]

        def get_mgroup(mid):
            with glock:
                if mid in mgroups:
                    return mgroups[mid]
            _, b, _ = esi.get(f"/markets/groups/{mid}/", use_etag=False)
            with glock:
                mgroups[mid] = b or {}
            return mgroups[mid]

        def mg_path(mid):
            parts, seen = [], set()
            while mid and mid not in seen:
                seen.add(mid)
                g = get_mgroup(mid)
                parts.append(g.get("name", "?"))
                mid = g.get("parent_group_id")
            return " > ".join(reversed(parts))

        def fetch_type(tid):
            st, t, _ = esi.get(f"/universe/types/{tid}/", use_etag=False)
            if st != 200 or not t:
                return None
            dogma = {d["attribute_id"]: d["value"] for d in t.get("dogma_attributes", [])}
            mg = dogma.get(1692)
            ml = dogma.get(633)
            g = get_group(t["group_id"]) if t.get("group_id") else {}
            cat = g.get("category_id")
            cls = classify(t["name"], cat, g.get("name"), int(mg) if mg is not None else None,
                           int(ml) if ml is not None else None, t.get("published"))
            return dict(type_id=tid, name=t["name"], group_id=t.get("group_id"), group_name=g.get("name"),
                        category_id=cat, market_group_path=mg_path(t.get("market_group_id")),
                        meta_group_id=int(mg) if mg is not None else None,
                        meta_level=int(ml) if ml is not None else None,
                        published=t.get("published"), volume=t.get("volume"), **cls)

        rows, errors = [], 0
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(fetch_type, tid): tid for tid in ids}
            for i, f in enumerate(as_completed(futs), 1):
                try:
                    r = f.result()
                    if r:
                        rows.append(r)
                except Exception as e:
                    errors += 1
                    if errors < 10:
                        log("feil på type", futs[f], e)
                if i % 1000 == 0:
                    log(f"{i}/{len(ids)} hentet, ratelimit {esi.ratelimit_remaining}")

        with conn.cursor() as cur:
            cur.executemany(
                """insert into jita.types (type_id, name, group_id, group_name, category_id, market_group_path,
                     meta_group_id, meta_level, published, volume,
                     is_t1, is_meta, is_t2, is_faction, is_ship, is_excluded, updated_at)
                   values (%(type_id)s, %(name)s, %(group_id)s, %(group_name)s, %(category_id)s, %(market_group_path)s,
                     %(meta_group_id)s, %(meta_level)s, %(published)s, %(volume)s,
                     %(is_t1)s, %(is_meta)s, %(is_t2)s, %(is_faction)s, %(is_ship)s, %(is_excluded)s, now())
                   on conflict (type_id) do update set
                     name = excluded.name, group_id = excluded.group_id, group_name = excluded.group_name,
                     category_id = excluded.category_id, market_group_path = excluded.market_group_path,
                     meta_group_id = excluded.meta_group_id, meta_level = excluded.meta_level,
                     published = excluded.published, volume = excluded.volume,
                     is_t1 = excluded.is_t1, is_meta = excluded.is_meta, is_t2 = excluded.is_t2,
                     is_faction = excluded.is_faction, is_ship = excluded.is_ship,
                     is_excluded = excluded.is_excluded, updated_at = now()""",
                rows)
        conn.commit()
        log(f"{len(rows)} typer skrevet, {errors} feil")

        # ── Systemer i The Forge + hopp fra Jita ─────────────────────────────
        _, region, _ = esi.get(f"/universe/regions/{REGION_FORGE}/", use_etag=False)
        systems = []
        for cid in region["constellations"]:
            _, c, _ = esi.get(f"/universe/constellations/{cid}/", use_etag=False)
            systems.extend(c["systems"])

        def sysinfo(sid):
            _, s, _ = esi.get(f"/universe/systems/{sid}/", use_etag=False)
            if sid == JITA_SYSTEM:
                jumps = 0
            else:
                st, route, _ = esi.get(f"/route/{JITA_SYSTEM}/{sid}/", use_etag=False, legacy=True)   # /route/ finnes bare uten compat-date
                jumps = (len(route) - 1) if st == 200 and route else 99
            return dict(system_id=sid, name=s["name"], jumps_from_jita=jumps)

        srows = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for r in ex.map(sysinfo, systems):
                srows.append(r)
        with conn.cursor() as cur:
            cur.executemany(
                """insert into jita.systems (system_id, name, jumps_from_jita) values (%(system_id)s, %(name)s, %(jumps_from_jita)s)
                   on conflict (system_id) do update set name = excluded.name, jumps_from_jita = excluded.jumps_from_jita""",
                srows)
        conn.commit()
        log(f"{len(srows)} systemer skrevet")

        # ── Kontroll ─────────────────────────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute("""select name, is_t1, is_t2, is_meta, is_excluded, is_faction, meta_group_id, meta_level
                           from jita.types where name in ('Tracking Speed Script','Amarr Shuttle','Damage Control I',
                           'Damage Control II','Relic Analyzer I') or name like 'Compact%' or name like '%Blueprint'
                           order by name limit 12""")
            for r in cur.fetchall():
                log("kontroll:", r)
            cur.execute("select count(*) filter (where is_t1), count(*) filter (where is_meta), count(*) filter (where is_t2), "
                        "count(*) filter (where is_faction), count(*) filter (where is_excluded) from jita.types")
            log("t1/meta/t2/faction/excluded:", cur.fetchone())
        runlog.ratelimit_remaining = esi.ratelimit_remaining
        runlog.finish(conn, ok=True, message=f"{len(rows)} typer, {len(srows)} systemer")
    except Exception as e:
        conn.rollback()
        fail(runlog, f"{type(e).__name__}: {e}", conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
