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
    FLYTER = {VELDSPAR: dict(daily_volume=5_000_000, trades_per_day=150.0),
              KOMPRIMERT: dict(daily_volume=5_000_000, trades_per_day=150.0),
              28431: dict(daily_volume=5_000_000, trades_per_day=150.0),
              28432: dict(daily_volume=5_000_000, trades_per_day=150.0)}
    r = evaluate(MALM, P, QUOTES, FLYTER)
    assert r, "evaluate ga None"
    rå = 10.0 - tick(10.0)
    rå_netto = max(rå * (1 - 0.01 - 0.075), 8.0 * (1 - 0.075))
    sjekk("rå netto per enhet", r["raw_net_per_unit"], round(rå_netto, 2))
    sjekk("refinet per m3", r["refined_value_per_m3"], round(verdi / 0.1, 2))
    sjekk("rått per m3", r["raw_net_per_m3"], round(rå_netto / 0.1, 2))
    beste = max(verdi / 0.1, rå_netto / 0.1)
    sjekk("beste verdi per m3", r["best_value_per_m3"], round(beste, 2))
    sjekk("ISK per time", r["isk_per_hour"], round(round(beste, 2) * 3000, 2))
    sjekk("tilgjengelig i Veldspar-gruppa", 1 if r["available"] else 0, 1)

    # Refine-premien: hvor mye mer refine gir enn å selge rått
    sjekk("refine-premie", r["refine_premium"], round((verdi / 0.1) / (rå_netto / 0.1) - 1, 4))

    # ── Dommen ──
    judge(r, P)
    sjekk("Veldspar passerer", 1 if r["passed"] else 0, 1)
    sjekk("score = ISK/time", r["score"], r["isk_per_hour"])

    ikke_her = evaluate(dict(MALM, group_name="Mercoxit"), P, QUOTES, FLYTER)
    judge(ikke_her, P)
    sjekk("malm utenfor området forkastes (m1)", 1 if "m1" in ikke_her["failed_rules"] else 0, 1)
    sjekk("score nulles når den ikke gjelder", ikke_her["score"], 0.0)

    # m3: tynt malmmarked rammer bare rå-salg
    rå_best = evaluate(dict(MALM, yields={}), P, QUOTES)      # uten utbytte er rå eneste vei
    sjekk("uten utbytte er rå eneste vei", 1 if rå_best["best_route"] == "rå" else 0, 1)
    rå_best.update(market_daily_volume=5, market_trades_per_day=0.5)
    judge(rå_best, P)
    sjekk("tynt malmmarked forkaster rå-salg (m3)", 1 if "m3" in rå_best["failed_rules"] else 0, 1)
    sjekk("uten utbytte mangler refine (m4)", 1 if "m4" in rå_best["failed_rules"] else 0, 1)

    billig = {**QUOTES, VELDSPAR: dict(buy_max=0.1, sell_min=0.2)}
    refine_best = evaluate(MALM, P, billig, FLYTER)
    refine_best.update(market_daily_volume=5, market_trades_per_day=0.5)
    judge(refine_best, P)
    sjekk("refine er beste vei når malmen selges billig",
          1 if refine_best["best_route"] == "refine" else 0, 1)
    sjekk("tynt malmmarked rammer ikke refine-veien",
          1 if "m3" in refine_best["failed_rules"] else 0, 0)

    # ── Komprimering er en salgsvei, ikke en egen rad ──
    from mining import compression_ratio
    # Veldspar: 415 Tritanium per 100 enheter rå = 4,15/enhet. Komprimert: 415 per 1 enhet → 100 til 1.
    sjekk("omregningsfaktor fra utbyttedata",
          compression_ratio({TRITANIUM: 415.0}, 100, {TRITANIUM: 415.0}, 1), 100.0)
    sjekk("omregningsfaktor uten felles mineral",
          1 if compression_ratio({TRITANIUM: 1}, 1, {PYERITE: 1}, 1) is None else 0, 1)

    med_komp = dict(MALM, compressed=[dict(type_id=KOMPRIMERT, name="Batch Compressed Veldspar",
                                           volume=0.15, batch_size=1, yields={TRITANIUM: 415.0})])
    rk = evaluate(med_komp, P, QUOTES, FLYTER)
    sjekk("faktoren regnes ut", rk["compression_ratio"], 100.0)
    # 100 enheter rå (10 m3) blir 1 komprimert enhet. Netto for den enheten fordeles på 10 m3 rå malm.
    komp_netto = max((900.0 - tick(900.0)) * (1 - 0.01 - 0.075), 700.0 * (1 - 0.075))
    sjekk("komprimert netto per enhet", rk["compressed_net_per_unit"], round(komp_netto, 2))
    sjekk("komprimert verdi per m3 RÅ malm", rk["compressed_net_per_m3"],
          round(komp_netto / (100 * 0.1), 2))
    sjekk("komprimert er ikke urealistisk høy",
          1 if rk["compressed_net_per_m3"] < 1000 else 0, 1)
    sjekk("beste vei velges blant tre", 1 if rk["best_route"] in ("refine", "rå", "komprimert") else 0, 1)
    sjekk("ISK/time regnes på rå-volumet", rk["isk_per_hour"],
          round(rk["best_value_per_m3"] * 3000, 2))

    # ── Malm uten salgspris: bare refine-veien finnes, og premien er ikke definert ──
    uten_salg = evaluate(MALM, P, {TRITANIUM: QUOTES[TRITANIUM]}, FLYTER)   # ingen pris på malmen selv
    sjekk("uten salgspris: refine er eneste vei", 1 if uten_salg["best_route"] == "refine" else 0, 1)
    sjekk("uten salgspris: ingen refine-premie",
          1 if uten_salg["refine_premium"] is None else 0, 1)
    sjekk("uten salgspris: ingen markedsvare", 1 if uten_salg["market_type_id"] is None else 0, 1)
    judge(uten_salg, P)
    sjekk("uten salgspris: passerer på refine", 1 if uten_salg["passed"] else 0, 1)

    bare_komp = dict(MALM, compressed=[dict(type_id=KOMPRIMERT, name="Batch Compressed Veldspar",
                                            volume=0.15, batch_size=1, yields={TRITANIUM: 415.0})])
    r_bk = evaluate(bare_komp, P, {TRITANIUM: QUOTES[TRITANIUM], KOMPRIMERT: QUOTES[KOMPRIMERT]}, FLYTER)
    sjekk("bare komprimert pris: premien regnes mot komprimert",
          1 if r_bk["refine_premium"] is not None else 0, 1)

    tom = evaluate(dict(MALM, yields={}), P, {}, FLYTER)                    # verken malm eller mineraler
    sjekk("uten noen priser gir None", 1 if tom is None else 0, 1)

    # ── Flere komprimerte varianter: den beste per m3 rå malm vinner ──
    TETT = 28431
    varianter = dict(MALM, compressed=[
        dict(type_id=KOMPRIMERT, name="Batch Compressed Veldspar", volume=0.15, batch_size=1,
             yields={TRITANIUM: 415.0}),                      # 100 rå per enhet
        dict(type_id=TETT, name="Compressed Veldspar", volume=0.001, batch_size=100,
             yields={TRITANIUM: 415.0}),                      # 1:1, 1/100 volum
    ])
    q2 = {**QUOTES, TETT: dict(buy_max=9.0, sell_min=11.0)}
    rv = evaluate(varianter, P, q2, FLYTER)
    komp1 = max((900.0 - tick(900.0)) * (1 - 0.01 - 0.075), 700.0 * (1 - 0.075)) / (100 * 0.1)
    komp2 = max((11.0 - tick(11.0)) * (1 - 0.01 - 0.075), 9.0 * (1 - 0.075)) / (1 * 0.1)
    sjekk("beste komprimerte variant velges", rv["compressed_net_per_m3"], round(max(komp1, komp2), 2))
    sjekk("faktoren hører til den valgte varianten", rv["compression_ratio"],
          100.0 if komp1 >= komp2 else 1.0)

    # Plausibilitet: en «komprimert» variant som ikke gir mindre volum forkastes
    umulig = dict(MALM, compressed=[dict(type_id=KOMPRIMERT, name="Feil", volume=0.2, batch_size=100,
                                         yields={TRITANIUM: 415.0})])   # 1:1 men større volum
    ru = evaluate(umulig, P, QUOTES, FLYTER)
    sjekk("urimelig komprimering forkastes",
          1 if ru["compressed_net_per_m3"] is None else 0, 1)

    # ── Veien velges bare blant markeder som flyter ──
    TETT2 = 28432
    to_veier = dict(MALM, compressed=[
        dict(type_id=KOMPRIMERT, name="Batch Compressed Veldspar", volume=0.15, batch_size=1,
             yields={TRITANIUM: 415.0}),                       # høy pris, dødt marked
        dict(type_id=TETT2, name="Compressed Veldspar", volume=0.001, batch_size=100,
             yields={TRITANIUM: 415.0}),                       # lavere pris, flyter
    ])
    q3 = {**QUOTES, TETT2: dict(buy_max=9.0, sell_min=11.0)}
    m3_data = {KOMPRIMERT: dict(daily_volume=160, trades_per_day=1.0),      # under terskelen
               TETT2: dict(daily_volume=5_000_000, trades_per_day=90.0),
               VELDSPAR: dict(daily_volume=5_000_000, trades_per_day=150.0)}
    rl = evaluate(to_veier, P, q3, m3_data)
    sjekk("den dyre men illikvide varianten velges bort",
          1 if rl["compressed_type_id"] == TETT2 else 0, 1)
    sjekk("den illikvide verdien tas vare på til forklaringen",
          1 if (rl["illiquid_routes"] or {}).get("komprimert") else 0, 1)
    judge(rl, P)
    sjekk("malmen ryker ikke ut fordi en variant var illikvid", 1 if rl["passed"] else 0, 1)

    # Forklaringen skal si fra når en illikvid vei ser bedre ut enn den valgte
    rl2 = evaluate(to_veier, P, {**q3, KOMPRIMERT: dict(buy_max=900000.0, sell_min=1000000.0)}, m3_data)
    judge(rl2, P)
    sjekk("forklaringen advarer om illikvid vei som ser bedre ut",
          1 if "flyter ikke" in rl2["notes"] else 0, 1)

    # Ingen vei med marked: raden beholdes, men forkastes med forklaring
    alt_dødt = evaluate(dict(MALM, yields={}), P, QUOTES,
                        {VELDSPAR: dict(daily_volume=3, trades_per_day=0.2)})
    judge(alt_dødt, P)
    sjekk("uten marked forkastes malmen (m3)", 1 if "m3" in alt_dødt["failed_rules"] else 0, 1)
    sjekk("men raden finnes fortsatt", 1 if alt_dødt["best_value_per_m3"] else 0, 1)

    # ── Likviditetsporten: manglende omsetningstall skal forkaste, ikke slippe gjennom ──
    selger_rått = evaluate(dict(MALM, yields={}), P, QUOTES, FLYTER)      # rå er eneste vei
    selger_rått.update(market_daily_volume=None, market_trades_per_day=None)
    judge(selger_rått, P)
    sjekk("manglende omsetningstall forkastes (m3)",
          1 if "m3" in selger_rått["failed_rules"] else 0, 1)

    nok_flyt = evaluate(dict(MALM, yields={}), P, QUOTES, FLYTER)
    nok_flyt.update(market_daily_volume=50000, market_trades_per_day=40)
    judge(nok_flyt, P)
    sjekk("nok flyt passerer", 1 if "m3" in nok_flyt["failed_rules"] else 0, 0)

    refine_uten_tall = evaluate(MALM, P, {**QUOTES, VELDSPAR: dict(buy_max=0.01, sell_min=0.02)}, FLYTER)
    refine_uten_tall.update(market_daily_volume=None, market_trades_per_day=None)
    judge(refine_uten_tall, P)
    sjekk("refine-veien rammes ikke av manglende malmomsetning",
          1 if "m3" in refine_uten_tall["failed_rules"] else 0, 0)
    sjekk("refine-veien er valgt der", 1 if refine_uten_tall["best_route"] == "refine" else 0, 1)

    # ── Utbyttet slår rett inn i verdien ──
    P2 = MiningProfile(reprocess_yield=0.78, m3_per_hour=3000, broker=0.01, tax=0.075,
                       thresholds=P.thresholds)
    r2 = evaluate(MALM, P2, QUOTES, FLYTER)
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
