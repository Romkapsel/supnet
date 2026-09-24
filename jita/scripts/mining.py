"""
mining.py – formlene bak mining-laget (steg 2).

Svarer på to spørsmål per malm:
  1. Hva er malmen verdt per m3 – refinet til mineraler, eller solgt som den er?
  2. Hva blir det i ISK per time med din mining-rate?

Testet av test_mining.py, uten nett og database.

Forutsetninger:
  - Refine i NPC-stasjon: utbyttet er en parameter (`reprocess_yield`, standard 52 % =
    50 % base × skills). Struktur med rigger gir mer; sett parameteren høyere da.
  - Mineraler og malm selges i Jita. For hver vare velges den beste av to veier:
      salgsordre  = (laveste ask − ett tick) × (1 − broker − skatt)
      dumping     = høyeste bud × (1 − skatt)          (ingen broker fee på å selge til et bud)
  - Malmen må finnes der du miner: `available_groups` i profilen (gruppenavn fra jita.types).
"""
from __future__ import annotations

from dataclasses import dataclass, field

ASTEROID_CATEGORY = 25          # category_id for malm og is i jita.types


@dataclass
class MiningProfile:
    reprocess_yield: float = 0.52
    m3_per_hour: float = 3000
    jumps_from_jita: int = 3
    broker: float = 0.01
    tax: float = 0.075
    thresholds: dict = field(default_factory=dict)

    def t(self, key: str, default):
        v = (self.thresholds or {}).get(key)
        return default if v is None else v

    @property
    def available_groups(self) -> set[str]:
        return {str(g).lower() for g in self.t("available_groups", [])}


def tick(p: float) -> float:
    import math
    if p <= 0:
        return 0.01
    return 10 ** (math.floor(math.log10(p)) - 3)


def net_sale(quote: dict | None, p: MiningProfile) -> tuple[float, str]:
    """Hva du faktisk får per enhet, og hvilken vei som er best.
    → (netto per enhet, 'salgsordre' | 'dumping' | '–')"""
    if not quote:
        return 0.0, "–"
    ask = float(quote.get("sell_min") or 0)
    bid = float(quote.get("buy_max") or 0)
    via_order = (ask - tick(ask)) * (1 - p.broker - p.tax) if ask > 0 else 0.0
    via_dump = bid * (1 - p.tax) if bid > 0 else 0.0
    if via_order <= 0 and via_dump <= 0:
        return 0.0, "–"
    return (via_order, "salgsordre") if via_order >= via_dump else (via_dump, "dumping")


def refined_value(yields: dict[int, float], batch_size: int, p: MiningProfile,
                  quotes: dict[int, dict]) -> tuple[float, dict[str, float], list[int]]:
    """Verdien av mineralene én enhet malm gir.
    → (netto per enhet malm, {mineral_type_id: andel av verdien}, mineraler uten pris)"""
    if not yields or batch_size <= 0:
        return 0.0, {}, []
    total = 0.0
    per_mineral: dict[str, float] = {}
    missing: list[int] = []
    for mineral_id, qty in yields.items():
        netto, _ = net_sale(quotes.get(mineral_id), p)
        if netto <= 0:
            missing.append(mineral_id)
            continue
        verdi = qty * p.reprocess_yield * netto
        total += verdi
        per_mineral[str(mineral_id)] = verdi
    if total > 0:
        per_mineral = {k: round(v / total, 4) for k, v in per_mineral.items()}
    return total / batch_size, per_mineral, missing


def evaluate(ore: dict, p: MiningProfile, quotes: dict[int, dict]) -> dict | None:
    """Regner ut verdien av én malmtype.

    ore: {'ore_type_id', 'name', 'group_name', 'volume', 'batch_size', 'yields': {mineral: mengde}}
    """
    volume = float(ore.get("volume") or 0)
    if volume <= 0:
        return None
    batch = int(ore.get("batch_size") or 0)
    refinet, mix, missing = refined_value(ore.get("yields") or {}, batch, p, quotes)
    rå, rå_vei = net_sale(quotes.get(ore["ore_type_id"]), p)

    ruter = {}
    if refinet > 0:
        ruter["refine"] = refinet / volume
    if rå > 0:
        ruter["rå"] = rå / volume
    if not ruter:
        return None
    beste = max(ruter, key=ruter.get)

    return dict(
        ore_type_id=ore["ore_type_id"], name=ore.get("name"), group_name=ore.get("group_name"),
        volume=volume, batch_size=batch,
        refined_value_per_unit=round(refinet, 2),
        refined_value_per_m3=round(refinet / volume, 2) if refinet else None,
        raw_net_per_unit=round(rå, 2) if rå else None,
        raw_net_per_m3=round(rå / volume, 2) if rå else None,
        raw_route=rå_vei,
        best_route=beste,
        best_value_per_m3=round(ruter[beste], 2),
        isk_per_hour=round(ruter[beste] * p.m3_per_hour, 2),
        refine_premium=round(ruter["refine"] / ruter["rå"] - 1, 4) if len(ruter) == 2 else None,
        mineral_mix=mix,
        missing_prices=missing,
        available=(str(ore.get("group_name") or "").lower() in p.available_groups),
    )


RULES = {
    "m1": "Finnes ikke der du miner",
    "m2": "Ingen pris i Jita (verken malm eller mineraler)",
    "m3": "For tynt marked for malmen selv (gjelder bare rå-salg)",
    "m4": "Mangler refine-utbytte",
}


def judge(row: dict, p: MiningProfile) -> dict:
    """Merker raden med hvorfor den ikke er aktuell. Score = ISK/time, nullet hvis den ikke gjelder."""
    failed = []
    if not row.get("available"):
        failed.append("m1")
    if not row.get("best_value_per_m3"):
        failed.append("m2")
    if not row.get("refined_value_per_m3"):
        failed.append("m4")
    if row.get("best_route") == "rå":
        vol = row.get("ore_daily_volume")
        handler = row.get("ore_trades_per_day")
        if (vol is not None and vol < float(p.t("min_daily_volume", 100))) or \
           (handler is not None and handler < float(p.t("min_trades_per_day", 3))):
            failed.append("m3")
    row["failed_rules"] = failed
    row["passed"] = not failed
    row["score"] = round(row.get("isk_per_hour") or 0, 2) if not failed else 0.0
    row["notes"] = notes(row, p)
    return row


def _isk(x) -> str:
    if x is None:
        return "–"
    return f"{float(x):,.0f}".replace(",", " ")


def notes(row: dict, p: MiningProfile) -> str:
    deler = []
    if row.get("refined_value_per_m3"):
        deler.append(f"Refinet ({p.reprocess_yield * 100:.0f} % utbytte): "
                     f"{_isk(row['refined_value_per_m3'])} ISK/m3.")
    if row.get("raw_net_per_m3"):
        deler.append(f"Solgt som den er ({row.get('raw_route')}): "
                     f"{_isk(row['raw_net_per_m3'])} ISK/m3.")
    if row.get("refine_premium") is not None:
        p_ = row["refine_premium"]
        deler.append(f"Refine gir {abs(p_) * 100:.0f} % {'mer' if p_ > 0 else 'mindre'} enn å selge rått.")
    deler.append(f"Med {_isk(p.m3_per_hour)} m3/time: {_isk(row.get('isk_per_hour'))} ISK/time "
                 f"({row.get('best_route')}).")
    if row.get("missing_prices"):
        deler.append(f"{len(row['missing_prices'])} mineral(er) mangler pris – verdien er et minimum.")
    if row.get("failed_rules"):
        deler.append("Ikke aktuell: " + ", ".join(RULES.get(r, r) for r in row["failed_rules"]) + ".")
    return " ".join(deler)
