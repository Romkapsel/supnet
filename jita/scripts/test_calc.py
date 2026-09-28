"""
test_calc.py – blueprint-kalkulatoren (lib/calc.js).

Kjører calc_probe.mjs med node og sjekker:
  1. ME-avrundingen er identisk med fasiten material_quantity() i industry.py over et rutenett
  2. malmplanen faktisk dekker behovet, i hele batcher, og velger riktig malm
  3. mineraler ingen malm gir havner i «må kjøpes», og rest under én batch refines ikke

Kjør:  python jita/scripts/test_calc.py   (krever node)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from industry import material_quantity

FEIL = []


def sjekk(navn: str, fikk, vil):
    ok = fikk == vil
    print(f"{'ok  ' if ok else 'FEIL'} {navn}: {fikk} (ventet {vil})")
    if not ok:
        FEIL.append(navn)


def main():
    probe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calc_probe.mjs")
    try:
        ut = subprocess.run(["node", probe], capture_output=True, text=True, timeout=60, check=True).stdout
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"FEIL: node mangler eller probe feilet: {e}")
        sys.exit(1)
    d = json.loads(ut)

    # ── 1. ME-avrunding: JS mot Python-fasiten ──
    avvik = [(b, r, me, js, material_quantity(b, r, me)) for b, r, me, js in d["rutenett"]
             if js != material_quantity(b, r, me)]
    sjekk(f"ME-avrunding lik Python over {len(d['rutenett'])} kombinasjoner", len(avvik), 0)
    for a in avvik[:5]:
        print("     avvik (base, runs, ME, js, py):", a)
    js = {(b, r, me): v for b, r, me, v in d["rutenett"]}
    sjekk("mengde 1 reduseres ikke av ME (10 runs à 1 stk = 10)", js[(1, 10, 10)], 10)
    sjekk("ME 10 på 8071 (skjermbildet 28. sept)", js[(8071, 1, 10)], 7264)
    sjekk("ME 0 er uendret", js[(8071, 1, 0)], 8071)
    sjekk("rundes opp per jobb, ikke per run (3 × 3 stk, ME 10 = 9 − 0,9 → 9)", js[(3, 3, 10)], 9)

    # ── 2. Malmplanen dekker behovet ──
    ores = {str(o["id"]): o for o in d["ORES"]}
    plan = d["plan"]
    behov = {"34": 7264, "35": 6739, "36": 197, "37": 2}
    gir = {}
    for linje in plan["ores"]:
        o = ores[str(linje["ore_type_id"])]
        sjekk(f"hele batcher: {o['name']}", linje["units"] % o["batch"], 0)
        for m, per in o["minerals"].items():
            gir[m] = gir.get(m, 0) + (linje["units"] // o["batch"]) * per * 0.5
    for m, n in behov.items():
        sjekk(f"planen dekker mineral {m} ({n})", 1 if gir.get(m, 0) >= n else 0, 1)
    navn = [x["name"] for x in plan["ores"]]

    def best_per_m3(mineral):
        """Fasit regnet fra dataene: malmen som gir mest av mineralet per m³."""
        return max((o for o in d["ORES"] if o["minerals"].get(mineral)),
                   key=lambda o: o["minerals"][mineral] / (o["batch"] * o["volume"]))["name"]
    # Isogen: Omber 75/60 m³ = 1,25 slår Kernite 120/120 m³ = 1,0 – testen ble skrevet feil først
    sjekk("Isogen (dyrest) tas fra malmen med mest Isogen per m³", best_per_m3("37") in navn, True)
    sjekk("Mexallon tas fra malmen med mest Mexallon per m³", best_per_m3("36") in navn, True)
    sjekk("Pyerite tas fra malmen med mest Pyerite per m³", best_per_m3("35") in navn, True)
    sjekk("Tritanium fra de andre malmene teller – ingen Veldspar trengs", "Veldspar" in navn, False)
    sjekk("hver malm tas bare én gang", len(navn), len(set(navn)))
    sjekk("m³ summerer riktig", round(sum(x["m3"] for x in plan["ores"]), 2), plan["m3"])
    sjekk("ingenting udekket når all malm finnes", plan["uncovered"], {})

    # ── 3. Det som ikke kan mines, og rest under én batch ──
    sjekk("Nocxium finnes ikke i malmen der du miner → må kjøpes", d["planMedNocx"]["uncovered"], {"38": 50})
    sjekk("resten av planen virker likevel", 1 if d["planMedNocx"]["ores"] else 0, 1)
    # 1 050 Veldspar = 10 hele batcher (50 blir liggende); 99 Scordite = 0 batcher
    sjekk("malm du har: bare hele batcher refines", d["fraMalm"].get("34"), 10 * 400 * 0.5)
    sjekk("malm du har: under én batch gir ingenting", d["fraMalm"].get("35", 0), 0)

    print()
    if FEIL:
        print(f"{len(FEIL)} feil: {', '.join(FEIL)}")
        sys.exit(1)
    print("Kalkulatoren regner riktig.")


if __name__ == "__main__":
    main()
