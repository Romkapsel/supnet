"""
probe_sources.py – sjekker hvilke eksterne datakilder som svarer, og hvordan svaret ser ut.

Brukes når en kilde flytter eller endrer format (SDE-dumper og ref-data har gjort begge).
Skriver ikke noe til databasen. Kjør via Actions → jita → Run workflow → job `probe`,
eller lokalt:  python jita/scripts/probe_sources.py
"""
from __future__ import annotations

import json

import requests

from common import USER_AGENT, log

JSON_KILDER = [
    "https://ref-data.everef.net/blueprints",
    "https://ref-data.everef.net/blueprints/682",
    "https://sde.hoboleaks.space/tq/blueprints.json",
    "https://api.everef.net/v1/industry/cost?product_id=1877&runs=10&me=10&te=20&system_id=30001395&facility_tax=0.0025",
]
HODE_KILDER = [
    "https://data.everef.net/reference-data/reference-data-latest.tar.xz",
    "https://www.fuzzwork.co.uk/dump/latest/industryActivityProducts.csv.bz2",
    "https://www.fuzzwork.co.uk/dump/latest/industryactivityproducts.csv.bz2",
    "https://www.fuzzwork.co.uk/dump/latest/industryActivityProducts.csv",
    "https://www.fuzzwork.co.uk/dump/latest/sqlite-latest.sqlite.bz2",
]
LISTINGER = [
    "https://www.fuzzwork.co.uk/dump/latest/",
    "https://data.everef.net/reference-data/",
]


def form(x, dybde=0) -> str:
    """Kort beskrivelse av strukturen: type, størrelse og de første nøklene."""
    if isinstance(x, dict):
        nokler = list(x)[:8]
        s = f"dict({len(x)}) nøkler={nokler}"
        if dybde < 2 and nokler:
            s += f"\n{'  ' * (dybde + 1)}[{nokler[0]}] → " + form(x[nokler[0]], dybde + 1)
        return s
    if isinstance(x, list):
        s = f"list({len(x)})"
        if x and dybde < 2:
            s += f"\n{'  ' * (dybde + 1)}[0] → " + form(x[0], dybde + 1)
        return s
    return f"{type(x).__name__} {json.dumps(x)[:80]}"


def main():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    for url in JSON_KILDER:
        try:
            r = s.get(url, timeout=120)
            log(f"GET {url} → {r.status_code}, {len(r.content) / 1e6:.2f} MB")
            if r.status_code == 200:
                try:
                    log("  struktur:", form(r.json()))
                except Exception:
                    log("  ikke JSON, starter med:", r.text[:200])
        except Exception as e:
            log(f"GET {url} → {type(e).__name__}: {e}")

    for url in HODE_KILDER:
        try:
            r = s.head(url, timeout=60, allow_redirects=True)
            log(f"HEAD {url} → {r.status_code}, {r.headers.get('Content-Length', '?')} bytes, "
                f"{r.headers.get('Content-Type', '?')}")
        except Exception as e:
            log(f"HEAD {url} → {type(e).__name__}: {e}")

    for url in LISTINGER:
        try:
            r = s.get(url, timeout=60)
            log(f"GET {url} → {r.status_code}")
            if r.status_code == 200:
                import re
                lenker = re.findall(r'href="([^"?][^"]*)"', r.text)
                industri = [l for l in lenker if "ndustr" in l or "lueprint" in l or "eference" in l]
                log("  relevante lenker:", industri[:20] or lenker[:20])
        except Exception as e:
            log(f"GET {url} → {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
