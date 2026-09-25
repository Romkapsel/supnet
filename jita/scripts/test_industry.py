"""
test_industry.py – sjekker formlene i industry.py mot tall regnet ut for hånd.

Krever verken nett eller database. Kjør:  python jita/scripts/test_industry.py

Fasiten er et oppdiktet produkt («Testmodul I») med to materialer, slik at hele kjeden
kan regnes etter i hodet: ME-runding → EIV → jobbavgift → kostpris → netto → ISK/dag/slot.
"""
from __future__ import annotations

import sys

from industry import (IndustryProfile, economics, factors, job_cost, judge, material_quantity,
                      pick_portfolio, realistic_throughput, tick, time_per_run)

FEIL = []


def dyr_tynn_isk(rangert):
    """Sjekker at den dyre varen faktisk har høyere ISK/dag enn den billige – altså at
    rangeringen valgte den billige PÅ TROSS AV lavere absolutt fortjeneste."""
    billig = [x for x in rangert if x["name"] == "Billig og likvid"][0]
    dyr = [x for x in rangert if x["name"] == "Dyr men stor"][0]
    return dyr["profit_per_day"] > billig["profit_per_day"]


def sjekk(navn: str, fikk, vil, tol=1e-6):
    if isinstance(vil, (list, tuple)) or isinstance(fikk, (list, tuple)):
        ok = list(fikk) == list(vil)
    elif isinstance(vil, str) or isinstance(fikk, str):
        ok = str(fikk) == str(vil)
    else:
        ok = abs(float(fikk) - float(vil)) <= tol * max(1.0, abs(float(vil)))
    print(f"{'ok  ' if ok else 'FEIL'} {navn}: {fikk} (ventet {vil})")
    if not ok:
        FEIL.append(navn)


# ── Testoppsettet ────────────────────────────────────────────────────────────
TRITANIUM, PYERITE, PRODUKT = 34, 35, 999999801

BOM = dict(blueprint_type_id=999999802, product_type_id=PRODUKT, units_per_run=2,
           base_time_s=600, max_runs=None,
           materials={TRITANIUM: 1000.0, PYERITE: 250.0})

QUOTES = {
    TRITANIUM: dict(buy_max=5.0, sell_min=6.0, volume=0.01, sell_orders=100),
    PYERITE: dict(buy_max=10.0, sell_min=12.0, volume=0.01, sell_orders=100),
    PRODUKT: dict(buy_max=8000.0, sell_min=10000.0, volume=5.0, sell_orders=12),
}
ADJUSTED = {TRITANIUM: 5.5, PYERITE: 11.0}

P = IndustryProfile(me=10, te=20, facility_tax=0.0025, scc_rate=0.04, industry=5,
                    advanced_industry=3, broker=0.01, tax=0.0375, cost_index=0.05,
                    material_source="buy",
                    thresholds={"min_margin": 0.10, "min_daily_volume": 20, "volume_share": 0.10,
                                "batch_days": 1, "min_sell_orders": 5, "max_capital_per_job": 20e6,
                                "max_bpo_price": 50e6, "max_payback_days": 30})


def main():
    # ── ME-runding: 1000 × 0,9 = 900 per run; ett materiale på 1 stk reduseres aldri ──
    sjekk("ME: 1000 × 10 runs", material_quantity(1000, 10, 10), 9000)
    sjekk("ME: rundes opp per jobb (7 × 1 run)", material_quantity(7, 1, 10), 7)   # ceil(6,3) = 7
    sjekk("ME: rundes opp per jobb (7 × 10 runs)", material_quantity(7, 10, 10), 63)
    sjekk("ME: mengde 1 reduseres ikke", material_quantity(1, 10, 10), 10)
    sjekk("ME: aldri under 1 per run", material_quantity(2, 10, 90), 10)

    # ── Tid: 600 s × (1 − 0,20) × (1 − 0,20) × (1 − 0,09) = 349,44 s ──
    t = time_per_run(600, P)
    sjekk("tid per run", t, 600 * 0.8 * 0.8 * 0.91)

    # ── Batch: ett døgn = 86400 / 349,44 = 247 runs ──
    r = economics(BOM, P, QUOTES, ADJUSTED)
    assert r, "economics ga None"
    runs = int(86400 // t)
    sjekk("runs i batchen", r["runs"], runs)
    sjekk("enheter i batchen", r["units"], runs * 2)

    # ── Materialkost: (900 × 5 + 225 × 10) × runs × (1 + broker) ──
    trit = material_quantity(1000, runs, 10)
    pye = material_quantity(250, runs, 10)
    vil_mat = (trit * 5.0 + pye * 10.0) * 1.01
    sjekk("materialkost", r["material_cost"], round(vil_mat, 2), tol=1e-9)

    # ── EIV på grunnmengdene (før ME) × adjusted_price ──
    vil_eiv = (1000 * 5.5 + 250 * 11.0) * runs
    sjekk("EIV", r["eiv"], vil_eiv)
    sjekk("jobbavgift", r["job_cost"], round(vil_eiv * (0.05 + 0.0025 + 0.04), 2), tol=1e-9)

    # ── Kostpris, salgspris og netto ──
    vil_cost = (r["material_cost"] + r["job_cost"]) / (runs * 2)
    sjekk("kostpris per enhet", r["cost_per_unit"], round(vil_cost, 2), tol=1e-6)
    sjekk("tick på 10 000", tick(10000.0), 10.0)
    sjekk("salgspris (ett tick under ask)", r["sell_price"], 9990.0)
    vil_net = 9990.0 * (1 - 0.01 - 0.0375) - r["cost_per_unit"]
    sjekk("netto per enhet", r["net_per_unit"], round(vil_net, 2), tol=1e-6)
    sjekk("margin", r["margin"], r["net_per_unit"] / r["cost_per_unit"])

    # ── Tempo: slotten rekker 86400/t × 2 enheter ──
    sjekk("enheter per døgn per slot", r["units_per_day_slot"], round(86400 / t * 2, 2))

    # ── Realistisk tempo: tre tak – slot, marked og kapital-omløp ──
    # Omløpstaket ligger alltid litt under de to andre: batchen må også selges før pengene er tilbake.
    lite = realistic_throughput(r, 100, P)                 # 10 % av 100 = 10 stk/dag i markedet
    sjekk("lite volum: potensialet er markedstaket", lite["potential_units_per_day"], 10.0)
    sjekk("lite volum: realistisk ligger under potensialet",
          1 if lite["realistic_units_per_day"] < 10.0 else 0, 1)
    sjekk("lite volum: omløpet er bremsen", 1 if lite["bottleneck"] == "omløp" else 0, 1)
    sjekk("lite volum: 494 stk selges på 49,4 d + 1 d produksjon",
          lite["realistic_units_per_day"], round(r["units"] / (1 + r["units"] / 10), 2), tol=1e-3)
    sjekk("ISK/dag følger realistisk tempo", lite["isk_per_day_slot"],
          round(r["net_per_unit"] * lite["realistic_units_per_day"], 2), tol=1e-6)

    stort = realistic_throughput(r, 100000, P)             # 10 % = 10 000 > slot-kapasitet
    sjekk("stort volum: potensialet er slot-kapasiteten",
          stort["potential_units_per_day"], r["units_per_day_slot"])
    sjekk("stort volum: realistisk ligger under slot-kapasiteten",
          1 if stort["realistic_units_per_day"] < r["units_per_day_slot"] else 0, 1)

    uten_volum = realistic_throughput(r, None, P)          # ingen historikk → bare slot-taket
    sjekk("uten volum: slot er bremsen", 1 if uten_volum["bottleneck"] == "slot" else 0, 1)
    sjekk("uten volum: full slot-kapasitet", uten_volum["realistic_units_per_day"],
          r["units_per_day_slot"])

    # Dyr vare: én enhet per batch, 2 mill. per stk – kapitalen kan ikke snus rundt fritt.
    dyr = dict(r, units=1, units_per_day_slot=120.0, time_per_batch_s=0.2 * 3600)
    kap = realistic_throughput(dyr, 275, P)                # 10 % av 275 = 27,5 stk/dag i markedet
    syklus = 0.2 / 24 + 1 / 27.5
    sjekk("dyr vare: omløpet bremser", kap["realistic_units_per_day"], round(1 / syklus, 2), tol=1e-3)
    sjekk("dyr vare: flaskehalsen navngis", 1 if kap["bottleneck"] == "omløp" else 0, 1)
    sjekk("dyr vare: under markedstaket", 1 if kap["realistic_units_per_day"] < 27.5 else 0, 1)
    sjekk("dyr vare: omløpstid", kap["cycle_days"], round(syklus, 3), tol=1e-3)

    # ── Frakt: m3 inn og ut ──
    sjekk("m3 inn", r["m3_in"], round((trit + pye) * 0.01, 2))
    sjekk("m3 ut", r["m3_out"], round(runs * 2 * 5.0, 2))

    # ── Dommen ──
    god = dict(r, daily_volume=100000, price_avg_30d=9500, price_volatility=0.05,
               price_drop_30d=0.02, sell_orders=12, bpo_price=1_000_000)
    god.update(realistic_throughput(god, 100000, P))
    god["payback_days"] = round(1_000_000 / god["isk_per_day_slot"], 2)
    judge(god, P)
    sjekk("god vare passerer", 1 if god["passed"] else 0, 1)
    sjekk("score = ISK/dag × faktorer", god["score"],
          round(god["isk_per_day_slot"] * god["factors"]["liquidity"] * god["factors"]["competition"]
                * god["factors"]["stable"] * god["factors"]["trend"], 2), tol=1e-6)

    tynn = dict(god, daily_volume=5)
    tynn.update(realistic_throughput(tynn, 5, P))
    judge(tynn, P)
    sjekk("tynt marked forkastes (i2)", 1 if "i2" in tynn["failed_rules"] else 0, 1)

    fall = dict(god, price_drop_30d=0.40)
    judge(fall, P)
    sjekk("prisfall forkastes (i6)", 1 if "i6" in fall["failed_rules"] else 0, 1)

    dyr = dict(god, bpo_price=900_000_000)
    judge(dyr, P)
    sjekk("dyr BPO forkastes (i4)", 1 if "i4" in dyr["failed_rules"] else 0, 1)

    spike = dict(god, price_avg_30d=1000.0)               # sell_min 10 000 = 10 × snittet
    judge(spike, P)
    sjekk("pristopp forkastes (i9)", 1 if "i9" in spike["failed_rules"] else 0, 1)

    # ── Porteføljevelgeren: 2 slots, kapital som bare rekker til én jobb ──
    a = dict(god, product_type_id=1, name="A", capital_per_job=1_000_000, score=500, passed=True)
    b = dict(god, product_type_id=2, name="B", capital_per_job=1_000_000, score=400, passed=True)
    c = dict(god, product_type_id=3, name="C", capital_per_job=5_000_000, score=999, passed=True)
    pf = pick_portfolio([a, b, c], slots=2, capital=1_500_000)
    sjekk("portefølje: antall valg", len(pf["picks"]), 1)
    sjekk("portefølje: valgte den beste som er råd til", pf["picks"][0]["product_type_id"], 1)
    sjekk("portefølje: ledig kapital", pf["left"], 500_000)
    pf2 = pick_portfolio([a, b, c], slots=2, capital=100_000_000)
    sjekk("portefølje: dyrest først når kapitalen holder", pf2["picks"][0]["product_type_id"], 3)
    sjekk("portefølje: fyller alle slots", len(pf2["picks"]), 2)

    # ── Faktorene ──
    f = factors(dict(daily_volume=50, sell_orders=40, price_volatility=0.5, price_drop_30d=0.1,
                     isk_per_day_slot=1000), P)
    sjekk("likviditet (50 av 100 ønsket)", f["liquidity"], 0.5)
    sjekk("konkurranse (40 selgere)", f["competition"], 0.5)
    sjekk("stabilitet har gulv 0,7", f["stable"], 0.7)
    sjekk("trend ved 10 % fall", f["trend"], 0.8)

    # ── Kapitaltaket: batchen skal ikke bli større enn kapitalen tåler ──
    liten = IndustryProfile(**{**{k: getattr(P, k) for k in
        ('me','te','facility_tax','scc_rate','industry','advanced_industry','broker','tax',
         'cost_index','material_source')}, 'capital_isk': 1_000_000,
        'thresholds': dict(P.thresholds, capital_share_per_job=0.5)})
    r2 = economics(BOM, liten, QUOTES, ADJUSTED)
    sjekk("kapitaltak: batchen holder seg innenfor budsjettet (500k = 50 % av 1 mill.)",
          1 if r2["capital_per_job"] <= 500_000 else 0, 1)
    sjekk("kapitaltak: færre runs enn tiden tillater", 1 if r2["runs"] < r["runs"] else 0, 1)
    sjekk("kapitaltak: minst én run", 1 if r2["runs"] >= 1 else 0, 1)
    sjekk("kapitaltak: kostpris per enhet er uendret (skalerer ikke med batchen)",
          round(r2["cost_per_unit"] / r["cost_per_unit"], 2), 1.0, tol=0.02)

    # ── i10: blueprint som ikke finnes på markedet kan ikke kjøpes ──
    ikke_bp = dict(god, blueprint_on_market=False)
    judge(ikke_bp, P)
    sjekk("i10 slår til når blueprinten ikke er på markedet",
          1 if "i10" in ikke_bp["failed_rules"] else 0, 1)
    pa_marked = dict(god, blueprint_on_market=True)
    judge(pa_marked, P)
    sjekk("i10 slår ikke til når blueprinten finnes", 1 if "i10" in pa_marked["failed_rules"] else 0, 0)

    # ── Momentvernet: markedet setter tak på batchen, og døde varer forkastes ──
    from industry import market_units_cap
    P2 = IndustryProfile(**{**{k: getattr(P, k) for k in
        ('me','te','facility_tax','scc_rate','industry','advanced_industry','broker','tax',
         'cost_index','material_source')},
        'thresholds': dict(P.thresholds, max_sell_days=5, min_trades_per_day=3)})

    sjekk("markedstak: 10 % av 40/dag i 5 dager = 20 stk", market_units_cap(40, P2), 20.0)
    sjekk("markedstak: ukjent volum gir ingen tak",
          1 if market_units_cap(None, P2) is None else 0, 1)

    # En vare som flyter 2 i uka (0,29/dag): taket blir under én enhet → batchen skal bli 1, ikke 200
    treg = economics(BOM, P2, QUOTES, ADJUSTED, max_units=market_units_cap(0.29, P2))
    sjekk("treg vare: batchen krympes til minimum", treg["runs"], 1)
    sjekk("treg vare: 2 enheter (units_per_run 2), ikke 494", treg["units"], 2)

    treg.update(daily_volume=0.29, trades_per_day=0.3, price_avg_30d=9500,
                price_volatility=0.05, price_drop_30d=0.0, sell_orders=12, blueprint_on_market=True)
    treg.update(realistic_throughput(treg, 0.29, P2))
    judge(treg, P2)
    sjekk("treg vare: forkastes på for få handler (i11)",
          1 if "i11" in treg["failed_rules"] else 0, 1)
    sjekk("treg vare: forkastes også på for lite volum (i2)",
          1 if "i2" in treg["failed_rules"] else 0, 1)
    sjekk("treg vare: passerer ikke", 1 if treg["passed"] else 0, 0)

    # i12: batch som er større enn markedet spiser innen 5 dager
    for_stor = dict(god, units=1000, daily_volume=40, trades_per_day=20, blueprint_on_market=True)
    judge(for_stor, P2)
    sjekk("for stor batch forkastes (i12)", 1 if "i12" in for_stor["failed_rules"] else 0, 1)
    passe = dict(god, units=20, daily_volume=40, trades_per_day=20, blueprint_on_market=True)
    judge(passe, P2)
    sjekk("batch på nøyaktig markedstaket godtas", 1 if "i12" in passe["failed_rules"] else 0, 0)

    # Moment straffer likviditetsfaktoren selv når volumet ser greit ut
    f_lite_moment = factors(dict(daily_volume=5000, trades_per_day=1, isk_per_day_slot=1000), P2)
    f_mye_moment = factors(dict(daily_volume=5000, trades_per_day=50, isk_per_day_slot=1000), P2)
    sjekk("få handler trekker likviditeten ned", f_lite_moment["liquidity"], round(1 / 9, 2))
    sjekk("mange handler gir full likviditet", f_mye_moment["liquidity"], 1.0)

    # ── Begrunnelsen må tåle manglende tall (den krasjet på batch_sell_days = None) ──
    from industry import reason as begrunnelse
    uten_marked = dict(r, trades_per_day=4.0, daily_volume=None, batch_sell_days=None, cycle_days=None)
    judge(uten_marked, P2)
    sjekk("begrunnelse uten markedstall krasjer ikke",
          1 if "handler/dag" in uten_marked["reason"] else 0, 1)
    bare_volum = dict(r, trades_per_day=None, daily_volume=None)
    judge(bare_volum, P2)
    sjekk("begrunnelse uten handler krasjer ikke", 1 if len(bare_volum["reason"]) > 20 else 0, 1)
    fullt = dict(god, trades_per_day=12.0, batch_sell_days=3.2, blueprint_on_market=True, units=20,
                 daily_volume=40)
    judge(fullt, P2)
    sjekk("begrunnelse med alle tall nevner salgstid",
          1 if "3.2 d å selge unna" in fullt["reason"] else 0, 1)
    sjekk("salgstiden lagres i factors (fanen viser den)", fullt["factors"]["batch_sell_days"], 3.2)

    # ── Marginen med en NYKJØPT (uforsket) BPO, og hva du bør starte med ──
    from industry import margin_at_me, start_recommendation
    me0 = margin_at_me(r, BOM, P, QUOTES, 0)
    sjekk("ME 0 gir lavere margin enn ME 10", 1 if me0 < r["margin"] else 0, 1)
    # ME 0 bruker 1/0,9 = 11 % mer materialer
    mat0 = (1000 * runs * 5.0 + 250 * runs * 10.0) * 1.01
    kost0 = (mat0 + r["job_cost"]) / r["units"]
    sjekk("ME 0-margin regnes riktig", me0,
          round(r["sell_price"] * (1 - P.sell_fees) / kost0 - 1, 4), tol=1e-3)

    billig_bpo = dict(god, product_type_id=1, name="Tåler ME 0", bpo_price=1_000_000,
                      bpo_price_source="jita", capital_per_job=2_000_000, margin_me0=0.30,
                      isk_per_day_slot=1_000_000, passed=True)
    tynn_margin = dict(god, product_type_id=2, name="Dør ved ME 0", bpo_price=500_000,
                       bpo_price_source="esi_average", capital_per_job=2_000_000, margin_me0=0.02,
                       isk_per_day_slot=9_000_000, passed=True)
    for_dyr = dict(god, product_type_id=3, name="For dyr start", bpo_price=50_000_000,
                   bpo_price_source="jita", capital_per_job=2_000_000, margin_me0=0.40,
                   isk_per_day_slot=99_000_000, passed=True)
    uten_pris = dict(god, product_type_id=4, name="Ukjent BPO-pris", bpo_price=None,
                     capital_per_job=2_000_000, margin_me0=0.40,
                     isk_per_day_slot=50_000_000, passed=True)
    anb = start_recommendation([tynn_margin, for_dyr, uten_pris, billig_bpo], P, 8_000_000)
    sjekk("anbefaling: bare den som tåler ME 0 og er innenfor kapitalen", len(anb), 1)
    sjekk("anbefaling: riktig vare", anb[0]["product_type_id"], 1)
    sjekk("anbefaling: startkostnad = BPO + batch", anb[0]["startup_cost"], 3_000_000)
    sjekk("anbefaling: tom liste når ingenting passer",
          len(start_recommendation([tynn_margin, for_dyr, uten_pris], P, 8_000_000)), 0)

    # ── «Kom i gang»-lista for en nybegynner: faste regler, ikke tersklene ──
    from industry import NYBEGYNNER, my_pick, starter_funnel, starter_list, why_not
    # Tersklene settes med vilje absurd strengt: lista skal IKKE bry seg om dem.
    P3 = IndustryProfile(**{**{k: getattr(P, k) for k in
        ('me','te','facility_tax','scc_rate','industry','advanced_industry','broker','tax',
         'cost_index','material_source')},
        'thresholds': dict(P.thresholds, min_margin=0.95, min_profit_per_run=10_000_000,
                           starter_min_trades=1000, starter_max_cost_share=0.0001,
                           newbro_runs_per_day=1)})
    fellesfelt = dict(passed=True, sell_price=100_000.0, units_per_run=2, time_per_run_s=3600,
                      margin=0.60, daily_volume=5000, factors={'trades_per_day': 40},
                      failed_rules=[])
    god_start = dict(fellesfelt, product_type_id=1, name="God start", bpo_price=1_000_000,
                     margin_me0=0.40, cost_per_unit_me0=60_000.0, runs_market_per_day=50)
    dyr_bpo = dict(fellesfelt, product_type_id=2, name="Dyr BPO", bpo_price=200_000_000,
                   margin_me0=0.50, cost_per_unit_me0=50_000.0, runs_market_per_day=50)
    tap_ved_me0 = dict(fellesfelt, product_type_id=3, name="Taper ved ME 0", bpo_price=500_000,
                       margin_me0=-0.05, cost_per_unit_me0=105_000.0, runs_market_per_day=50)
    smaatt = dict(fellesfelt, product_type_id=4, name="Ikke verdt bryet", bpo_price=100_000,
                  margin_me0=0.40, cost_per_unit_me0=600.0, runs_market_per_day=50,
                  units_per_run=1, sell_price=1_000.0)
    tregt = dict(fellesfelt, product_type_id=5, name="Markedet tar lite", bpo_price=500_000,
                 margin_me0=0.40, cost_per_unit_me0=60_000.0, runs_market_per_day=0.5)
    forkastet = dict(fellesfelt, product_type_id=6, name="Forkastet av dommen", passed=False,
                     bpo_price=500_000, margin_me0=0.45, cost_per_unit_me0=60_000.0,
                     runs_market_per_day=50, failed_rules=['i2'])
    feilpris = dict(fellesfelt, product_type_id=7, name="Feilpris", bpo_price=500_000,
                    margin_me0=0.80, cost_per_unit_me0=60_000.0, runs_market_per_day=50,
                    failed_rules=['i1x'])
    faller = dict(fellesfelt, product_type_id=8, name="Prisen faller", bpo_price=500_000,
                  margin_me0=0.75, cost_per_unit_me0=60_000.0, runs_market_per_day=50,
                  failed_rules=['i6'])
    alle = [god_start, dyr_bpo, tap_ved_me0, smaatt, tregt, forkastet, feilpris, faller]
    liste = starter_list(alle, P3, 8_000_000)
    navn = [x["name"] for x in liste]

    sjekk("kom i gang: tersklene i Avansert påvirker ikke lista", len(liste) >= 4 and 1 or 0, 1)
    sjekk("kom i gang: vare dommen forkastet på volum er MED (lista har egne krav)",
          1 if "Forkastet av dommen" in navn else 0, 1)
    sjekk("kom i gang: negativ margin ved ME 0 er ikke med",
          1 if "Taper ved ME 0" not in navn else 0, 1)
    sjekk("kom i gang: for liten fortjeneste per run er ikke med",
          1 if "Ikke verdt bryet" not in navn else 0, 1)
    sjekk("kom i gang: urealistisk pris (i1x) er ikke med", 1 if "Feilpris" not in navn else 0, 1)
    sjekk("kom i gang: fallende pris (i6) er ikke med", 1 if "Prisen faller" not in navn else 0, 1)
    sjekk("kom i gang: dyr BPO er MED, men merket", 1 if "Dyr BPO" in navn else 0, 1)
    sjekk("kom i gang: dyr BPO er ikke merket som overkommelig",
          1 if [x for x in liste if x["name"] == "Dyr BPO"][0]["affordable"] else 0, 0)
    sjekk("kom i gang: det du har råd til kommer først", navn[0], "Forkastet av dommen")
    sjekk("kom i gang: rangert på margin ved ME 0",
          [round(float(x["margin_me0"]), 2) for x in liste if x["affordable"]],
          sorted([round(float(x["margin_me0"]), 2) for x in liste if x["affordable"]], reverse=True))

    g = [x for x in liste if x["name"] == "God start"][0]
    netto_stk = 100_000 * (1 - P3.sell_fees) - 60_000
    sjekk("kom i gang: netto per enhet", g["profit_per_unit"], round(netto_stk, 2))
    sjekk("kom i gang: netto per run (2 stk)", g["profit_per_run"], round(netto_stk * 2, 2))
    sjekk("kom i gang: startkostnad = BPO + én run materialer", g["startup_cost"],
          round(1_000_000 + 60_000 * 2, 2))
    sjekk("kom i gang: tre runs per døgn, uavhengig av terskelen",
          g["runs_per_day"], NYBEGYNNER["runs_per_dag"])

    t = [x for x in liste if x["name"] == "Markedet tar lite"][0]
    sjekk("kom i gang: markedet begrenser runs når det tar mindre enn tre", t["runs_per_day"], 0.5)
    sjekk("kom i gang: fortjeneste per døgn følger markedet",
          t["profit_per_day"], round(t["profit_per_run"] * 0.5, 2))

    # ── «Selger helt ok»: handler per dag, med dagsvolum som reserve ──
    tynn = dict(fellesfelt, product_type_id=10, name="Bare 4 handler", bpo_price=125_000,
                margin_me0=0.50, cost_per_unit_me0=100_000.0, runs_market_per_day=50,
                units_per_run=1, sell_price=200_000.0, factors={'trades_per_day': 4})
    sjekk("tynt marked tas bare inn når lista ellers blir kort, og merkes",
          [x["thin_market"] for x in starter_list([tynn], P3, 8_000_000)], [True])
    sjekk("tynt marked kommer ikke med når det finnes nok gode",
          1 if "Bare 4 handler" not in [x["name"] for x in
              starter_list([tynn] + [dict(god_start, product_type_id=100 + i, name=f"God {i}")
                                     for i in range(10)], P3, 8_000_000)] else 0, 1)
    uten_data = dict(tynn, product_type_id=11, name="Ingen salgbarhetsdata",
                     factors={}, daily_volume=None)
    sjekk("vare uten handels- og volumtall er aldri «selger ok»",
          len(starter_list([uten_data], P3, 8_000_000)), 0)
    volum_reserve = dict(tynn, product_type_id=12, name="Bare volum", factors={},
                         daily_volume=500)
    sjekk("dagsvolum brukes når handelstallet mangler",
          len(starter_list([volum_reserve], P3, 8_000_000)), 1)

    # ── «Hvis jeg skulle velge for deg»: likviditet avgjør blant de nesten like gode ──
    topp_tynn = dict(fellesfelt, product_type_id=30, name="Best margin, lite handel",
                     bpo_price=125_000, margin_me0=0.80, cost_per_unit_me0=100_000.0,
                     runs_market_per_day=50, units_per_run=1, sell_price=200_000.0,
                     factors={'trades_per_day': 12})
    nesten_likvid = dict(fellesfelt, product_type_id=31, name="Nesten like god, mye handel",
                         bpo_price=125_000, margin_me0=0.65, cost_per_unit_me0=100_000.0,
                         runs_market_per_day=50, units_per_run=1, sell_price=190_000.0,
                         factors={'trades_per_day': 400})
    valg = my_pick(starter_list([topp_tynn, nesten_likvid], P3, 8_000_000))
    sjekk("mitt valg: mest omsatte vinner blant de nesten like gode",
          valg["name"], "Nesten like god, mye handel")
    sjekk("mitt valg: begrunnelsen nevner varen med best margin",
          1 if "Best margin, lite handel" in valg["reason"] else 0, 1)

    langt_bak = dict(nesten_likvid, product_type_id=32, name="Langt bak", margin_me0=0.30)
    valg2 = my_pick(starter_list([topp_tynn, langt_bak], P3, 8_000_000))
    sjekk("mitt valg: for dårlig margin vinner ikke på handelstall",
          valg2["name"], "Best margin, lite handel")
    sjekk("mitt valg: begrunnelsen sier at den er best",
          1 if "Best margin" in valg2["reason"] else 0, 1)
    sjekk("mitt valg: velger blant det du ikke har råd til om nødvendig",
          my_pick(starter_list([dyr_bpo], P3, 8_000_000))["name"], "Dyr BPO")
    sjekk("mitt valg: tom liste gir ingen anbefaling", 1 if my_pick([]) is None else 0, 1)
    sjekk("mitt valg: tynt marked får en advarsel i begrunnelsen",
          1 if "tynt" in my_pick(starter_list([tynn], P3, 8_000_000))["reason"] else 0, 1)

    # ── Trakten: en tom liste må kunne forklare hvor forslagene ble borte ──
    trakt = starter_funnel(alle + [dict(fellesfelt, product_type_id=40, name="Uten BPO",
                                        bpo_price=None, margin_me0=0.40,
                                        cost_per_unit_me0=60_000.0)], P3, 8_000_000)
    steg = {t["step"]: t["count"] for t in trakt}
    sjekk("trakt: fem steg", len(trakt), 5)
    sjekk("trakt: teller alt roboten vurderte", steg["vurdert av roboten"], 9)
    sjekk("trakt: teller bort dem uten pris og dem med upålitelige tall",
          steg["har blueprint-pris og priser å regne på"], 6)
    sjekk("trakt: teller bort dem som taper penger ved ME 0",
          steg["positiv margin med uforsket blueprint"], 5)
    sjekk("trakt: teller bort dem med for liten fortjeneste",
          steg["minst 5k fortjeneste per run"], 4)
    sjekk("trakt: stegene er i samme rekkefølge som filteret",
          trakt[0]["step"], "vurdert av roboten")

    grunner = why_not([forkastet, dict(fellesfelt, failed_rules=['i2', 'i11']),
                       dict(fellesfelt, failed_rules=['i2'])])
    sjekk("hvorfor ikke: teller vanligste grunn først", grunner[0]["rule"], "i2")
    sjekk("hvorfor ikke: riktig antall", grunner[0]["count"], 3)
    sjekk("hvorfor ikke: grunnen har lesbar tekst",
          1 if "dagsvolum" in grunner[0]["text"].lower() else 0, 1)

    # ── Oppskrift-parseren mot de to formene kildene faktisk bruker (sjekket med probe_sources.py) ──
    from ingest_industry import parse_blueprints
    ccp = {"681": {"blueprintTypeID": 681, "maxProductionLimit": 300,
                   "activities": {"manufacturing": {"materials": [{"quantity": 32, "typeID": 34},
                                                                  {"quantity": 6, "typeID": 35}],
                                                    "products": [{"quantity": 2, "typeID": 165}],
                                                    "time": 600},
                                  "copying": {"time": 480}}},
           "999": {"blueprintTypeID": 999, "activities": {"copying": {"time": 1}}}}   # uten manufacturing
    ut = parse_blueprints(ccp)
    sjekk("parser: CCP-format, antall oppskrifter", len(ut), 1)
    sjekk("parser: produkt", ut[681]["product_type_id"], 165)
    sjekk("parser: enheter per run", ut[681]["units_per_run"], 2)
    sjekk("parser: tid", ut[681]["base_time_s"], 600)
    sjekk("parser: maks runs", ut[681]["max_runs"], 300)
    sjekk("parser: materialmengde", ut[681]["materials"][34], 32)

    everef = [{"blueprint_type_id": 681, "max_production_limit": 300,
               "activities": {"manufacturing": {"materials": [{"type_id": 34, "quantity": 32}],
                                                "products": [{"type_id": 165, "quantity": 1}],
                                                "time": 600}}}]
    ut2 = parse_blueprints(everef)
    sjekk("parser: EVE Ref-format", ut2[681]["materials"][34], 32)
    sjekk("parser: EVE Ref-format, produkt", ut2[681]["product_type_id"], 165)

    print()
    if FEIL:
        print(f"{len(FEIL)} feil: {', '.join(FEIL)}")
        sys.exit(1)
    print("Alle formler stemmer.")


if __name__ == "__main__":
    main()
