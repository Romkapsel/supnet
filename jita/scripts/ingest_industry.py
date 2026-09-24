"""
ingest_industry.py – industri-marginfinneren (steg 1: produksjon).

Rangerer hvilke T1-produkter det er verdt å produsere i Ylandoki og selge i Jita 4-4.
Kjøres daglig av GitHub Actions (jobb «industry»), og kan kjøres manuelt.

Gangen i jobben:
  1. Oppskrifter (SDE) → jita.blueprints + jita.blueprint_materials. Bare når de er tomme
     eller eldre enn 7 dager (eller --refresh-sde).
  2. ESI: adjusted_price for alle varer (EIV-grunnlaget) og kostnadsindeksen for systemet.
  3. Fuzzwork: Jita-priser for alle materialer og aktuelle produkter.
  4. Trinn A – regn ut kostnad, netto og margin for ALLE T1-produkter med oppskrift.
  5. Trinn B – for de beste (--enrich, standard 400): ESI-historikk → dagsvolum, svingning, prisfall.
  6. Trinn C – for de aller beste (--deep, standard 120): antall selgere i Jita og BPO-pris.
  7. Dom + score → jita.industry_candidates. Varsel hvis en vare du produserer faller under terskel.

Miljø: SUPABASE_DB_URL (kreves, med mindre --csv brukes alene), DISCORD_WEBHOOK (valgfri).

Eksempler:
    python jita/scripts/ingest_industry.py                     # full kjøring, skriver til databasen
    python jita/scripts/ingest_industry.py --csv topp.csv      # skriv også CSV med topp 30
    python jita/scripts/ingest_industry.py --dry-run --csv topp.csv   # ingenting skrives til databasen
    python jita/scripts/ingest_industry.py --refresh-sde       # hent oppskriftene på nytt
    python jita/scripts/ingest_industry.py --verify 5          # sammenlign topp 5 mot EVE Ref sitt kost-API
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import requests

from common import (Esi, JITA_44, REGION_FORGE, RunLog, USER_AGENT, db, fail, log, notify, now_utc)
from industry import (MANUFACTURING, PRODUCT_CATEGORIES, IndustryProfile, economics, job_budget,
                      judge, margin_at_me, market_units_cap, realistic_throughput,
                      start_recommendation)

FUZZWORK_AGG = "https://market.fuzzwork.co.uk/aggregates/"
EVEREF_COST = "https://api.everef.net/v1/industry/cost"
HOBOLEAKS_BLUEPRINTS = "https://sde.hoboleaks.space/tq/blueprints.json"   # CCPs eget format, 5 082 oppskrifter i én fil
EVEREF_BULK = "https://data.everef.net/reference-data/reference-data-latest.tar.xz"
CHUNK = 200                 # varer per Fuzzwork-kall (URL-lengde)


def http() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


# ── 1. Oppskrifter fra SDE ───────────────────────────────────────────────────
# Kilder (sjekket 24. sept 2026 med probe_sources.py):
#   1. Hoboleaks – hele blueprints.json i CCPs eget format (camelCase), 11,5 MB, én nedlasting.
#   2. EVE Ref sin bulkpakke – samme innhold i snake_case, pakket i tar.xz.
# Fuzzwork-dumpene brukes IKKE: filene ligger i en csv/-undermappe med datostemplede navn,
# så adressen kan ikke hardkodes. (market.fuzzwork.co.uk/aggregates er noe helt annet og virker.)
def parse_blueprints(data) -> dict[int, dict]:
    """Tar CCP-formatet (blueprintTypeID/typeID) og EVE Ref-formatet (blueprint_type_id/type_id)."""
    rows = data.values() if isinstance(data, dict) else data
    out: dict[int, dict] = {}
    for bp in rows:
        if not isinstance(bp, dict):
            continue
        bid = bp.get("blueprint_type_id") or bp.get("blueprintTypeID") or bp.get("type_id")
        acts = bp.get("activities") or {}
        man = acts.get("manufacturing")
        if not bid or not man:
            continue
        mats = _as_qty_map(man.get("materials"))
        prods = _as_qty_map(man.get("products"))
        if not mats or not prods:
            continue
        pid, units = next(iter(prods.items()))
        maks = bp.get("max_production_limit") or bp.get("maxProductionLimit")
        out[int(bid)] = dict(blueprint_type_id=int(bid), product_type_id=int(pid),
                             units_per_run=int(units or 1),
                             base_time_s=int(man.get("time") or 0),
                             max_runs=int(maks) if maks else None,
                             materials={int(k): float(v) for k, v in mats.items()})
    return out


def _as_qty_map(x) -> dict[int, float]:
    """Materialer/produkter kommer som [{typeID, quantity}] (CCP), [{type_id, quantity}] (EVE Ref)
    eller {type_id: {quantity}}."""
    if not x:
        return {}
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            q = v.get("quantity") if isinstance(v, dict) else v
            tid = (v.get("type_id") or v.get("typeID") if isinstance(v, dict) else None) or k
            out[int(tid)] = float(q or 0)
        return out
    out = {}
    for i in x:
        tid = i.get("type_id") or i.get("typeID")
        if tid:
            out[int(tid)] = float(i.get("quantity") or 0)
    return out


def sde_from_hoboleaks(s: requests.Session) -> dict[int, dict]:
    r = s.get(HOBOLEAKS_BLUEPRINTS, timeout=180)
    r.raise_for_status()
    return parse_blueprints(r.json())


def sde_from_everef_bulk(s: requests.Session) -> dict[int, dict]:
    """EVE Ref sin bulkpakke: tar.xz med blueprints.json inni."""
    import io as _io
    import tarfile
    r = s.get(EVEREF_BULK, timeout=300)
    r.raise_for_status()
    with tarfile.open(fileobj=_io.BytesIO(r.content), mode="r:xz") as tf:
        navn = next((m for m in tf.getnames() if m.endswith("blueprints.json")), None)
        if not navn:
            raise RuntimeError(f"fant ikke blueprints.json i pakken ({tf.getnames()[:5]}…)")
        with tf.extractfile(navn) as f:
            return parse_blueprints(json.load(f))


def load_sde(s: requests.Session) -> tuple[dict[int, dict], str]:
    errors = []
    for name, henter in (("hoboleaks", sde_from_hoboleaks), ("everef-bulk", sde_from_everef_bulk)):
        try:
            bps = henter(s)
            if bps:
                log(f"SDE fra {name}: {len(bps)} oppskrifter")
                return bps, name
            errors.append(f"{name}: tom")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__} {e}")
            log(f"SDE-kilde {name} feilet:", e)
    raise RuntimeError("fikk ikke oppskriftene fra noen kilde – " + " | ".join(errors))


def write_sde(conn, bps: dict[int, dict], source: str):
    with conn.cursor() as cur:
        cur.executemany(
            """insert into jita.blueprints (blueprint_type_id, product_type_id, units_per_run,
                 base_time_s, max_runs, source, updated_at)
               values (%(blueprint_type_id)s, %(product_type_id)s, %(units_per_run)s,
                 %(base_time_s)s, %(max_runs)s, %(source)s, now())
               on conflict (blueprint_type_id) do update set
                 product_type_id = excluded.product_type_id, units_per_run = excluded.units_per_run,
                 base_time_s = excluded.base_time_s, max_runs = excluded.max_runs,
                 source = excluded.source, updated_at = now()""",
            [dict(b, source=source) for b in bps.values()])
        # materialer: skriv på nytt for de oppskriftene vi nettopp hentet
        cur.execute("delete from jita.blueprint_materials where blueprint_type_id = any(%s)",
                    (list(bps),))
        cur.executemany(
            "insert into jita.blueprint_materials (blueprint_type_id, material_type_id, quantity) "
            "values (%s, %s, %s) on conflict do nothing",
            [(b["blueprint_type_id"], tid, q)
             for b in bps.values() for tid, q in b["materials"].items()])
    conn.commit()


def read_sde(conn) -> dict[int, dict]:
    with conn.cursor() as cur:
        cur.execute("""select blueprint_type_id, product_type_id, units_per_run, base_time_s, max_runs
                       from jita.blueprints""")
        bps = {r[0]: dict(blueprint_type_id=r[0], product_type_id=r[1], units_per_run=r[2],
                          base_time_s=r[3], max_runs=r[4], materials={}) for r in cur.fetchall()}
        cur.execute("select blueprint_type_id, material_type_id, quantity from jita.blueprint_materials")
        for bid, tid, q in cur.fetchall():
            if bid in bps:
                bps[bid]["materials"][tid] = float(q)
    return {k: v for k, v in bps.items() if v["materials"]}


def sde_age_days(conn) -> float | None:
    with conn.cursor() as cur:
        cur.execute("select extract(epoch from (now() - max(updated_at))) / 86400 from jita.blueprints")
        v = cur.fetchone()[0]
    return float(v) if v is not None else None


# ── 2. ESI: adjusted price og kostnadsindeks ─────────────────────────────────
def load_market_prices(esi: Esi, conn, dry: bool) -> tuple[dict[int, float], dict[int, float]]:
    st, body, _ = esi.get("/markets/prices/", use_etag=False)
    if st != 200 or not body:
        raise RuntimeError("fikk ikke /markets/prices/")
    adjusted = {int(r["type_id"]): float(r.get("adjusted_price") or 0) for r in body}
    average = {int(r["type_id"]): float(r.get("average_price") or 0) for r in body
               if r.get("average_price")}
    if not dry:
        with conn.cursor() as cur:
            cur.executemany(
                """insert into jita.market_prices (type_id, adjusted_price, average_price, updated_at)
                   values (%s, %s, %s, now())
                   on conflict (type_id) do update set adjusted_price = excluded.adjusted_price,
                     average_price = excluded.average_price, updated_at = now()""",
                [(int(r["type_id"]), r.get("adjusted_price"), r.get("average_price")) for r in body])
        conn.commit()
    log(f"adjusted_price for {len(adjusted)} varer, average_price for {len(average)}")
    return adjusted, average


def load_cost_index(esi: Esi, conn, system_id: int, dry: bool) -> float:
    st, body, _ = esi.get("/industry/systems/", use_etag=False)
    if st != 200 or not body:
        raise RuntimeError("fikk ikke /industry/systems/")
    rows, mine = [], 0.0
    for s in body:
        idx = {i["activity"]: float(i["cost_index"]) for i in s.get("cost_indices", [])}
        rows.append((s["solar_system_id"], idx.get("manufacturing"), idx.get("copying"),
                     idx.get("invention"), idx.get("reaction")))
        if s["solar_system_id"] == system_id:
            mine = idx.get("manufacturing") or 0.0
    if not dry:
        with conn.cursor() as cur:
            cur.executemany(
                """insert into jita.industry_systems (system_id, manufacturing, copying, invention, reaction, updated_at)
                   values (%s, %s, %s, %s, %s, now())
                   on conflict (system_id) do update set manufacturing = excluded.manufacturing,
                     copying = excluded.copying, invention = excluded.invention,
                     reaction = excluded.reaction, updated_at = now()""", rows)
        conn.commit()
    log(f"kostnadsindeks for system {system_id}: {mine:.4f} ({len(rows)} systemer lagret)")
    return mine


# ── 3. Fuzzwork: Jita-priser ─────────────────────────────────────────────────
def load_quotes(s: requests.Session, conn, type_ids: list[int], volumes: dict[int, float],
                dry: bool) -> dict[int, dict]:
    quotes: dict[int, dict] = {}
    ids = sorted(set(type_ids))
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        r = s.get(FUZZWORK_AGG, params={"station": JITA_44, "types": ",".join(map(str, chunk))}, timeout=60)
        r.raise_for_status()
        for k, v in r.json().items():
            tid = int(k)
            buy, sell = v.get("buy") or {}, v.get("sell") or {}
            quotes[tid] = dict(
                buy_max=float(buy.get("max") or 0) or None, buy_volume=int(float(buy.get("volume") or 0)),
                buy_orders=int(float(buy.get("orderCount") or 0)),
                sell_min=float(sell.get("min") or 0) or None, sell_volume=int(float(sell.get("volume") or 0)),
                sell_orders=int(float(sell.get("orderCount") or 0)),
                volume=volumes.get(tid))
        if (i // CHUNK) % 10 == 0:
            log(f"Jita-priser {min(i + CHUNK, len(ids))}/{len(ids)}")
    if not dry:
        with conn.cursor() as cur:
            cur.executemany(
                """insert into jita.market_quotes (type_id, buy_max, buy_volume, buy_orders,
                     sell_min, sell_volume, sell_orders, updated_at)
                   values (%s, %s, %s, %s, %s, %s, %s, now())
                   on conflict (type_id) do update set buy_max = excluded.buy_max,
                     buy_volume = excluded.buy_volume, buy_orders = excluded.buy_orders,
                     sell_min = excluded.sell_min, sell_volume = excluded.sell_volume,
                     sell_orders = excluded.sell_orders, updated_at = now()""",
                [(t, q["buy_max"], q["buy_volume"], q["buy_orders"],
                  q["sell_min"], q["sell_volume"], q["sell_orders"]) for t, q in quotes.items()])
        conn.commit()
    log(f"Jita-priser for {len(quotes)} varer")
    return quotes


# ── 5. ESI-historikk (dagsvolum, svingning, prisfall) ────────────────────────
def load_history(esi: Esi, conn, type_ids: list[int], dry: bool) -> dict[int, dict]:
    out: dict[int, dict] = {}
    rows_to_write: list[tuple] = []
    cutoff30 = date.today() - timedelta(days=30)
    cutoff90 = date.today() - timedelta(days=90)

    def one(tid: int):
        st, body, _ = esi.get(f"/markets/{REGION_FORGE}/history/", {"type_id": tid})
        return tid, (body if st in (200, 304) else None)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(one, t) for t in type_ids]
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                tid, body = fut.result()
            except Exception as e:
                log("historikk feilet:", e)
                continue
            if not body:
                continue
            days = []
            for d in body:
                try:
                    dd = date.fromisoformat(d["date"])
                except Exception:
                    continue
                if dd >= cutoff90:
                    days.append((dd, d))
                if dd >= cutoff30:
                    rows_to_write.append((tid, dd, d.get("average"), d.get("highest"),
                                          d.get("lowest"), d.get("volume"), d.get("order_count")))
            if not days:
                continue
            days.sort()
            last30 = [d for dd, d in days if dd >= cutoff30]
            vol30 = statistics.fmean([float(d.get("volume") or 0) for d in last30]) if last30 else 0.0
            vol90 = statistics.fmean([float(d.get("volume") or 0) for _, d in days]) if days else 0.0
            trades = [float(d.get("order_count") or 0) for d in last30]
            trades30 = statistics.fmean(trades) if trades else 0.0
            avgs = [float(d.get("average") or 0) for d in last30 if d.get("average")]
            avg30 = statistics.fmean(avgs) if avgs else None
            volat = (statistics.pstdev(avgs) / avg30) if avg30 and len(avgs) > 2 else None
            drop = None
            if len(avgs) >= 14:
                first, last = statistics.fmean(avgs[:7]), statistics.fmean(avgs[-7:])
                if first > 0:
                    drop = (first - last) / first
            out[tid] = dict(daily_volume=round(vol30, 2), daily_volume_90d=round(vol90, 2),
                            trades_per_day=round(trades30, 2),
                            price_avg_30d=avg30, price_volatility=volat, price_drop_30d=drop)
            if n % 100 == 0:
                log(f"historikk {n}/{len(type_ids)}")

    if not dry and rows_to_write:
        with conn.cursor() as cur:
            cur.executemany(
                """insert into jita.history_daily (type_id, date, average, highest, lowest, volume, order_count)
                   values (%s, %s, %s, %s, %s, %s, %s)
                   on conflict (type_id, date) do update set average = excluded.average,
                     highest = excluded.highest, lowest = excluded.lowest,
                     volume = excluded.volume, order_count = excluded.order_count""", rows_to_write)
            cur.execute("update jita.types set history_fetched_at = now() where type_id = any(%s)",
                        (list(out),))
        conn.commit()
    log(f"historikk for {len(out)} varer")
    return out


# ── 6. Konkurrenter i Jita og BPO-pris ───────────────────────────────────────
def load_competition(esi: Esi, type_ids: list[int]) -> dict[int, int]:
    """Antall salgsordrer i Jita 4-4 per vare (konkurrentene du legger deg under)."""
    out: dict[int, int] = {}

    def one(tid: int):
        st, body, _ = esi.get(f"/markets/{REGION_FORGE}/orders/",
                              {"type_id": tid, "order_type": "sell"})
        if st not in (200, 304) or not body:
            return tid, None
        return tid, sum(1 for o in body if o.get("location_id") == JITA_44)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for tid, n in ex.map(one, type_ids):
            if n is not None:
                out[tid] = n
    log(f"selgere i Jita for {len(out)} varer")
    return out


# NPC-seedede BPO-er ligger i NPC-stasjoner spredt over empire, ikke bare i The Forge – et
# region-oppslag mot Jita fant pris på 54 av 120 og ingen av de beste. Vi spør derfor
# Fuzzwork-aggregatet for de fem store knutene og tar laveste sell.
HUBS = {60003760: "Jita", 60008494: "Amarr", 60011866: "Dodixie", 60004588: "Rens", 60005686: "Hek"}


def load_bpo(esi: Esi, s: requests.Session, conn, blueprint_ids: list[int], dry: bool) -> dict[int, dict]:
    """BPO-pris (laveste sell i de store knutene) og NPC-flagg (salgsordre med ≥ 365 dagers
    varighet finnes bare fra NPC – samme kjennetegn som timesjobben bruker)."""
    out: dict[int, dict] = {}
    ids = sorted(set(blueprint_ids))
    for station, navn in HUBS.items():
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            try:
                r = s.get(FUZZWORK_AGG, params={"station": station, "types": ",".join(map(str, chunk))}, timeout=60)
                r.raise_for_status()
            except Exception as e:
                log(f"BPO-priser fra {navn} feilet:", e)
                continue
            for k, v in r.json().items():
                pris = float((v.get("sell") or {}).get("min") or 0)
                if pris <= 0:
                    continue
                bid = int(k)
                if bid not in out or pris < out[bid]["bpo_price"]:
                    out[bid] = dict(bpo_price=pris, npc_bpo=None, bpo_price_source=navn.lower())

    # NPC-flagget: sjekk varigheten på salgsordrene i The Forge for dem vi fant pris på
    def npc_check(bid: int):
        st, body, _ = esi.get(f"/markets/{REGION_FORGE}/orders/", {"type_id": bid, "order_type": "sell"})
        if st not in (200, 304) or not body:
            return bid, None
        return bid, any(int(o.get("duration") or 0) >= 365 for o in body)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for bid, npc in ex.map(npc_check, list(out)):
            if npc is not None:
                out[bid]["npc_bpo"] = npc
    if not dry and out:
        with conn.cursor() as cur:
            cur.executemany(
                """update jita.blueprints set bpo_price = %s, npc_bpo = %s, bpo_price_source = %s,
                     bpo_checked_at = now() where blueprint_type_id = %s""",
                [(v["bpo_price"], v["npc_bpo"], v["bpo_price_source"], b) for b, v in out.items()])
        conn.commit()
    log(f"BPO-pris for {len(out)} oppskrifter")
    return out


# ── Profil ───────────────────────────────────────────────────────────────────
def load_industry_profile(conn) -> IndustryProfile:
    with conn.cursor() as cur:
        cur.execute("select * from jita.industry_profile where id = 1")
        cols = [d.name for d in cur.description]
        row = dict(zip(cols, cur.fetchone() or []))
        cur.execute("select * from jita.profile_calc(jita.effective_profile())")
        ccols = [d.name for d in cur.description]
        calc = dict(zip(ccols, cur.fetchone() or []))
    fields = IndustryProfile.__dataclass_fields__
    kw = {k: v for k, v in row.items() if k in fields and v is not None}
    for k in ("facility_tax", "scc_rate", "sell_fee_override"):
        if k in kw:
            kw[k] = float(kw[k])
    p = IndustryProfile(**kw)
    p.broker = float(calc.get("broker") or 0.01)
    p.tax = float(calc.get("tax") or 0.075)
    with conn.cursor() as cur:
        cur.execute("select coalesce((jita.effective_profile()->>'cash_isk')::float8, "
                    "(jita.effective_profile()->>'capital_isk')::float8, 0)")
        p.capital_isk = float(cur.fetchone()[0] or 0)
    if row.get("thresholds"):
        p.thresholds = row["thresholds"]
    return p


def load_types(conn) -> dict[int, dict]:
    with conn.cursor() as cur:
        cur.execute("""select type_id, name, category_id, group_name, volume, is_t1, is_t2,
                              is_excluded, published from jita.types""")
        return {r[0]: dict(type_id=r[0], name=r[1], category_id=r[2], group_name=r[3],
                           volume=float(r[4]) if r[4] is not None else None,
                           is_t1=r[5], is_t2=r[6], is_excluded=r[7], published=r[8])
                for r in cur.fetchall()}


# ── EVE Ref-kryssjekk (frivillig, logges) ────────────────────────────────────
def verify_against_everef(s: requests.Session, rows: list[dict], p: IndustryProfile, n: int):
    """Sammenligner våre tall mot EVE Ref sitt kost-API for de n beste. Bare logging –
    små avvik er normalt (ESI oppgir kostnadsindeksen med 4 desimaler)."""
    for r in rows[:n]:
        try:
            resp = s.get(EVEREF_COST, params={
                "product_id": r["product_type_id"], "runs": r["runs"], "me": p.me, "te": p.te,
                "system_id": p.system_id, "facility_tax": p.facility_tax,
                "industry": p.industry, "advanced_industry": p.advanced_industry}, timeout=60)
            if resp.status_code != 200:
                log(f"kryssjekk {r['product_type_id']}: HTTP {resp.status_code}")
                continue
            man = (resp.json().get("manufacturing") or {})
            got = man.get(str(r["product_type_id"])) or (next(iter(man.values())) if man else None)
            if not got:
                continue
            theirs = float(got.get("total_cost_per_unit") or got.get("total_cost", 0) / max(r["units"], 1))
            ours = r["cost_per_unit"]
            diff = (ours - theirs) / theirs * 100 if theirs else 0
            log(f"kryssjekk {r.get('name', r['product_type_id'])}: vår {ours:,.2f} mot EVE Ref "
                f"{theirs:,.2f} ISK/stk ({diff:+.1f} %)")
        except Exception as e:
            log("kryssjekk feilet:", e)


# ── Hovedløpet ───────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="skriv topp-tabellen til denne fila")
    ap.add_argument("--csv-rows", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="ikke skriv kandidater til databasen")
    ap.add_argument("--refresh-sde", action="store_true", help="hent oppskriftene på nytt")
    ap.add_argument("--enrich", type=int, default=400, help="hvor mange som får historikk (trinn B)")
    ap.add_argument("--deep", type=int, default=120, help="hvor mange som får selgere + BPO (trinn C)")
    ap.add_argument("--verify", type=int, default=0, help="kryssjekk N beste mot EVE Ref")
    args = ap.parse_args()

    runlog = RunLog("industry")
    s = http()
    esi = Esi()
    conn = db()
    try:
        p = load_industry_profile(conn)
        log(f"produserer i {p.system_name} ({p.system_id}), ME {p.me}/TE {p.te}, "
            f"{p.slot_count} slot(s), materialer fra {p.material_source}-side, "
            f"salgsgebyr {p.sell_fees * 100:.2f} %, kapital {p.capital_isk:,.0f} ISK "
            f"→ budsjett per jobb {job_budget(p):,.0f} ISK")

        # 1. oppskrifter
        age = sde_age_days(conn)
        if args.refresh_sde or age is None or age > 7:
            bps, src = load_sde(s)
            if not args.dry_run:
                write_sde(conn, bps, src)
        else:
            bps = read_sde(conn)
            log(f"bruker lagrede oppskrifter ({len(bps)}, {age:.1f} dager gamle)")
        if not bps:
            raise RuntimeError("ingen oppskrifter tilgjengelig")

        # produktfilter: T1, publisert, riktig kategori, finnes på markedet i The Forge
        types = load_types(conn)
        cands = {}
        for bid, b in bps.items():
            t = types.get(b["product_type_id"])
            if not t or not t["is_t1"] or t["is_excluded"] or t["published"] is False:
                continue
            if t["category_id"] not in PRODUCT_CATEGORIES:
                continue
            bt = types.get(bid)
            if bt and (bt["is_t2"] or bt["category_id"] != 9):
                continue                      # T2-blueprint: kommer fra invention, ikke fra NPC
            b["blueprint_on_market"] = bt is not None
            cands[bid] = b
        log(f"{len(cands)} T1-produkter med oppskrift (av {len(bps)} oppskrifter)")
        if not cands:
            raise RuntimeError("ingen T1-produkter å vurdere – er jita.types fylt (seed_types.py)?")

        # 2. ESI
        adjusted, adjusted_avg = load_market_prices(esi, conn, args.dry_run)
        p.cost_index = load_cost_index(esi, conn, p.system_id, args.dry_run)

        # 3. Jita-priser for produkter + alle materialer
        needed = {b["product_type_id"] for b in cands.values()}
        needed |= {tid for b in cands.values() for tid in b["materials"]}
        volumes = {tid: t["volume"] for tid, t in types.items()}
        quotes = load_quotes(s, conn, sorted(needed), volumes, args.dry_run)

        # 4. trinn A – kostnad og margin for alle
        rows: list[dict] = []
        for b in cands.values():
            r = economics(b, p, quotes, adjusted)
            if not r:
                continue
            q = quotes.get(r["product_type_id"]) or {}
            r["sell_orders"] = q.get("sell_orders")
            r["name"] = types[r["product_type_id"]]["name"]
            r["blueprint_on_market"] = b.get("blueprint_on_market")
            r.update(realistic_throughput(r, None, p))
            rows.append(r)
        log(f"trinn A: {len(rows)} produkter med komplett pris "
            f"({sum(1 for r in rows if r['margin'] >= float(p.t('min_margin', 0.10)))} over marginterskelen)")

        # 5. trinn B – historikk for de beste
        rows.sort(key=lambda r: r["isk_per_day_slot"], reverse=True)
        enrich = [r for r in rows if r["margin"] >= float(p.t("min_margin", 0.10))][:args.enrich]
        hist = load_history(esi, conn, [r["product_type_id"] for r in enrich], args.dry_run)
        for r in rows:
            h = hist.get(r["product_type_id"])
            if not h:
                continue
            r.update(h)
            # Nå vet vi hvor mye markedet spiser: regn batchen på nytt med det taket.
            cap = market_units_cap(h["daily_volume"], p)
            if cap:
                b = cands[r["blueprint_type_id"]]
                ny = economics(b, p, quotes, adjusted, max_units=cap)
                if ny:
                    ny["name"] = r["name"]
                    ny["sell_orders"] = r.get("sell_orders")
                    ny["blueprint_on_market"] = r.get("blueprint_on_market")
                    r.update({k: v for k, v in ny.items() if k != "factors"})
                    r.update(h)
            r.update(realistic_throughput(r, h["daily_volume"], p))

        # 6. trinn C – selgere og BPO for de aller beste
        rows.sort(key=lambda r: r["isk_per_day_slot"] if r.get("daily_volume") else -1, reverse=True)
        deep = [r for r in rows if r.get("daily_volume")][:args.deep]
        comp = load_competition(esi, [r["product_type_id"] for r in deep])
        bpo = load_bpo(esi, s, conn, [r["blueprint_type_id"] for r in deep], args.dry_run)
        # Handelsknutene har bare halvparten av BPO-ene – NPC seeder dem spredt i empire.
        # ESI-ens average_price er selve NPC-prisen der vi har begge (sjekket 24. sept), så den
        # er en god reserve. Uten den mangler 50 av 51 forslag startkostnad, og da kan verktøyet
        # ikke svare på hva du bør kjøpe først.
        for r in rows:
            if r["product_type_id"] in comp:
                r["sell_orders"] = comp[r["product_type_id"]]
            info = bpo.get(r["blueprint_type_id"])
            if info:
                r["bpo_price"] = info["bpo_price"]
                r["bpo_price_source"] = info["bpo_price_source"]
                r["npc_bpo"] = info["npc_bpo"]
            elif adjusted_avg.get(r["blueprint_type_id"]):
                r["bpo_price"] = adjusted_avg[r["blueprint_type_id"]]
                r["bpo_price_source"] = "esi_average"
            if r.get("bpo_price") and r["isk_per_day_slot"] > 0:
                r["payback_days"] = round(r["bpo_price"] / r["isk_per_day_slot"], 2)
            if r.get("bpo_price"):
                r["startup_cost"] = round(r["bpo_price"] + (r.get("capital_per_job") or 0), 2)
            # Marginen med en NYKJØPT (uforsket) BPO – avgjørende for hva du bør starte med
            r["margin_me0"] = margin_at_me(r, cands[r["blueprint_type_id"]], p, quotes, 0)

        # 7. dom
        for r in rows:
            judge(r, p)
        rows.sort(key=lambda r: r["score"] or 0, reverse=True)
        passed = [r for r in rows if r["passed"]]
        log(f"dom: {len(passed)} passerer av {len(rows)} vurdert")
        for r in rows[:10]:
            log(f"  {r['name']}: {r['isk_per_day_slot']:,.0f} ISK/dag/slot, margin "
                f"{r['margin'] * 100:.1f} %, volum {r.get('daily_volume') or '–'}"
                f"{'' if r['passed'] else ' [' + ','.join(r['failed_rules']) + ']'}")

        anbefaling = start_recommendation(rows, p, p.capital_isk)
        if anbefaling:
            log("start med (raad til BPO + batch, og loennsom alt ved ME 0):")
            for a in anbefaling:
                log(f"  {a['name']}: BPO {a['bpo_price']:,.0f} ({a['bpo_price_source']}) + batch "
                    f"{a['capital_per_job']:,.0f} = {a['startup_cost']:,.0f} ISK start, "
                    f"{a['isk_per_day_slot']:,.0f} ISK/dag, margin {a['margin'] * 100:.0f} % "
                    f"(ME 0: {a['margin_me0'] * 100:.0f} %)")
        else:
            log("ingen vare har baade raad-til-startkostnad og margin ved ME 0")

        if args.verify:
            verify_against_everef(s, rows, p, args.verify)

        run_at = now_utc()
        if not args.dry_run:
            write_candidates(conn, run_at, rows)
            check_margins(conn, p, rows)
        if args.csv:
            write_csv(args.csv, rows[:args.csv_rows])

        runlog.ratelimit_remaining = esi.ratelimit_remaining
        runlog.orders_count = len(rows)
        esi.save_etags()
        runlog.finish(conn, ok=True,
                      message=f"{len(rows)} vurdert, {len(passed)} passerer, indeks {p.cost_index:.4f}")
    except Exception as e:
        conn.rollback()
        fail(runlog, f"{type(e).__name__}: {e}", conn)
    finally:
        conn.close()


COLS = ("product_type_id", "blueprint_type_id", "runs", "units", "units_per_run", "material_cost",
        "job_cost", "eiv", "total_cost", "cost_per_unit", "sell_price", "net_per_unit", "margin",
        "time_per_run_s", "time_per_batch_s", "units_per_day_slot", "daily_volume", "daily_volume_90d",
        "realistic_units_per_day", "isk_per_day_slot", "isk_per_hour_slot", "capital_per_job",
        "bpo_price", "bpo_price_source", "startup_cost", "margin_me0", "payback_days",
        "sell_orders", "price_avg_30d", "price_drop_30d",
        "price_volatility", "m3_in", "m3_out", "score", "passed", "failed_rules", "reason")


def write_candidates(conn, run_at, rows: list[dict]):
    with conn.cursor() as cur:
        cur.executemany(
            f"""insert into jita.industry_candidates (run_at, {', '.join(COLS)}, factors)
                values (%s, {', '.join(['%s'] * len(COLS))}, %s)
                on conflict (run_at, product_type_id) do nothing""",
            [tuple([run_at] + [r.get(c) for c in COLS] + [json.dumps(r.get("factors") or {})])
             for r in rows])
    conn.commit()
    log(f"{len(rows)} rader skrevet til jita.industry_candidates ({run_at:%Y-%m-%d %H:%M} UTC)")


def check_margins(conn, p: IndustryProfile, rows: list[dict]):
    """Varsel når en vare du faktisk har i hangaren eller har solgt nylig faller under marginterskelen."""
    min_margin = float(p.t("min_margin", 0.10))
    by_id = {r["product_type_id"]: r for r in rows}
    with conn.cursor() as cur:
        cur.execute("""select distinct type_id from jita.my_assets where location_id = 60003760
                       union
                       select distinct type_id from jita.my_transactions
                       where not is_buy and date > now() - interval '30 days'""")
        mine = [r[0] for r in cur.fetchall()]
    hits = []
    for tid in mine:
        r = by_id.get(tid)
        if r and r["margin"] < min_margin:
            hits.append(r)
            with conn.cursor() as cur:
                cur.execute(
                    """insert into jita.alerts (kind, type_id, payload)
                       select 'industry_margin', %s, %s
                       where not exists (select 1 from jita.alerts
                         where kind = 'industry_margin' and type_id = %s
                           and created_at > now() - interval '24 hours')""",
                    (tid, json.dumps(dict(
                        text=f"{r['name']}: margin {r['margin'] * 100:.1f} % (< {min_margin * 100:.0f} %) "
                             f"– netto {r['net_per_unit']:,.0f} ISK/stk",
                        margin=r["margin"], net_per_unit=r["net_per_unit"],
                        cost_per_unit=r["cost_per_unit"], sell_price=r["sell_price"])), tid))
            conn.commit()
    if hits:
        notify("📉 Industri: margin under terskel på " + ", ".join(r["name"] for r in hits[:5]))


def write_csv(path: str, rows: list[dict]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["navn"] + list(COLS))
        for r in rows:
            w.writerow([r.get("name")] + [
                ",".join(r.get(c) or []) if c == "failed_rules" else r.get(c) for c in COLS])
    log(f"CSV skrevet: {path} ({len(rows)} rader)")


if __name__ == "__main__":
    main()
