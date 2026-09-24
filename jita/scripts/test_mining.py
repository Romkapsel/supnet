"""
test_mining.py – sjekker formlene i mining.py mot tall regnet ut for hånd.
Krever verken nett eller database. Kjør:  python jita/scripts/test_mining.py
"""
from __future__ import annotations

import sys

from mining import MiningProfile, evaluate, judge, net_sale, refined_value, tick

FEIL = []


def sjekk(navn: str, fikk, vil, tol=1e-6):
    ok = abs(float(fikk) - float(vil)) <= tol * max(1.0, abs(float(vil)))
    print(f"{'ok  ' if ok else 'FEIL'} {navn}: {fikk} (ventet {vil})")
    if not ok:
        FEIL.append(navn)


TRITANIUM, PYERITE, VELDSPAR, KOMPRIMERT = 34, 35, 1230, 28430

# Veldspar: 100 enheter (batch) → 415 Tritanium. 0,1 m3 per enhet.
MALM = dict(ore_type_id=VELDSPAR, name="Veldspar", group_name="Veldspar", volume=0.1,
            batch_size=100, yields={TRITANIUM: 415.0})

QUOTES = {
    TRITANIUM: dict(buy_max=5.0, sell_min=6.0),        # salgsordre: 5,99 × 0,9125; dumping: 5 × 0,925
    PYERITE: dict(buy_max=10.0, sell_min=12.0),
    VELDSPAR: dict(buy_max=8.0, sell_min=10.0),
    KOMPRIMERT: dict(buy_max=700.0, sell_min=900.0),
}

P = MiningProfile(reprocess_yield=0.52, m3_per_hour=3000, broker=0.01, tax=0.075,
                  thresholds={"available_groups": ["Veldspar", "Scordite"],
                              "min_daily_volume": 100, "min_trades_per_day": 3})


def main():
    # ── Salgsvei: salgsordre mot dumping ──
    netto, vei = net_sale(QUOTES[TRITANIUM], P)
    via_ordre = (6.0 - tick(6.0)) * (1 - 0.01 - 0.075)
    via_dump = 5.0 * (1 - 0.075)
    sjekk("Tritanium: velger beste vei", netto, max(via_ordre, via_dump))
    sjekk("Tritanium: veien er salgsordre", 1 if vei == "salgsordre" else 0,
          1 if via_ordre >= via_dump else 0)

    tynn = dict(buy_max=100.0, sell_min=None)          # bare bud → må dumpes
    n2, v2 = net_sale(tynn, P)
    sjekk("bare bud: dumping", n2, 100.0 * (1 - 0.075))
    sjekk("bare bud: veien er dumping", 1 if v2 == "dumping" else 0, 1)
    sjekk("ingen pris gir 0", net_sale(None, P)[0], 0.0)

    # ── Refine-verdi: 415 Tritanium × 52 % / 100 enheter ──
    verdi, mix, mangler = refined_value({TRITANIUM: 415.0}, 100, P, QUOTES)
    sjekk("refine-verdi per enhet malm", verdi, 415.0 * 0.52 * netto / 100)
    sjekk("mineral-miks summerer til 1", sum(mix.values()), 1.0)
    sjekk("ingen mangler pris", len(mangler), 0)

    _, _, mangler2 = refined_value({TRITANIUM: 415.0, 999999: 10.0}, 100, P, QUOTES)
    sjekk("mineral uten pris rapporteres", len(mangler2), 1)

    # ── Hele vurderingen ──
    r = evaluate(MALM, P, QUOTES)
    assert r, "evaluate ga None"
    rå = 10.0 - tick(10.0)
    rå_netto = max(rå * (1 - 0.01 - 0.075), 8.0 * (1 - 0.075))
    sjekk("rå netto per enhet", r["raw_net_per_unit"], round(rå_netto, 2))
    sjekk("refinet per m3", r["refined_value_per_m3"], round(verdi / 0.1, 2))
    sjekk("rått per m3", r["raw_net_per_m3"], round(rå_netto / 0.1, 2))
    beste = max(verdi / 0.1, rå_netto / 0.1)
    sjekk("beste verdi per m3", r["best_value_per_m3"], round(beste, 2))
    sjekk("ISK per time", r["isk_per_hour"], round(beste * 3000, 2))
    sjekk("tilgjengelig i Veldspar-gruppa", 1 if r["available"] else 0, 1)

    # Refine-premien: hvor mye mer refine gir enn å selge rått
    sjekk("refine-premie", r["refine_premium"], round((verdi / 0.1) / (rå_netto / 0.1) - 1, 4))

    # ── Dommen ──
    judge(r, P)
    sjekk("Veldspar passerer", 1 if r["passed"] else 0, 1)
    sjekk("score = ISK/time", r["score"], r["isk_per_hour"])

    ikke_her = evaluate(dict(MALM, group_name="Mercoxit"), P, QUOTES)
    judge(ikke_her, P)
    sjekk("malm utenfor området forkastes (m1)", 1 if "m1" in ikke_her["failed_rules"] else 0, 1)
    sjekk("score nulles når den ikke gjelder", ikke_her["score"], 0.0)

    # m3: tynt malmmarked rammer bare rå-salg
    rå_best = evaluate(dict(MALM, yields={}), P, QUOTES)      # uten utbytte er rå eneste vei
    sjekk("uten utbytte er rå eneste vei", 1 if rå_best["best_route"] == "rå" else 0, 1)
    rå_best.update(ore_daily_volume=5, ore_trades_per_day=0.5)
    judge(rå_best, P)
    sjekk("tynt malmmarked forkaster rå-salg (m3)", 1 if "m3" in rå_best["failed_rules"] else 0, 1)
    sjekk("uten utbytte mangler refine (m4)", 1 if "m4" in rå_best["failed_rules"] else 0, 1)

    billig = {**QUOTES, VELDSPAR: dict(buy_max=0.1, sell_min=0.2)}
    refine_best = evaluate(MALM, P, billig)
    refine_best.update(ore_daily_volume=5, ore_trades_per_day=0.5)
    judge(refine_best, P)
    sjekk("refine er beste vei når malmen selges billig",
          1 if refine_best["best_route"] == "refine" else 0, 1)
    sjekk("tynt malmmarked rammer ikke refine-veien",
          1 if "m3" in refine_best["failed_rules"] else 0, 0)

    # ── Komprimert malm er sin egen rad, med eget volum og batch ──
    komp = dict(ore_type_id=KOMPRIMERT, name="Compressed Veldspar", group_name="Veldspar",
                volume=0.15, batch_size=1, yields={TRITANIUM: 415.0})
    rk = evaluate(komp, P, QUOTES)
    sjekk("komprimert: refine per enhet er hele batchen",
          rk["refined_value_per_unit"], round(415.0 * 0.52 * netto, 2))
    sjekk("komprimert gir mer per m3 enn rå malm",
          1 if rk["best_value_per_m3"] > r["best_value_per_m3"] else 0, 1)

    # ── Utbyttet slår rett inn i verdien ──
    P2 = MiningProfile(reprocess_yield=0.78, m3_per_hour=3000, broker=0.01, tax=0.075,
                       thresholds=P.thresholds)
    r2 = evaluate(MALM, P2, QUOTES)
    sjekk("høyere refine-utbytte gir høyere verdi",
          r2["refined_value_per_m3"], round(r["refined_value_per_m3"] * 0.78 / 0.52, 2), tol=1e-3)

    # ── Parseren for refine-utbytte mot alle skrivemåtene kildene bruker ──
    from ingest_mining import parse_typematerials
    ccp = {"1230": {"materials": [{"materialTypeID": 34, "quantity": 415}]}}
    sjekk("parser: CCP (materialTypeID)", parse_typematerials(ccp)[1230][34], 415)
    everef_dict = {"1230": {"materials": [{"type_id": 34, "quantity": 415}]}}
    sjekk("parser: snake_case", parse_typematerials(everef_dict)[1230][34], 415)
    som_dict = {"1230": {"materials": {"34": {"typeID": 34, "quantity": 415}}}}
    sjekk("parser: materials som dict", parse_typematerials(som_dict)[1230][34], 415)
    som_liste = [{"type_id": 1230, "materials": [{"material_type_id": 34, "quantity": 415}]}]
    sjekk("parser: hele svaret som liste", parse_typematerials(som_liste)[1230][34], 415)
    try:
        parse_typematerials({"1230": {"materials": [{"ukjent": 1}]}})
        sjekk("parser: ukjent form gir feil med eksempel", 0, 1)
    except RuntimeError as e:
        sjekk("parser: ukjent form gir feil med eksempel", 1 if "første material" in str(e) else 0, 1)

    print()
    if FEIL:
        print(f"{len(FEIL)} feil: {', '.join(FEIL)}")
        sys.exit(1)
    print("Alle mining-formler stemmer.")


if __name__ == "__main__":
    main()
