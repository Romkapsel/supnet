"""
structures.py – spillerstrukturer (AV som standard, se ingest_orders.py – regionsboka har allerede disse ordrene) med marked nær Jita (TTT i Perimeter, «Neutral States Market HQ» osv.).

ESIs åpne regionsordrebok inneholder BARE NPC-stasjoner. Kjøpsordrer i strukturer med rekkevidde (region / N hopp)
konkurrerer likevel om de samme selgerne i Jita 4-4 – og kan ligge langt over Jita-budet. Uten dem blir
toppbud og margin feil (Datacore-tilfellet 21. sept: 25 000 i 4-4 mot 69 110 i Perimeter-struktur).

Roboten får karakterens token fra /api/token (PIN i GitHub-secret JITA_PIN) og henter
  /universe/structures/?filter=market  → hvilke strukturer har marked (oppdages daglig, lagres i jita.structures)
  /markets/structures/{id}/            → alle ordrer (paginert); vi beholder kjøpsordrene
Bare strukturer innen STRUCT_MAX_JUMPS hopp fra Jita tas med.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from common import ESI, COMPAT_DATE, USER_AGENT, Esi, log

STRUCT_MAX_JUMPS = int(os.environ.get("STRUCT_MAX_JUMPS", "2"))
API = os.environ.get("JITA_API", "https://jita-eve.vercel.app/api")


def get_token() -> str | None:
    pin = os.environ.get("JITA_PIN")
    if not pin:
        log("JITA_PIN mangler – strukturordrer hoppes over")
        return None
    try:
        r = requests.get(f"{API}/token", headers={"x-jita-pin": pin}, timeout=30)
        if r.status_code != 200:
            log(f"token-kall ga {r.status_code}: {r.text[:120]}")
            return None
        return r.json().get("token")
    except requests.RequestException as e:
        log("token-kall feilet:", e)
        return None


def _auth_get(token: str, path: str, params=None, timeout=30):
    r = requests.get(ESI + path, params=params, timeout=timeout,
                     headers={"Authorization": f"Bearer {token}", "X-Compatibility-Date": COMPAT_DATE,
                              "User-Agent": USER_AGENT, "Accept": "application/json"})
    return r


EVEREF = "https://data.everef.net/structures/structures-latest.v2.json"


def discover(conn, token: str, jumps: dict[int, int]) -> int:
    """Finn markedsstrukturer innen STRUCT_MAX_JUMPS fra Jita og lagre i jita.structures. Kjøres daglig.
    ESIs /universe/structures/?filter=market gir bare strukturer karakteren kan dokke i (56 i hele New Eden) –
    TTT og Perimeter-markedene mangler. EVE Ref publiserer et åpent datasett over alle kjente offentlige
    strukturer (https://docs.everef.net/datasets/structures.html); det brukes i stedet, ESI-lista som reserve."""
    found = []
    try:
        data = requests.get(EVEREF, timeout=120, headers={"User-Agent": USER_AGENT}).json()
        for sid, st in data.items():
            sysid = st.get("solar_system_id")
            if st.get("is_market_structure") and sysid in jumps and jumps[sysid] <= STRUCT_MAX_JUMPS:
                found.append((int(sid), st.get("name"), int(sysid), jumps[sysid]))
        log(f"EVE Ref: {len(data)} strukturer, {len(found)} med marked innen {STRUCT_MAX_JUMPS} hopp fra Jita")
    except Exception as e:
        log("EVE Ref-datasettet feilet, bruker ESI-lista:", e)
        r = _auth_get(token, "/universe/structures/", {"filter": "market"})
        for sid in (r.json() if r.status_code == 200 else []):
            rr = _auth_get(token, f"/universe/structures/{sid}/")
            if rr.status_code == 200:
                j = rr.json(); sysid = j.get("solar_system_id")
                if sysid in jumps and jumps[sysid] <= STRUCT_MAX_JUMPS:
                    found.append((sid, j.get("name"), sysid, jumps[sysid]))
    with conn.cursor() as cur:
        cur.executemany("""insert into jita.structures (structure_id, name, system_id, jumps_from_jita, has_market, updated_at)
                           values (%s, %s, %s, %s, true, now())
                           on conflict (structure_id) do update set name = excluded.name, jumps_from_jita = excluded.jumps_from_jita, updated_at = now()""", found)
        cur.execute("update jita.structures set updated_at = now()")   # også de som ikke fikk treff, så daglig-sjekken vet vi har prøvd
    conn.commit()
    return len(found)


def fetch_structure_buy_orders(conn, token: str) -> list:
    """→ ordre-rader (samme format som order_row) for KJØPSORDRER i strukturene nær Jita."""
    with conn.cursor() as cur:
        cur.execute("select structure_id, name, system_id from jita.structures where has_market")
        structs = cur.fetchall()
    if not structs:
        return []
    out = []

    def fetch_one(st):
        sid, name, system_id = st
        rows, page, pages, status = [], 1, 1, None
        while page <= pages:
            r = _auth_get(token, f"/markets/structures/{sid}/", {"page": page}, timeout=40)
            status = r.status_code
            if status == 403:                             # mistet docking-tilgang
                return sid, name, None, "403 ingen tilgang"
            if status != 200:
                return sid, name, None, f"{status}"
            pages = int(r.headers.get("X-Pages", "1"))
            for o in r.json():
                if o.get("is_buy_order"):
                    rows.append([o["order_id"], o["type_id"], True, float(o["price"]), int(o["volume_remain"]),
                                 o["issued"], int(o["duration"]), int(sid), int(system_id), str(o.get("range"))])
            page += 1
        return sid, name, rows, None

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(fetch_one, structs))
    with conn.cursor() as cur:
        for sid, name, rows, err in results:
            if rows is None:
                cur.execute("update jita.structures set last_error = %s, has_market = (%s <> '403 ingen tilgang'), updated_at = now() where structure_id = %s", (err, err, sid))
                log(f"struktur {name}: {err}")
            else:
                cur.execute("update jita.structures set last_ok = now(), last_error = null, orders_count = %s, updated_at = now() where structure_id = %s", (len(rows), sid))
                out.extend(rows)
    conn.commit()
    log(f"strukturordrer: {len(out)} kjøpsordrer fra {sum(1 for r in results if r[2] is not None)} strukturer")
    return out
