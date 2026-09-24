"""
ingest_mining.py – mining-laget (steg 2): hvilken malm gir mest ISK per time?

Kjøres rett etter industri-jobben (samme Actions-jobb), og kan kjøres alene.

Gangen:
  1. Refine-utbytte fra SDE → jita.ore_yields. Bare når de er tomme eller > 7 dager gamle.
     Kilde: sde.hoboleaks.space/tq/typematerials.json ({typeID: {materials: [{typeID, quantity}]}}),
     med EVE Refs bulkpakke som reserve. Batch-størrelsen (portionSize) hentes fra bulkpakkens types.json.
  2. Jita-priser for all malm og alle mineraler den gir (Fuzzwork-aggregat).
  3. ESI-historikk for malmen selv (bare rå-salg er avhengig av at malmmarkedet flyter).
  4. Regn ut verdi per m3 refinet og rått, velg beste vei, og skriv jita.mining_candidates.

Miljø: SUPABASE_DB_URL. Eksempler:
    python jita/scripts/ingest_mining.py
    python jita/scripts/ingest_mining.py --csv malm.csv --dry-run
    python jita/scripts/ingest_mining.py --refresh-sde
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import requests

from common import Esi, JITA_44, REGION_FORGE, RunLog, USER_AGENT, db, fail, log, now_utc
from mining import ASTEROID_CATEGORY, MiningProfile, evaluate, judge

HOBOLEAKS_TYPEMATERIALS = "https://sde.hoboleaks.space/tq/typematerials.json"
EVEREF_BULK = "https://data.everef.net/reference-data/reference-data-latest.tar.xz"
FUZZWORK_AGG = "https://market.fuzzwork.co.uk/aggregates/"
CHUNK = 200


def http() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


# ── 1. Refine-utbytte og batch-størrelse ─────────────────────────────────────
def bulk_json(s: requests.Session, navn: str) -> dict:
    """Henter én fil ut av EVE Refs bulkpakke (tar.xz)."""
    r = s.get(EVEREF_BULK, timeout=300)
    r.raise_for_status()
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:xz") as tf:
        medlem = next((m for m in tf.getnames() if m.endswith(navn)), None)
        if not medlem:
            raise RuntimeError(f"fant ikke {navn} i bulkpakken ({tf.getnames()[:5]}…)")
        with tf.extractfile(medlem) as f:
            return json.load(f)


def _plukk(d: dict, *navn):
    """Henter en nøkkel uansett skrivemåte (typeID / type_id / materialTypeID …)."""
    lav = {str(k).lower(): v for k, v in d.items()}
    for n in navn:
        if n.lower() in lav:
            return lav[n.lower()]
    return None


def parse_typematerials(data) -> dict[int, dict[int, float]]:
    """{typeID: {materialTypeID: mengde per batch}}.

    Tåler CCP-formen ({"34": {"materials": [{"materialTypeID": 34, "quantity": 415}]}}),
    EVE Ref-formen (snake_case) og lister. Kaster feil med eksempeldata hvis ingenting
    kan tolkes – en tom parsering uten forklaring kostet en kjøring 24. sept.
    """
    rows = list(data.items()) if isinstance(data, dict) else \
        [(_plukk(d, "typeID", "type_id"), d) for d in data]
    ut: dict[int, dict[int, float]] = {}
    for tid, v in rows:
        if tid is None or not isinstance(v, dict):
            continue
        mats = _plukk(v, "materials", "type_materials") or []
        if isinstance(mats, dict):
            mats = list(mats.values())
        m = {}
        for rad in mats:
            if not isinstance(rad, dict):
                continue
            mid = _plukk(rad, "materialTypeID", "typeID", "material_type_id", "type_id")
            qty = _plukk(rad, "quantity", "qty")
            if mid and qty:
                m[int(mid)] = float(qty)
        if m:
            ut[int(tid)] = m
    if not ut and rows:
        tid, v = rows[0]
        mats = _plukk(v, "materials", "type_materials") if isinstance(v, dict) else None
        eksempel = mats[0] if isinstance(mats, list) and mats else mats
        raise RuntimeError(f"klarte ikke tolke refine-utbyttene: {len(rows)} rader, "
                           f"første nøkler {list(v)[:6] if isinstance(v, dict) else type(v).__name__}, "
                           f"første material {eksempel}")
    return ut


def load_yields(s: requests.Session, ore_ids: list[int]) -> tuple[dict[int, dict[int, float]], dict[int, int], str]:
    """→ (utbytte per malm, batch-størrelse per malm, kildenavn)"""
    feil = []
    utbytte: dict[int, dict[int, float]] = {}
    kilde = ""
    try:
        r = s.get(HOBOLEAKS_TYPEMATERIALS, timeout=180)
        r.raise_for_status()
        utbytte = parse_typematerials(r.json())
        kilde = "hoboleaks"
        log(f"refine-utbytte fra hoboleaks: {len(utbytte)} varetyper")
    except Exception as e:
        feil.append(f"hoboleaks: {type(e).__name__} {e}")
        log("hoboleaks feilet:", e)

    typer = {}
    try:
        typer = bulk_json(s, "types.json")
    except Exception as e:
        feil.append(f"everef-bulk: {type(e).__name__} {e}")
        log("bulkpakken feilet:", e)

    if not utbytte and typer:
        # Reserve: bulkpakken har utbyttene i type_materials.json
        try:
            utbytte = parse_typematerials(bulk_json(s, "type_materials.json"))
            kilde = "everef-bulk"
            log(f"refine-utbytte fra bulkpakken: {len(utbytte)} varetyper")
        except Exception as e:
            feil.append(f"everef-bulk type_materials: {type(e).__name__} {e}")

    if not utbytte:
        raise RuntimeError("fikk ikke refine-utbytte fra noen kilde – " + " | ".join(feil))

    batch: dict[int, int] = {}
    for tid in ore_ids:
        t = (typer or {}).get(str(tid)) or (typer or {}).get(tid) or {}
        ps = t.get("portion_size") or t.get("portionSize")
        if ps:
            batch[tid] = int(ps)
    log(f"batch-størrelse for {len(batch)} av {len(ore_ids)} malmtyper")
    return utbytte, batch, kilde


def write_yields(conn, utbytte: dict[int, dict[int, float]], batch: dict[int, int],
                 ore_ids: list[int], kilde: str):
    rader = [(oid, mid, qty, batch.get(oid, 100), kilde)
             for oid in ore_ids for mid, qty in (utbytte.get(oid) or {}).items()]
    if not rader:
        return
    with conn.cursor() as cur:
        cur.execute("delete from jita.ore_yields where ore_type_id = any(%s)", (ore_ids,))
        cur.executemany(
            """insert into jita.ore_yields (ore_type_id, mineral_type_id, quantity, batch_size, source, updated_at)
               values (%s, %s, %s, %s, %s, now())
               on conflict (ore_type_id, mineral_type_id) do update set
                 quantity = excluded.quantity, batch_size = excluded.batch_size,
                 source = excluded.source, updated_at = now()""", rader)
    conn.commit()
    log(f"{len(rader)} utbytte-rader skrevet")


def read_yields(conn) -> tuple[dict[int, dict[int, float]], dict[int, int]]:
    with conn.cursor() as cur:
        cur.execute("select ore_type_id, mineral_type_id, quantity, batch_size from jita.ore_yields")
        utbytte: dict[int, dict[int, float]] = {}
        batch: dict[int, int] = {}
        for oid, mid, qty, bs in cur.fetchall():
            utbytte.setdefault(oid, {})[mid] = float(qty)
            batch[oid] = int(bs)
    return utbytte, batch


def yields_age_days(conn) -> float | None:
    with conn.cursor() as cur:
        cur.execute("select extract(epoch from (now() - max(updated_at))) / 86400 from jita.ore_yields")
        v = cur.fetchone()[0]
    return float(v) if v is not None else None


# ── 2. Priser ────────────────────────────────────────────────────────────────
def load_quotes(s: requests.Session, conn, type_ids: list[int], dry: bool) -> dict[int, dict]:
    quotes: dict[int, dict] = {}
    ids = sorted(set(type_ids))
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        r = s.get(FUZZWORK_AGG, params={"station": JITA_44, "types": ",".join(map(str, chunk))}, timeout=60)
        r.raise_for_status()
        for k, v in r.json().items():
            buy, sell = v.get("buy") or {}, v.get("sell") or {}
            quotes[int(k)] = dict(
                buy_max=float(buy.get("max") or 0) or None, buy_orders=int(float(buy.get("orderCount") or 0)),
                sell_min=float(sell.get("min") or 0) or None, sell_orders=int(float(sell.get("orderCount") or 0)))
    if not dry:
        with conn.cursor() as cur:
            cur.executemany(
                """insert into jita.market_quotes (type_id, buy_max, buy_orders, sell_min, sell_orders, updated_at)
                   values (%s, %s, %s, %s, %s, now())
                   on conflict (type_id) do update set buy_max = excluded.buy_max,
                     buy_orders = excluded.buy_orders, sell_min = excluded.sell_min,
                     sell_orders = excluded.sell_orders, updated_at = now()""",
                [(t, q["buy_max"], q["buy_orders"], q["sell_min"], q["sell_orders"]) for t, q in quotes.items()])
        conn.commit()
    log(f"Jita-priser for {len(quotes)} varer (malm + mineraler)")
    return quotes


# ── 3. Historikk for malmen selv ─────────────────────────────────────────────
def load_ore_history(esi: Esi, type_ids: list[int]) -> dict[int, dict]:
    ut: dict[int, dict] = {}
    cutoff = date.today() - timedelta(days=30)

    def one(tid: int):
        st, body, _ = esi.get(f"/markets/{REGION_FORGE}/history/", {"type_id": tid})
        return tid, (body if st in (200, 304) else None)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(one, t) for t in type_ids]):
            try:
                tid, body = fut.result()
            except Exception as e:
                log("malmhistorikk feilet:", e)
                continue
            if not body:
                continue
            siste = [d for d in body if date.fromisoformat(d["date"]) >= cutoff]
            if not siste:
                continue
            ut[tid] = dict(
                ore_daily_volume=round(statistics.fmean([float(d.get("volume") or 0) for d in siste]), 2),
                ore_trades_per_day=round(statistics.fmean([float(d.get("order_count") or 0) for d in siste]), 2))
    log(f"malmhistorikk for {len(ut)} typer")
    return ut


# ── Profil og malmliste ──────────────────────────────────────────────────────
def load_mining_profile(conn) -> MiningProfile:
    with conn.cursor() as cur:
        cur.execute("select * from jita.mining_profile where id = 1")
        cols = [d.name for d in cur.description]
        row = dict(zip(cols, cur.fetchone() or []))
        cur.execute("select * from jita.profile_calc(jita.effective_profile())")
        ccols = [d.name for d in cur.description]
        calc = dict(zip(ccols, cur.fetchone() or []))
    p = MiningProfile(
        reprocess_yield=float(row.get("reprocess_yield") or 0.52),
        m3_per_hour=float(row.get("m3_per_hour") or 3000),
        jumps_from_jita=int(row.get("jumps_from_jita") or 3),
        broker=float(calc.get("broker") or 0.01), tax=float(calc.get("tax") or 0.075),
        thresholds=row.get("thresholds") or {})
    return p


KOMPRIMERT_PREFIKS = ("compressed ", "batch compressed ")


def load_ores(conn) -> tuple[dict[int, dict], dict[int, dict]]:
    """→ (rå malm, komprimerte varianter). Bare rå malm rangeres – du miner rå malm,
    komprimering skjer etterpå. Koblingen mellom dem går på navn («Compressed Veldspar»)."""
    with conn.cursor() as cur:
        cur.execute("""select type_id, name, group_name, volume from jita.types
                       where category_id = %s and coalesce(published, true) and volume > 0""",
                    (ASTEROID_CATEGORY,))
        alle = [dict(type_id=r[0], name=r[1], group_name=r[2], volume=float(r[3])) for r in cur.fetchall()]

    rå, komp_etter_navn = {}, {}
    for t in alle:
        lav = (t["name"] or "").lower()
        prefiks = next((pr for pr in KOMPRIMERT_PREFIKS if lav.startswith(pr)), None)
        if prefiks:
            # «Batch Compressed» foretrekkes ikke over «Compressed» – vi tar den som gir mest per m3 senere
            komp_etter_navn.setdefault(lav[len(prefiks):], []).append(t)
        else:
            rå[t["type_id"]] = dict(ore_type_id=t["type_id"], name=t["name"],
                                    group_name=t["group_name"], volume=t["volume"])
    for oid, o in rå.items():
        o["compressed_kandidater"] = komp_etter_navn.get((o["name"] or "").lower(), [])
    return rå, {t["type_id"]: t for liste in komp_etter_navn.values() for t in liste}


# ── Hovedløpet ───────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="skriv tabellen til denne fila")
    ap.add_argument("--csv-rows", type=int, default=40)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh-sde", action="store_true")
    args = ap.parse_args()

    runlog = RunLog("mining")
    s = http()
    esi = Esi()
    conn = db()
    try:
        p = load_mining_profile(conn)
        log(f"mining: {p.reprocess_yield * 100:.0f} % refine-utbytte, {p.m3_per_hour:,.0f} m3/time, "
            f"{len(p.available_groups)} tilgjengelige grupper, salgsgebyr "
            f"broker {p.broker * 100:.2f} % + skatt {p.tax * 100:.2f} %")

        ores, komprimerte = load_ores(conn)
        if not ores:
            raise RuntimeError("ingen malmtyper i jita.types – er seed_types.py kjørt?")
        log(f"{len(ores)} rå malmtyper, {len(komprimerte)} komprimerte varianter")

        alle_ids = list(ores) + list(komprimerte)
        alder = yields_age_days(conn)
        if args.refresh_sde or alder is None or alder > 7:
            utbytte, batch, kilde = load_yields(s, alle_ids)
            if not args.dry_run:
                write_yields(conn, utbytte, batch, alle_ids, kilde)
        else:
            utbytte, batch = read_yields(conn)
            log(f"bruker lagrede utbytter ({len(utbytte)} varetyper, {alder:.1f} dager gamle)")

        for oid, o in ores.items():
            o["yields"] = utbytte.get(oid) or {}
            o["batch_size"] = batch.get(oid, 100)
            # velg den komprimerte varianten vi har både utbytte og volum for
            kandidater = [k for k in o.pop("compressed_kandidater", []) if utbytte.get(k["type_id"])]
            o["compressed"] = None
            if kandidater:
                k = kandidater[0]
                o["compressed"] = dict(type_id=k["type_id"], name=k["name"], volume=k["volume"],
                                       yields=utbytte.get(k["type_id"]) or {},
                                       batch_size=batch.get(k["type_id"], 1))

        # priser for rå malm, komprimerte varianter og alle mineralene
        trengs = set(ores) | set(komprimerte) | {mid for o in ores.values() for mid in o["yields"]}
        quotes = load_quotes(s, conn, sorted(trengs), args.dry_run)

        rader = []
        for o in ores.values():
            r = evaluate(o, p, quotes)
            if r:
                rader.append(r)
        log(f"{len(rader)} rå malmtyper med pris "
            f"({sum(1 for r in rader if r['compressed_type_id'])} med komprimert variant)")

        # Historikk for den varen du faktisk selger på beste vei (rå eller komprimert).
        # Refine-veien trenger den ikke – mineralmarkedet flyter alltid.
        trenger_hist = {r["market_type_id"] for r in rader if r["available"] and r.get("market_type_id")}
        hist = load_ore_history(esi, sorted(trenger_hist))
        for r in rader:
            h = hist.get(r.get("market_type_id")) or {}
            r["market_daily_volume"] = h.get("ore_daily_volume")
            r["market_trades_per_day"] = h.get("ore_trades_per_day")
            r["ore_daily_volume"] = hist.get(r["ore_type_id"], {}).get("ore_daily_volume")
            r["ore_trades_per_day"] = hist.get(r["ore_type_id"], {}).get("ore_trades_per_day")
            judge(r, p)

        rader.sort(key=lambda r: r["score"], reverse=True)
        passerer = [r for r in rader if r["passed"]]
        log(f"{len(passerer)} malmtyper er aktuelle der du miner")
        for r in rader[:12]:
            log(f"  {r['name']}: {r['isk_per_hour']:,.0f} ISK/time ({r['best_route']}), "
                f"{r['best_value_per_m3']:,.0f} ISK per m3 rå malm, "
                f"marked {r.get('market_daily_volume') if r.get('market_daily_volume') is not None else '–'}/dag"
                f"{'' if r['passed'] else ' [' + ','.join(r['failed_rules']) + ']'}")

        run_at = now_utc()
        if not args.dry_run:
            write_candidates(conn, run_at, rader)
        if args.csv:
            write_csv(args.csv, rader[:args.csv_rows])

        runlog.ratelimit_remaining = esi.ratelimit_remaining
        runlog.orders_count = len(rader)
        esi.save_etags()
        runlog.finish(conn, ok=True, message=f"{len(rader)} malmtyper, {len(passerer)} aktuelle")
    except Exception as e:
        conn.rollback()
        fail(runlog, f"{type(e).__name__}: {e}", conn)
    finally:
        conn.close()


COLS = ("ore_type_id", "volume", "batch_size", "refined_value_per_unit", "refined_value_per_m3",
        "raw_net_per_unit", "raw_net_per_m3", "raw_route",
        "compressed_type_id", "compression_ratio", "compressed_net_per_unit", "compressed_net_per_m3",
        "refine_premium", "best_route", "best_value_per_m3", "isk_per_hour",
        "market_type_id", "market_daily_volume", "market_trades_per_day",
        "ore_daily_volume", "ore_trades_per_day", "available", "failed_rules", "score", "notes")


def write_candidates(conn, run_at, rader: list[dict]):
    with conn.cursor() as cur:
        cur.executemany(
            f"""insert into jita.mining_candidates (run_at, {', '.join(COLS)}, mineral_mix)
                values (%s, {', '.join(['%s'] * len(COLS))}, %s)
                on conflict (run_at, ore_type_id) do nothing""",
            [tuple([run_at] + [r.get(c) for c in COLS] + [json.dumps(r.get("mineral_mix") or {})])
             for r in rader])
    conn.commit()
    log(f"{len(rader)} rader skrevet til jita.mining_candidates ({run_at:%Y-%m-%d %H:%M} UTC)")


def write_csv(path: str, rader: list[dict]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["navn", "gruppe"] + list(COLS))
        for r in rader:
            w.writerow([r.get("name"), r.get("group_name")] + [r.get(c) for c in COLS])
    log(f"CSV skrevet: {path} ({len(rader)} rader)")


if __name__ == "__main__":
    main()
