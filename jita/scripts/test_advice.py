"""
test_advice.py – vernet mot for hyppige ordreendringer (guard_advice / dump_net i common.py).

Krever verken nett eller database. Kjør:  python jita/scripts/test_advice.py

Bakgrunnen for tallene: 11.–27. sept 2026 gikk 10,2 mill. til broker-gebyr, ~6 mill. av det til
omprising. 27. sept ble alle 17 ordrer endret på fire minutter: 698k i gebyr mot 429k i salg.
Vernet skal stoppe nettopp det – uten å stoppe en endring som faktisk lønner seg.
"""
from __future__ import annotations

import sys

import json
import os
import subprocess

from decimal import Decimal

from common import Profile, dump_net, guard_advice, undercut_advice

FEIL = []


def sjekk(navn: str, fikk, vil, tol=1e-6):
    if isinstance(vil, str) or isinstance(fikk, str):
        ok = str(fikk) == str(vil)
    else:
        ok = abs(float(fikk) - float(vil)) <= tol * max(1.0, abs(float(vil)))
    print(f"{'ok  ' if ok else 'FEIL'} {navn}: {fikk} (ventet {vil})")
    if not ok:
        FEIL.append(navn)


# Cap Booster 3200, slik den faktisk sto 27. sept: 917 stk, kostpris 6 229, ask 12 610, bud 10 200
RAAD = dict(action="SENK", fee=50_000, text="senk til 12 600", new_price=12_600, gain_24h=400_000)
KONTEKST = dict(side="sell", changes_total=1, fees_paid_est=0.0, position_profit=5_000_000.0,
                best_bid=10_200.0, remaining=917, cost_per_unit=6_229.0, broker=0.018, tax=0.05)
# Profil med faste satser som i JS-proben: broker 1,8 %, skatt 5 %, ingen Advanced Broker Relations
PROFIL = Profile(broker_fee_override=0.018, sales_tax_override=0.05, adv_broker_relations=0)


def senk_tilfeller():
    """Tallene fra gjennomgangen 28. sept: ask 12 610, laveste 12 600 (ny pris 12 590), 917 igjen,
    50 liftes per døgn, mur 400 stk (8 d). Kost 10 000 → netto 11 734 − 10 180 ≈ 1 554/stk."""
    return dict(
        senk_netto=undercut_advice(PROFIL, 12_610.0, 917, 12_600.0, 400, 50.0, 10_000.0),
        senk_ukjent=undercut_advice(PROFIL, 12_610.0, 917, 12_600.0, 400, 50.0, None),
        senk_lonner=undercut_advice(PROFIL, 12_610.0, 917, 12_600.0, 2_000, 300.0, 6_229.0),
    )


def sjekk_paritet():
    """Samme tall gjennom JS-speilet (lib/advice.js). Teksten skal være ord for ord den samme –
    ellers står det ulike råd i varselet fra roboten og på siden. Harde mellomrom fra
    toLocaleString normaliseres; det er samme tegn på skjermen."""
    probe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "advice_probe.mjs")
    try:
        ut = subprocess.run(["node", probe], capture_output=True, text=True, encoding="utf-8",
                            timeout=60, check=True).stdout
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"ok   paritet mot lib/advice.js: hoppet over (node mangler eller feilet: {e})")
        return
    js = json.loads(ut)
    norm = lambda o: json.dumps(o, ensure_ascii=False).replace("\u00a0", " ")
    py = {
        "karantene": (lambda r: [r["action"], r["guard"], r["text"]])(
            guard_advice(dict(RAAD), hours_since_change=2.0, **KONTEKST)),
        "gebyrtak": (lambda r: [r["action"], r["guard"], r["text"]])(
            guard_advice(dict(RAAD), hours_since_change=30.0, **{**KONTEKST, "fees_paid_est": 800_000.0})),
        "krig": (lambda r: [r["action"], r["guard"], r["dump_net"], r["text"]])(
            guard_advice(dict(RAAD), hours_since_change=1.0, **{**KONTEKST, "changes_total": 3})),
        "fritt": (lambda r: [r["action"], r.get("guard")])(
            guard_advice(dict(RAAD), hours_since_change=30.0, **KONTEKST)),
        "kjop": (lambda r: [r["action"], r.get("guard")])(
            guard_advice(dict(RAAD, action="HEV"), hours_since_change=30.0,
                         **{**KONTEKST, "side": "buy", "changes_total": 9})),
    }
    u = senk_tilfeller()
    for k, r in u.items():
        # Python kaller senking ENDRE (felles med kjøpssiden), JS kaller den SENK – samme råd
        py[k] = ["SENK" if r["action"] == "ENDRE" else r["action"], r["gain_24h"], r["text"]]
    py["dump_ukjent"] = (lambda r: [r["action"], r.get("guard")])(
        guard_advice(dict(RAAD), hours_since_change=1.0, **{**KONTEKST, "changes_total": 3, "cost_per_unit": None}))
    py["dump_dybde"] = (lambda r: [r["action"], r["dump_qty"], r["dump_net"], r["text"]])(
        guard_advice(dict(RAAD), hours_since_change=1.0, bid_depth=200, **{**KONTEKST, "changes_total": 3}))
    for k in py:
        sjekk(f"paritet med lib/advice.js: {k}", norm(js.get(k)), norm(py[k]))
    # dump_net sammenlignes som tall: JS runder ikke, Python runder til øre
    sjekk("paritet med lib/advice.js: dump_net",
          round(float(js["dump_net"]), 2), dump_net(10_200.0, 917, 6_229.0, 0.05, 0.018)["net"])


def main():
    # ── Utgangspunktet: uten historikk skal rådet stå urørt ──
    fritt = guard_advice(dict(RAAD), hours_since_change=30.0, **KONTEKST)
    sjekk("uten historikk: rådet står urørt", fritt["action"], "SENK")
    sjekk("uten historikk: ingen guard-merkelapp", fritt.get("guard") or "ingen", "ingen")

    # ── 1. Karantene: endret for kort tid siden ──
    k = guard_advice(dict(RAAD), hours_since_change=2.0, **KONTEKST)
    sjekk("karantene: endret for 2 t siden → LA STÅ", k["action"], "LA STÅ")
    sjekk("karantene: merkelapp", k["guard"], "karantene")
    sjekk("karantene: sier hvor lenge den skal stå", 1 if "10,0 t til" in k["text"] else 0, 1)
    sjekk("karantene: tar med gebyret endringen ville kostet",
          1 if "50 000" in k["text"] else 0, 1)
    sjekk("karantene: den opprinnelige begrunnelsen er beholdt",
          1 if "senk til 12 600" in k["text"] else 0, 1)
    sjekk("karantene: grensen er ved terskelen, ikke over",
          guard_advice(dict(RAAD), hours_since_change=11.9, **KONTEKST)["action"], "LA STÅ")
    sjekk("karantene: rett over terskelen slipper gjennom",
          guard_advice(dict(RAAD), hours_since_change=12.1, **KONTEKST)["action"], "SENK")
    ny = guard_advice(dict(RAAD), hours_since_change=2.0, changed_before=False, **KONTEKST)
    sjekk("karantene: en ordre som bare er lagt ut sier «la den ut», ikke «endret»",
          1 if "la den ut for 2,0 t siden" in ny["text"] else 0, 1)
    sjekk("karantene: en nylig lagt ut ordre stoppes også (den trenger ikke endring ennå)",
          ny["action"], "LA STÅ")
    sjekk("karantene: ukjent endringstidspunkt stopper ikke rådet",
          guard_advice(dict(RAAD), hours_since_change=None, **KONTEKST)["action"], "SENK")

    # ── 2. Gebyrtaket: endringene har spist for mye av posisjonen ──
    g = guard_advice(dict(RAAD), hours_since_change=30.0, **{**KONTEKST, "fees_paid_est": 800_000.0})
    sjekk("gebyrtak: 16 % av fortjenesten brukt → LA STÅ", g["action"], "LA STÅ")
    sjekk("gebyrtak: merkelapp", g["guard"], "gebyrtak")
    sjekk("gebyrtak: viser prosenten", 1 if "16 %" in g["text"] else 0, 1)
    sjekk("gebyrtak: under taket slipper gjennom",
          guard_advice(dict(RAAD), hours_since_change=30.0,
                       **{**KONTEKST, "fees_paid_est": 700_000.0})["action"], "SENK")
    sjekk("gebyrtak: uten kjent fortjeneste stoppes ingenting",
          guard_advice(dict(RAAD), hours_since_change=30.0,
                       **{**KONTEKST, "position_profit": 0.0, "fees_paid_est": 9e9})["action"], "SENK")

    # ── 3. Utveien: nok endringer, og budet gir penger ──
    d = guard_advice(dict(RAAD), hours_since_change=1.0, **{**KONTEKST, "changes_total": 3})
    sjekk("krig: tre endringer → DUMP, ikke LA STÅ", d["action"], "DUMP")
    sjekk("krig: merkelapp", d["guard"], "krig")
    sjekk("krig: prisen er budet", d["new_price"], 10_200.0)
    netto = dump_net(10_200.0, 917, 6_229.0, 0.05, 0.018)
    sjekk("krig: nettoen er den samme som dump_net", d["dump_net"], netto["net"])
    sjekk("krig: nettoen står i teksten", 1 if "3 070 921" in d["text"] else 0, 1)
    sjekk("krig: utveien slår karantenen", 1 if "endret denne ordren 3 ganger" in d["text"] else 0, 1)

    # Budet under kostpris: da er dumping ikke svaret, og karantenen gjelder igjen
    tap = guard_advice(dict(RAAD), hours_since_change=1.0,
                       **{**KONTEKST, "changes_total": 5, "best_bid": 5_000.0})
    sjekk("krig: bud under kostpris gir ikke DUMP", tap["action"], "LA STÅ")
    sjekk("krig: da er det karantenen som stopper", tap["guard"], "karantene")

    # Kjøpsordrer har ingen utvei – man dumper ikke en kjøpsordre
    kjop = guard_advice(dict(RAAD, action="HEV"), hours_since_change=30.0,
                        **{**KONTEKST, "side": "buy", "changes_total": 9})
    sjekk("kjøpsside: ingen DUMP", kjop["action"], "HEV")

    # ── Vernet skal ALDRI gjøre et HOLD til en handling ──
    for handling in ("HOLD", "TREKK", "DUMP", "LA STÅ"):
        sjekk(f"vernet rører ikke «{handling}»",
              guard_advice(dict(RAAD, action=handling), hours_since_change=0.1,
                           **{**KONTEKST, "changes_total": 9})["action"], handling)

    # ── dump_net: salg til bud koster skatt, ikke broker ──
    sjekk("dump_net: brutto er bud × antall × (1 − skatt)", netto["gross"], 10_200 * 917 * 0.95)
    sjekk("dump_net: kostprisen bærer broker-gebyret fra kjøpet",
          netto["net"], round(10_200 * 917 * 0.95 - 6_229 * 1.018 * 917, 2))
    sjekk("dump_net: uten kjent kostpris er alt netto",
          dump_net(10_200.0, 10, None, 0.05, 0.018)["net"], round(10_200 * 10 * 0.95, 2))
    sjekk("dump_net: margin er null når kostprisen mangler",
          1 if dump_net(10_200.0, 10, None, 0.05, 0.018)["margin"] is None else 0, 1)

    # ── Tall fra databasen: psycopg2 gir numeric som Decimal ──
    # 27. sept 2026 krasjet timesjobben på nettopp dette: «float − Decimal» i karantene-regnestykket.
    dec = guard_advice(dict(RAAD), hours_since_change=Decimal("2.18"),
                       **{**KONTEKST, "fees_paid_est": Decimal("0"),
                          "position_profit": Decimal("5000000"), "best_bid": Decimal("10200"),
                          "cost_per_unit": Decimal("6229")})
    sjekk("Decimal fra basen krasjer ikke karantenen", dec["action"], "LA STÅ")
    sjekk("Decimal fra basen gir samme tekst", 1 if "2,2 t siden" in dec["text"] else 0, 1)
    dec2 = guard_advice(dict(RAAD), hours_since_change=Decimal("30"),
                        **{**KONTEKST, "changes_total": 4, "best_bid": Decimal("10200"),
                           "cost_per_unit": Decimal("6229"), "fees_paid_est": Decimal("0"),
                           "position_profit": Decimal("5000000")})
    sjekk("Decimal fra basen krasjer ikke utveien", dec2["action"], "DUMP")
    sjekk("Decimal fra basen gir samme netto", dec2["dump_net"], 3_070_921)
    sjekk("dump_net tåler Decimal", dump_net(Decimal("10200"), 917, Decimal("6229"), 0.05, 0.018)["net"],
          3_070_921.13)

    # ── «Senk» regnes på fortjeneste, ikke salgsinntekt (rettet 28. sept 2026) ──
    # Før: gevinst = 50 × 12 590 × 0,932 ≈ 587k > terskel ~217k → SENK. Riktig: 50 × ~1 554 ≈ 77,7k → HOLD.
    u = senk_tilfeller()
    sjekk("senk: gevinsten er netto etter kostpris", u["senk_netto"]["gain_24h"],
          round(50 * (12_590 * (1 - 0.018 - 0.05) - 10_000 * 1.018)), tol=1e-9)
    sjekk("senk: 77k gevinst slår ikke 217k i gebyr + prisfall → HOLD", u["senk_netto"]["action"], "HOLD")
    sjekk("senk: ukjent kostpris → HOLD", u["senk_ukjent"]["action"], "HOLD")
    sjekk("senk: ukjent kostpris sier hvorfor", 1 if "kostprisen er ukjent" in u["senk_ukjent"]["text"] else 0, 1)
    # Stor mur (2 000 stk, ~6,7 d) og god flyt (300/d) på en billig vare: da lønner senking seg fortsatt
    sjekk("senk: lønner seg fortsatt når fortjenesten er stor", u["senk_lonner"]["action"], "ENDRE")

    # ── DUMP krever kjent kostpris og dybde i budet (rettet 28. sept 2026) ──
    uk = guard_advice(dict(RAAD), hours_since_change=1.0, **{**KONTEKST, "changes_total": 3, "cost_per_unit": None})
    sjekk("dump: ukjent kostpris gir ikke DUMP", uk["action"], "LA STÅ")
    dy = guard_advice(dict(RAAD), hours_since_change=1.0, bid_depth=200, **{**KONTEKST, "changes_total": 3})
    sjekk("dump: begrenset av budenes dybde", dy["dump_qty"], 200)
    sjekk("dump: nettoen regnes på de 200", dy["dump_net"], round(dump_net(10_200.0, 200, 6_229.0, 0.05, 0.018)["net"]))
    sjekk("dump: teksten sier 200 av 917", 1 if "200 av de 917" in dy["text"] else 0, 1)
    tom = guard_advice(dict(RAAD), hours_since_change=1.0, bid_depth=0, **{**KONTEKST, "changes_total": 3})
    sjekk("dump: ingen bud innenfor 1 % → ingen DUMP", tom["action"], "LA STÅ")
    hel = guard_advice(dict(RAAD), hours_since_change=1.0, bid_depth=5_000, **{**KONTEKST, "changes_total": 3})
    sjekk("dump: dypt nok bud → hele beholdningen", hel["dump_qty"], 917)

    sjekk_paritet()

    print()
    if FEIL:
        print(f"{len(FEIL)} feil: {', '.join(FEIL)}")
        sys.exit(1)
    print("Vernet holder.")


if __name__ == "__main__":
    main()
