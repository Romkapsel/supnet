"""
industry.py – formlene bak industri-marginfinneren (steg 1: produksjon).

Skilt ut fra ingest_industry.py slik at tallene kan testes uten database og uten nett
(se test_industry.py). FASITEN for hva som vises i Industri-fanen.

Kildene til formlene:
  - ME/TE-runding: CCP, «Industry» – materialer rundes opp per JOBB, aldri under 1 per run.
  - Jobbavgift: EIV × (systemets manufacturing cost index + facility tax + SCC-avgift).
    EIV = sum(grunnmengde × adjusted_price fra ESI /markets/prices/) – altså FØR ME.
  - Tid: base_time × (1 − TE/100) × (1 − 0,04 × Industry) × (1 − 0,03 × Advanced Industry).
    NPC-stasjon gir ingen bonus på material eller tid (rigger finnes ikke der).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# Kategorier vi rangerer (spec: T1-moduler, droner, ammo, rigs, deployables, T1-skip)
PRODUCT_CATEGORIES = (6, 7, 8, 18, 22)      # ship, module, charge, drone, deployable
MANUFACTURING = 1                            # activityID i SDE


@dataclass
class IndustryProfile:
    """Speiler jita.industry_profile + broker/skatt fra jita.profile."""
    system_id: int = 30001395
    system_name: str = "Ylandoki"
    jumps_from_jita: int = 3
    me: int = 10
    te: int = 20
    facility_tax: float = 0.0025
    scc_rate: float = 0.04
    industry: int = 5
    advanced_industry: int = 3
    mass_production: int = 0
    adv_mass_production: int = 0
    slots: int | None = None
    sell_fee_override: float | None = None
    material_source: str = "buy"
    broker: float = 0.01                     # fra jita.profile (profile_calc)
    tax: float = 0.075
    capital_isk: float = 0.0                 # cash + bundet (jita.effective_profile)
    cost_index: float = 0.0                  # systemets manufacturing cost index
    thresholds: dict = field(default_factory=dict)

    @property
    def slot_count(self) -> int:
        return self.slots or (1 + self.mass_production + self.adv_mass_production)

    @property
    def sell_fees(self) -> float:
        """Andelen av salgsprisen som forsvinner i broker fee + sales tax."""
        if self.sell_fee_override is not None:
            return float(self.sell_fee_override)
        return float(self.broker) + float(self.tax)

    def t(self, key: str, default):
        v = (self.thresholds or {}).get(key)
        return default if v is None else v


def job_budget(p: IndustryProfile) -> float:
    """Hvor mye kapital én jobb får binde. Uten dette blir batchene dimensjonert bare etter tid,
    og forslagene havner langt over det du faktisk har (8 mill. kapital, 15 mill. per jobb)."""
    tak = float(p.t("max_capital_per_job", 20e6))
    if p.capital_isk > 0:
        tak = min(tak, p.capital_isk * float(p.t("capital_share_per_job", 0.5)))
    return max(tak, 1.0)


def tick(p: float) -> float:
    """Minste prissteg ved 4 signifikante siffer (samme som common.tick)."""
    if p <= 0:
        return 0.01
    return 10 ** (math.floor(math.log10(p)) - 3)


def material_quantity(base_qty: float, runs: int, me: int, facility_mult: float = 1.0) -> int:
    """Materialbehov for hele jobben. Aldri under 1 per run, og rundes opp per jobb – ikke per run."""
    if base_qty <= 1:
        return runs                                  # mengde 1 reduseres ikke av ME
    exact = runs * base_qty * (1 - me / 100) * facility_mult
    return max(runs, math.ceil(round(exact, 2)))


def time_per_run(base_time_s: float, p: IndustryProfile) -> float:
    return (base_time_s * (1 - p.te / 100)
            * (1 - 0.04 * p.industry)
            * (1 - 0.03 * p.advanced_industry))


def runs_for_days(base_time_s: float, p: IndustryProfile, days: float = 1.0,
                  max_runs: int | None = None) -> int:
    """Hvor mange runs én slot rekker på «days» døgn (minst 1, aldri over BPC-grensen)."""
    t = time_per_run(base_time_s, p)
    n = max(1, int(days * 86400 // max(t, 1)))
    if max_runs:
        n = min(n, max_runs)
    return n


def eiv_per_run(materials: dict[int, float], adjusted: dict[int, float]) -> float:
    """Estimated item value: grunnmengdene (før ME) ganget med ESI-ens adjusted_price."""
    return sum(q * float(adjusted.get(tid) or 0) for tid, q in materials.items())


def job_cost(eiv: float, p: IndustryProfile) -> float:
    """Jobbavgift for hele batchen: systemindeks + facility tax + SCC, alle av EIV."""
    return eiv * (float(p.cost_index) + float(p.facility_tax) + float(p.scc_rate))


def material_cost(materials: dict[int, float], runs: int, p: IndustryProfile,
                  quotes: dict[int, dict]) -> tuple[float, float, list[int]]:
    """→ (kostnad for batchen inkl. gebyr, m3 inn, materialer vi mangler pris på).

    material_source 'buy': du legger kjøpsordre (høyeste buy-pris) og betaler broker fee.
    material_source 'sell': du kjøper instant fra laveste sell-pris, ingen gebyr.
    """
    cost = 0.0
    m3 = 0.0
    missing: list[int] = []
    for tid, base_q in materials.items():
        need = material_quantity(base_q, runs, p.me)
        q = quotes.get(tid) or {}
        price = q.get("buy_max") if p.material_source == "buy" else q.get("sell_min")
        if not price:                                    # fall tilbake på den andre siden av boken
            price = q.get("sell_min") if p.material_source == "buy" else q.get("buy_max")
        if not price:
            missing.append(tid)
            continue
        unit = float(price) * (1 + p.broker if p.material_source == "buy" else 1)
        cost += unit * need
        m3 += float(q.get("volume") or 0) * need
    return cost, m3, missing


def market_units_cap(daily_volume: float | None, p: IndustryProfile) -> float | None:
    """Hvor mange enheter én batch får være: det markedet spiser innen `max_sell_days`,
    med vår andel av dagsvolumet. 200 skip i et marked som flytter 2 i uka blir liggende
    – eller tanker prisen når du senker deg for å bli kvitt dem."""
    if not daily_volume:
        return None
    return float(daily_volume) * float(p.t("volume_share", 0.10)) * float(p.t("max_sell_days", 5))


def economics(bom: dict, p: IndustryProfile, quotes: dict[int, dict],
              adjusted: dict[int, float], batch_days: float | None = None,
              max_units: float | None = None) -> dict | None:
    """Regner ut kostnad, salgspris, netto og tempo for ett produkt.

    bom: {'blueprint_type_id', 'product_type_id', 'units_per_run', 'base_time_s',
          'max_runs', 'materials': {material_type_id: grunnmengde per run}}
    → dict med tallene, eller None hvis produktet mangler pris i Jita.
    """
    mats = bom["materials"]
    if not mats:
        return None
    pid = bom["product_type_id"]
    pq = quotes.get(pid) or {}
    sell_min = float(pq.get("sell_min") or 0)
    if sell_min <= 0:
        return None

    days = p.t("batch_days", 1) if batch_days is None else batch_days
    base_time = float(bom.get("base_time_s") or 0)
    runs = runs_for_days(base_time, p, days, bom.get("max_runs")) if base_time > 0 else 1
    units_per_run = int(bom.get("units_per_run") or 1)

    # Kapitalen setter taket sammen med tiden. Jobbavgiften må være med i taket – den betales
    # samtidig med materialene, og er proporsjonal med antall runs.
    budsjett = job_budget(p)
    per_run, _, missing = material_cost(mats, 1, p, quotes)
    if missing:
        return None                                      # mangler materialpris → ikke til å stole på
    avgift_per_run = job_cost(eiv_per_run(mats, adjusted), p)
    per_run_total = per_run + avgift_per_run
    if per_run_total > 0:
        runs = max(1, min(runs, int(budsjett // per_run_total) or 1))
    # Markedet setter det tredje taket: batchen må kunne selges unna innen max_sell_days.
    if max_units:
        runs = max(1, min(runs, int(max_units // units_per_run) or 1))
    units = runs * units_per_run

    mat_cost, m3_in, missing = material_cost(mats, runs, p, quotes)
    if missing:
        return None
    eiv = eiv_per_run(mats, adjusted) * runs
    jcost = job_cost(eiv, p)
    total = mat_cost + jcost
    cost_per_unit = round(total / units, 2)              # rundes her, så netto og margin stemmer med det som vises

    sell_price = sell_min - tick(sell_min)               # ett tick under laveste ask
    net_per_unit = sell_price * (1 - p.sell_fees) - cost_per_unit
    margin = net_per_unit / cost_per_unit if cost_per_unit > 0 else 0.0

    t_run = time_per_run(base_time, p) if base_time > 0 else 0.0
    t_batch = t_run * runs
    units_per_day_slot = (86400 / t_run * units_per_run) if t_run > 0 else float(units)

    return dict(
        blueprint_type_id=bom["blueprint_type_id"], product_type_id=pid,
        runs=runs, units=units, units_per_run=units_per_run,
        material_cost=round(mat_cost, 2), job_cost=round(jcost, 2), eiv=round(eiv, 2),
        total_cost=round(total, 2), cost_per_unit=round(cost_per_unit, 2),
        sell_price=sell_price, net_per_unit=round(net_per_unit, 2), margin=margin,
        time_per_run_s=round(t_run, 1), time_per_batch_s=round(t_batch, 1),
        units_per_day_slot=round(units_per_day_slot, 2),
        capital_per_job=round(total, 2),
        m3_in=round(m3_in, 2), m3_out=round(float(pq.get("volume") or 0) * units, 2),
        buy_max=float(pq.get("buy_max") or 0), sell_min=sell_min,
    )


def realistic_throughput(row: dict, daily_volume: float | None, p: IndustryProfile) -> dict:
    """«Realistisk ISK per døgn per slot» = netto × det minste av tre tak:

      slot     – hva slotten rekker å produsere per døgn
      marked   – andelen av dagsvolumet vi tillater oss å ta (10 %)
      omløp    – hvor mange enheter kapitalen rekker å finansiere per døgn: pengene er bundet
                 fra jobben starter til varen er solgt, så én batch tar (produksjonstid +
                 tid å selge unna) før samme ISK kan brukes igjen. Dette taket ligger alltid
                 litt under de to andre, fordi batchen også må selges før pengene er tilbake.

    Uten kapitaltaket blir dyre varer urealistisk høyt rangert: 42 mill. ISK/dag på en vare
    som koster 2 mill. per stk krever 55 mill. ISK gjennom materialene hvert døgn.
    """
    share = float(p.t("volume_share", 0.10))
    slot_cap = row["units_per_day_slot"]
    market = (float(daily_volume) * share) if daily_volume else None

    prod_days = (row.get("time_per_batch_s") or 0) / 86400
    sell_days = (row["units"] / market) if market else None
    funded = None
    if sell_days is not None:
        cycle = max(prod_days + sell_days, 1 / 24)       # gulv: én time, ellers blir tallet støy
        funded = row["units"] / cycle

    tak = {"slot": slot_cap}
    if market is not None:
        tak["marked"] = market
    if funded is not None:
        tak["omløp"] = funded
    flaskehals = min(tak, key=tak.get)
    real = round(tak[flaskehals], 2)                     # rundes her, så ISK/dag stemmer med antallet som vises
    isk_day = row["net_per_unit"] * real
    return dict(realistic_units_per_day=real,
                isk_per_day_slot=round(isk_day, 2),
                isk_per_hour_slot=round(isk_day / 24, 2),
                bottleneck=flaskehals,
                batch_sell_days=None if sell_days is None else round(sell_days, 2),
                cycle_days=round(prod_days + (sell_days or 0), 3),
                potential_units_per_day=round(min(slot_cap, market) if market else slot_cap, 2))


# ── Dommeren ─────────────────────────────────────────────────────────────────
RULES = {
    "i1": "Margin under terskel",
    "i1x": "Urealistisk margin (prisen er nok ikke ekte)",
    "i2": "For lite dagsvolum",
    "i3": "For få selgere (tynt marked)",
    "i3b": "For stor spread (tynt marked)",
    "i4": "BPO-en er for dyr",
    "i5": "Binder for mye kapital per jobb",
    "i6": "Prisen faller",
    "i7": "Mangler data",
    "i8": "For lang tilbakebetaling på BPO-en",
    "i9": "Pristopp (prisen er langt over 30-dagers snitt)",
    "i10": "BPO-en kan ikke kjøpes (blueprinten finnes ikke på markedet)",
    "i11": "For få handler per dag (ingen moment i markedet)",
    "i12": "Én batch kan ikke selges unna (markedet er for tregt)",
}


def factors(row: dict, p: IndustryProfile) -> dict:
    """Faktorene som ganges inn i scoren. > 1 løfter, < 1 trekker ned."""
    min_vol = float(p.t("min_daily_volume", 20))
    vol = row.get("daily_volume") or 0
    liquidity = min(1.0, vol / max(min_vol * 5, 1)) if vol else 0.3
    trades = row.get("trades_per_day")
    if trades is not None:                                   # moment: få handler straffer, uansett volum
        liquidity = min(liquidity, min(1.0, trades / max(float(p.t("min_trades_per_day", 3)) * 3, 1)))
    orders = row.get("sell_orders")
    competition = min(1.0, 20 / max(orders, 1)) if orders else 1.0
    volat = row.get("price_volatility")
    stable = max(0.7, 1 - min(0.3, float(volat))) if volat is not None else 1.0
    drop = row.get("price_drop_30d")
    trend = max(0.6, min(1.1, 1 - float(drop) * 2)) if drop is not None else 1.0
    return dict(isk_per_day=row.get("isk_per_day_slot"), liquidity=round(liquidity, 2),
                competition=round(competition, 2), stable=round(stable, 2), trend=round(trend, 2),
                bottleneck=row.get("bottleneck"), cycle_days=row.get("cycle_days"),
                potential_units_per_day=row.get("potential_units_per_day"),
                daily_volume=vol, trades_per_day=trades, sell_orders=orders,
                volatility=None if volat is None else round(float(volat), 3),
                drop_30d=None if drop is None else round(float(drop), 3))


def judge(row: dict, p: IndustryProfile) -> dict:
    """→ row med passed, failed_rules, score, factors og begrunnelse på norsk."""
    f = factors(row, p)
    failed: list[str] = []
    margin = row.get("margin")
    vol = row.get("daily_volume")
    orders = row.get("sell_orders")

    if margin is None or row.get("cost_per_unit") is None:
        failed.append("i7")
    else:
        if margin < float(p.t("min_margin", 0.10)):
            failed.append("i1")
        if margin > float(p.t("max_margin", 3.0)):
            failed.append("i1x")
    if vol is None:
        failed.append("i7")
    elif vol < float(p.t("min_daily_volume", 20)):
        failed.append("i2")
    if orders is not None and orders < int(p.t("min_sell_orders", 5)):
        failed.append("i3")
    if row.get("sell_min") and row.get("buy_max"):
        spread = (row["sell_min"] - row["buy_max"]) / row["sell_min"]
        if spread > float(p.t("max_spread", 0.60)):
            failed.append("i3b")
    if row.get("bpo_price") and row["bpo_price"] > float(p.t("max_bpo_price", 50e6)):
        failed.append("i4")
    if row.get("capital_per_job", 0) > job_budget(p) * 1.01:   # batchen er alt begrenset av budsjettet:
        failed.append("i5")                                    # slår bare til når én enkelt run er for dyr
    if row.get("price_drop_30d") is not None and row["price_drop_30d"] > float(p.t("max_price_drop_30d", 0.15)):
        failed.append("i6")
    if row.get("payback_days") is not None and row["payback_days"] > float(p.t("max_payback_days", 30)):
        failed.append("i8")
    if row.get("price_avg_30d") and row.get("sell_min") and \
            row["sell_min"] > float(p.t("max_price_spike", 3.0)) * row["price_avg_30d"]:
        failed.append("i9")
    if row.get("blueprint_on_market") is False:
        failed.append("i10")
    # Moment: volum alene kan være én stor ordre. Antall handler per dag sier om varen faktisk flyter.
    trades = row.get("trades_per_day")
    if trades is not None and trades < float(p.t("min_trades_per_day", 3)):
        failed.append("i11")
    # Blir batchen større enn markedet spiser innen max_sell_days, blir du sittende med den.
    cap = market_units_cap(row.get("daily_volume"), p)
    if cap is not None and row.get("units") and row["units"] > cap * 1.01:
        failed.append("i12")

    score = (row.get("isk_per_day_slot") or 0) * f["liquidity"] * f["competition"] * f["stable"] * f["trend"]
    row.update(passed=not failed, failed_rules=sorted(set(failed)), score=round(score, 2),
               factors=f, reason=reason(row, f, failed, p))
    return row


def _isk(x) -> str:
    if x is None:
        return "–"
    return f"{float(x):,.0f}".replace(",", " ")


def reason(row: dict, f: dict, failed: list[str], p: IndustryProfile) -> str:
    """Begrunnelsen som vises på kortet – samme stil som station-trading-delen."""
    hours = (row.get("time_per_batch_s") or 0) / 3600
    parts = [
        f"Én jobb: {row.get('runs')} runs → {row.get('units')} stk på {hours:.1f} t. "
        f"Kostpris {_isk(row.get('cost_per_unit'))} ISK/stk "
        f"(materialer {_isk(row.get('material_cost'))} + avgift {_isk(row.get('job_cost'))}), "
        f"selges på {_isk(row.get('sell_price'))} → netto {_isk(row.get('net_per_unit'))} ISK/stk "
        f"({(row.get('margin') or 0) * 100:.1f} %).",
    ]
    if row.get("daily_volume"):
        hals = {"slot": "produksjonstiden", "marked": "markedet",
                "omløp": "kapital-omløpet"}.get(row.get("bottleneck"), "?")
        parts.append(
            f"Markedet omsetter {_isk(row['daily_volume'])} stk/dag; taket ditt er "
            f"{float(p.t('volume_share', 0.10)) * 100:.0f} % av det. Bremsen er {hals}: "
            f"{_isk(row.get('realistic_units_per_day'))} stk/dag "
            f"(slotten rekker {_isk(row.get('units_per_day_slot'))}, "
            f"kapitalen snur rundt på {row.get('cycle_days', 0):.2f} døgn) → "
            f"{_isk(row.get('isk_per_day_slot'))} ISK/dag per slot.")
    if row.get("trades_per_day") is not None:
        parts.append(f"Markedet har {row['trades_per_day']:.1f} handler/dag; batchen på "
                     f"{row.get('units')} stk tar ~{row.get('batch_sell_days', 0):.1f} d å selge unna "
                     f"med din andel av volumet.")
    if row.get("bpo_price"):
        parts.append(f"BPO {_isk(row['bpo_price'])} ISK, tilbakebetalt på "
                     f"{row.get('payback_days'):.1f} d." if row.get("payback_days") is not None
                     else f"BPO {_isk(row['bpo_price'])} ISK.")
    parts.append(f"Kapital per jobb {_isk(row.get('capital_per_job'))} ISK. "
                 f"Frakt {row.get('m3_in', 0):.0f} m3 inn / {row.get('m3_out', 0):.0f} m3 ut "
                 f"({p.jumps_from_jita} hopp).")
    weak = []
    if f["liquidity"] < 0.8:
        weak.append(f"tynn omsetning ({_isk(row.get('daily_volume'))} stk/dag)")
    if f["competition"] < 0.8:
        weak.append(f"{row.get('sell_orders')} selgere i Jita")
    if f["stable"] < 0.9:
        weak.append(f"ustabil pris ({(row.get('price_volatility') or 0) * 100:.0f} % svingning)")
    if f["trend"] < 1.0:
        weak.append(f"prisen har falt {(row.get('price_drop_30d') or 0) * 100:.0f} % på 30 d")
    if weak:
        parts.append("Svakhet: " + ", ".join(weak) + ".")
    if failed:
        parts.append("Forkastet: " + ", ".join(RULES.get(r, r) for r in failed) + ".")
    return " ".join(parts)


# ── Porteføljevelger ─────────────────────────────────────────────────────────
def pick_portfolio(rows: list[dict], slots: int, capital: float) -> dict:
    """Velg hvilke produkter slottene skal brukes på: høyest ISK/dag/slot først, én vare per slot,
    aldri mer enn 10 % av noe marked (ligger allerede i isk_per_day_slot) og innenfor kapitalen."""
    picks, left = [], float(capital)
    for r in sorted((x for x in rows if x.get("passed")),
                    key=lambda x: x.get("score") or 0, reverse=True):
        if len(picks) >= slots:
            break
        cost = float(r.get("capital_per_job") or 0)
        if cost <= 0 or cost > left:
            continue
        picks.append(dict(product_type_id=r["product_type_id"], name=r.get("name"),
                          runs=r.get("runs"), units=r.get("units"),
                          capital_per_job=cost, isk_per_day_slot=r.get("isk_per_day_slot"),
                          hours=round((r.get("time_per_batch_s") or 0) / 3600, 1),
                          margin=r.get("margin"), m3_in=r.get("m3_in"), m3_out=r.get("m3_out")))
        left -= cost
    return dict(slots=slots, capital=float(capital), left=round(left, 2), picks=picks,
                isk_per_day=round(sum(p["isk_per_day_slot"] or 0 for p in picks), 2))
